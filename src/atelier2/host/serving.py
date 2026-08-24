from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from atelier2.adapters.claude_subscription import (
    ClaudeSubscriptionExecutorFactory,
    ClaudeSubscriptionSettings,
    ClaudeWorkspaceToolExecutorFactory,
)
from atelier2.adapters.codex_subscription import (
    CodexSubscriptionExecutorFactory,
    CodexSubscriptionSettings,
)
from atelier2.adapters.dbos.advancer import (
    agent_open_pr_runs_pending_live_redemption,
)
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.artifact_store import DbosArtifactStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import (
    AGENT_TERMINATION_GRACE_SECONDS,
    SQLITE_LOCK_TIMEOUT_SECONDS,
    DbosRuntime,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.webhook_delivery import DbosWebhookDeliveryPublisher
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.free_runner_executor import FreeRunnerExecutorFactory
from atelier2.adapters.github import (
    GitHubRepository,
    GitHubTokenCredential,
    LiveGitHubEffectAdapterFactory,
)
from atelier2.adapters.grok_subscription import (
    GrokSubscriptionExecutorFactory,
    GrokSubscriptionSettings,
    GrokWorkspaceToolExecutorFactory,
)
from atelier2.adapters.http_webhook_transport import open_webhook_transport
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.limits import (
    ApiLimits,
    base64_characters_for,
    durable_projection_limit,
)
from atelier2.api.stream import EventPollBackoff
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.pages import PageLimit
from atelier2.host.address import DEFAULT_HOST, DEFAULT_PORT, is_loopback_host
from atelier2.host.logging import configure_process_logging
from atelier2.host.webhook_delivery import (
    WebhookDeliveryLoop,
    WebhookDeliverySettings,
    resolve_signing_key,
    webhook_delivery_lifespan,
)
from atelier2.ports.agent_executions import (
    AgentExecutorCarrier,
    AgentExecutorFactoryV2,
    AgentExecutorRegistration,
)
from atelier2.ports.effects import EffectAdapterFactory

# The edge must admit exactly the largest result the durable agent contract
# accepts, and nothing larger: a tighter bound refuses work the store would
# have kept, a looser one admits work the store then refuses. So both numbers
# are derivations of one owner rather than typed constants -- the decoded bound
# *is* the durable bound, and the base64 bound is that same number in transport
# form. As typed literals they drifted silently, because `api/stream.py`
# reports the resulting refusal as a clean end of stream.
MAXIMUM_DECODED_PAYLOAD_BYTES = MAXIMUM_AGENT_OUTPUT_BYTES_V2
MAXIMUM_BASE64_CHARACTERS = base64_characters_for(MAXIMUM_DECODED_PAYLOAD_BYTES)
# The HTTP body owns its transport envelope independently of any one field. This
# deployment default admits the largest supported answer together with its JSON
# keys, revision, and maximum node id; a behavior test crosses the real middleware
# seam so envelope growth cannot silently make that legal payload undeliverable.
MAXIMUM_REQUEST_BODY_BYTES = 68 * 1_024
MAXIMUM_FIELD_CHARACTERS = MAXIMUM_AGENT_FIELD_CHARACTERS
MAXIMUM_WORKFLOW_NODES = 100

# A listing that says what its revisions are called has to read and parse their
# documents, and the measurement says those are two costs in two units: the
# parse is paid per node -- 0.66 to 1.52 ms per node, holding across a 150x byte
# range -- and the read is paid per byte. So one page may parse no more nodes,
# and move no more document bytes, than this edge already admits for a single
# document. Both are derivations of those two owners rather than second literals,
# which is what stops a raised document bound from leaving a page bound behind.
# Neither implies the other: a hundred one-node documents still weigh megabytes,
# and a page bounded only by bytes still holds hundreds of nodes.
MAXIMUM_ENRICHED_PAGE_NODES = MAXIMUM_WORKFLOW_NODES
MAXIMUM_ENRICHED_PAGE_DOCUMENT_BYTES = MAXIMUM_REQUEST_BODY_BYTES

EVENT_PAGE_SIZE = 50
MAXIMUM_CONTROL_QUERIES = 8
MAXIMUM_EVENT_POLL_QUERIES = 2
MAXIMUM_QUERY_ADMISSION_WAIT_MILLISECONDS = 1_000

INITIAL_EVENT_POLL_DELAY_SECONDS = 0.05
MAXIMUM_EVENT_POLL_DELAY_SECONDS = 1.0
EVENT_POLL_DELAY_MULTIPLIER = 2.0


def api_limits(
    *,
    event_page_size: int = EVENT_PAGE_SIZE,
    maximum_control_queries: int = MAXIMUM_CONTROL_QUERIES,
    maximum_event_poll_queries: int = MAXIMUM_EVENT_POLL_QUERIES,
    maximum_query_admission_wait_milliseconds: int = (
        MAXIMUM_QUERY_ADMISSION_WAIT_MILLISECONDS
    ),
) -> ApiLimits:
    """The limits one served instance enforces, with this deployment's answers.

    The bounds above the signature are the wire's and the store's: they say what
    the product can represent, and an instance does not get to disagree with
    them. The four below it are this instance's own -- how much reading it admits
    at once and how large a page it answers with -- and they are the ones a second
    machine honestly wants differently.

    The defaults are the same values the host baked in before, in the one place
    that names them, so an instance that configures nothing behaves exactly as it
    did.
    """

    return ApiLimits(
        maximum_request_body_bytes=MAXIMUM_REQUEST_BODY_BYTES,
        maximum_field_characters=MAXIMUM_FIELD_CHARACTERS,
        maximum_base64_characters=MAXIMUM_BASE64_CHARACTERS,
        maximum_decoded_payload_bytes=MAXIMUM_DECODED_PAYLOAD_BYTES,
        maximum_workflow_nodes=MAXIMUM_WORKFLOW_NODES,
        maximum_enriched_page_nodes=MAXIMUM_ENRICHED_PAGE_NODES,
        maximum_enriched_page_document_bytes=MAXIMUM_ENRICHED_PAGE_DOCUMENT_BYTES,
        event_page_size=PageLimit(event_page_size),
        maximum_control_queries=maximum_control_queries,
        maximum_event_poll_queries=maximum_event_poll_queries,
        maximum_query_admission_wait_milliseconds=(
            maximum_query_admission_wait_milliseconds
        ),
    )


def event_poll_backoff(
    *,
    initial_delay_seconds: float = INITIAL_EVENT_POLL_DELAY_SECONDS,
    maximum_delay_seconds: float = MAXIMUM_EVENT_POLL_DELAY_SECONDS,
    multiplier: float = EVENT_POLL_DELAY_MULTIPLIER,
) -> EventPollBackoff:
    """How this instance waits between polls, refused by its own owner.

    Every range rule already lives on `EventPollBackoff` -- positive start, a
    ceiling no lower than the start, a multiplier above one. Nothing is restated
    here: what was missing was never the refusal, only a way to reach the values.
    """

    return EventPollBackoff(
        initial_delay_seconds=initial_delay_seconds,
        maximum_delay_seconds=maximum_delay_seconds,
        multiplier=multiplier,
    )


@dataclass(frozen=True)
class GitHubEffectDeployment:
    """The live-GitHub `open-pr` destination this instance composes, by reference.

    ADR 0010 binds a project's repository scope and its credential reference to a
    durable project-connection record (#23/#24). That record is unbuilt, so this
    is a deliberately temporary single-operator, single-repository slice (`#430`):
    the operator names one credential directory and one repository at serve time,
    and it is superseded by the connection record rather than being a second owner
    of repository scope.

    The credential reaches the adapter by reference only (ADR 0009 §6): this holds
    the directory, and the token it names is read once when the adapter opens and
    lives nowhere durable. Because live GitHub cannot prove absence, composing this
    also refuses an agent-authored `open-pr` grant at admission (`#430`/`#431`).
    """

    credential_directory: Path
    repository: GitHubRepository

    def effect_adapter_factory(
        self, adapter_revision: AdapterRevision, destination: EffectDestination
    ) -> LiveGitHubEffectAdapterFactory:
        return LiveGitHubEffectAdapterFactory(
            adapter_revision,
            destination,
            self.repository,
            GitHubTokenCredential(self.credential_directory),
        )


@dataclass(frozen=True)
class HostSettings:
    database_path: Path
    effect_store_path: Path
    effect_adapter_revision: str
    effect_destination: str
    application_version: str
    source_commit: str
    source_tree: str
    frontend_dist: Path
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    limits: ApiLimits = field(default_factory=api_limits)
    # The two store- and process-side answers, beside the two API-side ones above.
    # Their range rules live on `DbosRuntimeSettings`, which is built from them.
    sqlite_lock_timeout_seconds: float = SQLITE_LOCK_TIMEOUT_SECONDS
    agent_termination_grace_seconds: float = AGENT_TERMINATION_GRACE_SECONDS
    event_poll_backoff: EventPollBackoff = field(default_factory=event_poll_backoff)
    agent_scratch_root: Path | None = None
    project_id: ProjectId | None = None
    project_root: Path | None = None
    claude_subscription: ClaudeSubscriptionSettings | None = None
    claude_workspace_tools: bool = False
    """Whether the Claude deployment also serves its tool-bearing executor.

    A separate answer from the deployment itself, because it is a separate
    grant: the tool-free executor is what a Claude deployment is, and the
    tool-bearing one lets a node's own process read, write and run commands as
    the serving user. An operator says yes to that once, here, and never as a
    side effect of naming an executable.
    """
    claude_start_refusal: str | None = None
    claude_workspace_tools_start_refusal: str | None = None
    grok_subscription: GrokSubscriptionSettings | None = None
    grok_workspace_tools: bool = False
    """Whether the Grok deployment also serves its tool-bearing executor.

    A separate answer from the deployment itself, because it is a separate
    grant: the tool-free executor is what a Grok deployment is, and the
    tool-bearing one lets a node's own process read, write and run commands as
    the serving user. An operator says yes to that once, here, and never as a
    side effect of naming an executable.
    """
    grok_start_refusal: str | None = None
    grok_workspace_tools_start_refusal: str | None = None
    codex_subscription: CodexSubscriptionSettings | None = None
    codex_start_refusal: str | None = None
    # The Runner-lease deployment (`#540` C-3.6): declared together or not at
    # all -- `DbosRuntimeSettings.__post_init__`, reached through
    # `runtime_settings()` below, owns that refusal so it stays one owner
    # rather than a second copy of the same rule here. `source_commit` above
    # doubles as the manifest's own provenance fact once this deployment is
    # declared; it is not repeated as a seventh flag.
    runner_lease_root: Path | None = None
    runner_image: str | None = None
    runner_image_digest: str | None = None
    runner_console_container: str | None = None
    runner_core_identity_directory: Path | None = None
    runner_accept_timeout_seconds: float | None = None
    # The cross-project attention feed's outbound delivery (`#433` phase 2):
    # declared all-or-nothing by `WebhookDeliverySettings`, which owns the URL
    # and the signing-key file path. `None` serves the API with no delivery
    # loop; a value starts the first lifespan background task in
    # `compose_application`. Not the project-scoped configuration channel
    # (`#425`), because the attention page is project-wide.
    webhook: WebhookDeliverySettings | None = None
    # The live-GitHub `open-pr` destination (`#430`), declared all-or-nothing by
    # `GitHubEffectDeployment` at the command line. `None` composes the loopback
    # effect adapter exactly as before; a value composes the live adapter for the
    # Action `open-pr` effect and, because it cannot prove absence, makes
    # admission refuse an agent-authored `open-pr` grant.
    github_effect: GitHubEffectDeployment | None = None

    @property
    def billed_providers(self) -> tuple[str, ...]:
        """Name every configured provider whose attempts spend a subscription."""

        configured = (
            ("Claude", self.claude_subscription),
            ("Grok", self.grok_subscription),
            ("Codex", self.codex_subscription),
        )
        return tuple(name for name, settings in configured if settings is not None)

    def runtime_settings(self) -> DbosRuntimeSettings:
        """The durable runtime's own answers, built by the record that holds them.

        Built rather than re-checked, and built here rather than deep inside the
        composition: the range rules live on `DbosRuntimeSettings`, and asking it
        early is what puts its refusal on the same path as every other one --
        where the command line can turn it into a named error instead of a
        traceback. Copying the rules up here would have been the other way, and
        the wrong one.
        """

        return DbosRuntimeSettings(
            self.database_path,
            self.application_version,
            agent_scratch_root=self.agent_scratch_root,
            project_id=self.project_id,
            bootstrap_project_root=self.project_root,
            agent_termination_grace_seconds=self.agent_termination_grace_seconds,
            sqlite_lock_timeout_seconds=self.sqlite_lock_timeout_seconds,
            runner_lease_root=self.runner_lease_root,
            runner_image=self.runner_image,
            runner_image_digest=self.runner_image_digest,
            runner_console_container=self.runner_console_container,
            runner_core_identity_directory=self.runner_core_identity_directory,
            runner_accept_timeout_seconds=self.runner_accept_timeout_seconds,
            runner_lease_source_commit=(
                self.source_commit if self.runner_lease_root is not None else None
            ),
        )

    def __post_init__(self) -> None:
        database_path = self.database_path.resolve()
        effect_store_path = self.effect_store_path.resolve()
        frontend_dist = self.frontend_dist.resolve()
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "effect_store_path", effect_store_path)
        object.__setattr__(self, "frontend_dist", frontend_dist)
        if self.runner_lease_root is not None:
            object.__setattr__(
                self, "runner_lease_root", self.runner_lease_root.resolve()
            )
        if self.runner_core_identity_directory is not None:
            object.__setattr__(
                self,
                "runner_core_identity_directory",
                self.runner_core_identity_directory.resolve(),
            )
        if database_path == effect_store_path:
            raise ValueError("durable database and effect store must be distinct")
        if self.project_root is not None and self.project_id is None:
            raise ValueError(
                "--project-root writes the host configuration channel, so it "
                "needs --project-id"
            )
        if self.project_id is not None and not isinstance(self.project_id, ProjectId):
            raise TypeError("project id must use its typed contract")
        for name in (
            "effect_adapter_revision",
            "effect_destination",
            "application_version",
            "source_commit",
            "source_tree",
            "host",
        ):
            if not getattr(self, name).strip():
                raise ValueError(f"{name} must be nonempty")
        if type(self.port) is not int or not 1 <= self.port <= 65_535:
            raise ValueError("port must be an integer between 1 and 65535")
        if (
            not (frontend_dist / "index.html").is_file()
            or not (frontend_dist / "assets").is_dir()
        ):
            raise ValueError("frontend distribution must contain index.html and assets")
        billed = self.billed_providers
        if billed and self.agent_scratch_root is None:
            raise ValueError(
                "serving a provider executor and declaring --agent-scratch-root "
                "go together: every attempt runs in a scratch workspace of its "
                "own, and a provider without a scratch root would share one "
                "directory across every attempt"
            )
        if self.claude_workspace_tools and self.claude_subscription is None:
            raise ValueError(
                "serving the Claude workspace-tool executor needs the Claude "
                "deployment it is a second executor of"
            )
        if self.grok_workspace_tools and self.grok_subscription is None:
            raise ValueError(
                "serving the Grok workspace-tool executor needs the Grok "
                "deployment it is a second executor of"
            )
        if self.agent_scratch_root is not None and not billed:
            raise ValueError(
                "a scratch root without a provider executor serves nothing"
            )
        if billed and not is_loopback_host(self.host):
            raise ValueError(
                f"serving {' and '.join(billed)} subscription agents requires a "
                f"loopback bind, not {self.host!r}: starting a billed provider is "
                "unauthenticated on this API, so the billed boundary stays on this "
                "machine until an authenticated boundary exists"
            )
        _require_start_refusal(
            "Claude", self.claude_subscription, self.claude_start_refusal
        )
        _require_start_refusal(
            "Claude workspace-tool",
            self.claude_subscription if self.claude_workspace_tools else None,
            self.claude_workspace_tools_start_refusal,
        )
        _require_start_refusal("Grok", self.grok_subscription, self.grok_start_refusal)
        _require_start_refusal(
            "Grok workspace-tool",
            self.grok_subscription if self.grok_workspace_tools else None,
            self.grok_workspace_tools_start_refusal,
        )
        _require_start_refusal(
            "Codex", self.codex_subscription, self.codex_start_refusal
        )
        # Asked last, once every path this record resolves is settled. Its
        # refusals belong to the durable runtime and are raised here so they
        # travel the same way as the ones above -- the command line catches this
        # constructor, and nothing below it.
        self.runtime_settings()


def _require_start_refusal(
    name: str, declared: object | None, refusal: str | None
) -> None:
    if refusal is None:
        return
    if declared is None:
        raise ValueError(f"a {name} start refusal names a declared {name} deployment")
    if not refusal.strip():
        raise ValueError(f"a {name} start refusal must be nonempty")


def _subscription_executor_registrations(
    settings: HostSettings,
) -> tuple[AgentExecutorRegistration, ...]:
    claude_subscription = settings.claude_subscription
    grok_subscription = settings.grok_subscription
    codex_subscription = settings.codex_subscription
    return (
        *(
            (
                _subscription_registration(
                    ClaudeSubscriptionExecutorFactory(claude_subscription),
                    settings.claude_start_refusal is not None,
                ),
            )
            if claude_subscription is not None
            else ()
        ),
        *(
            (
                _subscription_registration(
                    ClaudeWorkspaceToolExecutorFactory(claude_subscription),
                    settings.claude_start_refusal is not None
                    or settings.claude_workspace_tools_start_refusal is not None,
                ),
            )
            if (claude_subscription is not None and settings.claude_workspace_tools)
            else ()
        ),
        *(
            (
                _subscription_registration(
                    GrokSubscriptionExecutorFactory(grok_subscription),
                    settings.grok_start_refusal is not None,
                ),
            )
            if grok_subscription is not None
            else ()
        ),
        *(
            (
                _subscription_registration(
                    GrokWorkspaceToolExecutorFactory(grok_subscription),
                    settings.grok_start_refusal is not None
                    or settings.grok_workspace_tools_start_refusal is not None,
                ),
            )
            if (grok_subscription is not None and settings.grok_workspace_tools)
            else ()
        ),
        *(
            (
                _subscription_registration(
                    CodexSubscriptionExecutorFactory(codex_subscription),
                    settings.codex_start_refusal is not None,
                ),
            )
            if codex_subscription is not None
            else ()
        ),
    )


def _subscription_registration(
    factory: AgentExecutorFactoryV2, unavailable: bool
) -> AgentExecutorRegistration:
    if unavailable:
        return AgentExecutorRegistration.unavailable(factory)
    return AgentExecutorRegistration.startable(factory)


def _runner_lease_executor_registrations(
    settings: HostSettings,
) -> tuple[AgentExecutorRegistration, ...]:
    """The fake-free candidate as this deployment's one `RUNNER_LEASE` offer.

    `#540` C-3.6's slice: only the fixed fake-free candidate is served this
    way, and only once the whole Runner-lease deployment is declared
    (`DbosRuntimeSettings.__post_init__`, reached through
    `runtime_settings()`, refuses a partial declaration by name). Real
    providers over a Runner lease wait on `#15` and B-3.
    """

    if settings.runner_lease_root is None:
        return ()
    return (
        AgentExecutorRegistration.startable(
            FreeRunnerExecutorFactory(), AgentExecutorCarrier.RUNNER_LEASE
        ),
    )


def _log_unstartable_executors(settings: HostSettings) -> None:
    logger = logging.getLogger("atelier2")
    seen: set[str] = set()
    for refusal in (
        settings.claude_start_refusal,
        settings.claude_workspace_tools_start_refusal,
        settings.grok_start_refusal,
        settings.grok_workspace_tools_start_refusal,
        settings.codex_start_refusal,
    ):
        if refusal is not None and refusal not in seen:
            seen.add(refusal)
            logger.warning(refusal)


class LiveGitHubOpenPrRunPending(RuntimeError):
    """A V3 run could still redeem an agent `open-pr` grant against live GitHub.

    Admission refuses a new agent-authored `open-pr` run against an adapter that
    cannot prove absence (`#430`/`#431`), but that door only guards the runs a
    live-GitHub instance itself admits. A run admitted earlier under the
    absence-proving loopback adapter can still owe its redemption when the
    operator restarts the same database with the live adapter; DBOS recovery
    would then resume its durable redemption node against live GitHub, whose
    not-found readback is `EffectUnknownOutcome`, and the run would end ERROR
    after it had already committed COMPLETED. The run may be non-terminal, or it
    may have committed COMPLETED and crashed before the sink node's own workflow
    finished the redemption -- either way the redemption is still owed. Composing
    the live adapter therefore refuses to start while any such run remains, so the
    operator lets it finish or cancels it before serving live GitHub.
    """


def _refuse_pending_agent_open_pr_runs(runtime: DbosRuntime) -> None:
    """Fail the live-GitHub start while any V3 run still owes an agent open-pr PR.

    The dbos adapter owns the scan; this only turns a nonempty answer into the
    loud startup refusal, naming the runs the operator must let finish or cancel.
    """

    blocking = agent_open_pr_runs_pending_live_redemption(runtime.engine)
    if blocking:
        named = ", ".join(sorted(run.value for run in blocking))
        raise LiveGitHubOpenPrRunPending(
            "refusing to serve live GitHub open-pr while these runs still carry an "
            f"agent-authored open-pr grant and have not finished: {named}. Let them "
            "finish or cancel them before serving with --github-*."
        )


def _effect_adapter_factory(settings: HostSettings) -> EffectAdapterFactory:
    """The one effect adapter this instance drives.

    The live-GitHub destination composes only when the operator declared it; with
    no declaration this is the loopback adapter exactly as before. The token, when
    the live adapter opens it, is read from the credential directory by reference
    and never returns here (ADR 0009 §6).

    Whether the composed adapter proves absence is read back from the runtime that
    binds it (`runtime.effect_adapter_proves_absence`), so the runtime is the one
    owner of that answer rather than a second reading here that must agree.
    """

    adapter_revision = AdapterRevision(settings.effect_adapter_revision)
    destination = EffectDestination(settings.effect_destination)
    if settings.github_effect is not None:
        return settings.github_effect.effect_adapter_factory(
            adapter_revision, destination
        )
    return LoopbackEffectAdapterFactory(
        settings.effect_store_path, adapter_revision, destination
    )


def compose_application(settings: HostSettings) -> tuple[FastAPI, DbosRuntime]:
    subscription_executors = (
        *_subscription_executor_registrations(settings),
        *_runner_lease_executor_registrations(settings),
    )
    runtime = DbosRuntime(
        settings.runtime_settings(),
        _effect_adapter_factory(settings),
        ExactOutputAgentExecutorFactory(),
        subscription_executors,
    )
    # A destination that cannot prove absence (live GitHub) makes an agent-authored
    # `open-pr` grant unreconcilable, so admission refuses it (`#430`/`#431`). The
    # runtime composed the adapter, so it owns this answer for every start path.
    effect_adapter_proves_absence = runtime.effect_adapter_proves_absence
    try:
        # Before the launch that recovers durable nodes: a live adapter cannot
        # prove absence, so a non-terminal V3 run whose agent node opens its own
        # pull request must not be resumed against it (`#430`/`#431`). Admission
        # already refuses such a run at start, but a cross-restart adapter swap can
        # leave one admitted earlier under loopback, so this refuses the whole
        # start rather than recovering it into a COMPLETED-lie.
        if not effect_adapter_proves_absence:
            _refuse_pending_agent_open_pr_runs(runtime)
        # One expression feeds both the reader's bound and the API's own, so the
        # promise that they cannot describe different numbers holds by
        # construction rather than by two readings agreeing today.
        limits = settings.limits
        queries = DbosQueries(runtime.engine, durable_projection_limit(limits))
        webhook = settings.webhook
        if webhook is not None:
            # The signing key is read once, here, and lives only in the loop
            # that holds it (ADR 0009 §6). A key file that will not resolve
            # fails the whole start rather than serving with delivery quietly
            # off.
            signing_key = resolve_signing_key(webhook.signing_key_path)
            transport = open_webhook_transport(webhook.target_url)
            delivery_loop = WebhookDeliveryLoop(
                DbosWebhookDeliveryPublisher(runtime.engine),
                queries,
                transport,
                signing_key,
            )
            lifespan = webhook_delivery_lifespan(delivery_loop, transport)
        else:
            lifespan = None
        app = create_app(
            source_commit=settings.source_commit,
            source_tree=settings.source_tree,
            lifespan=lifespan,
            ports=ApiPorts(
                workflow_revision_publisher=DbosWorkflowRevisionPublisher(
                    runtime.engine
                ),
                published_run_starter=DbosDurableRunStarter(
                    runtime.engine,
                    runtime.settings,
                    runtime.agent_executor_registry,
                    effect_adapter_proves_absence,
                ),
                wait_answerer=DbosWaitAnswerer(
                    runtime.engine, runtime.settings.application_version
                ),
                reconcile_commander=DbosEffectReconcileCommander(
                    runtime.engine, runtime.settings
                ),
                workflow_revision_queries=queries,
                run_queries=queries,
                run_event_queries=queries,
                workflow_document_parser=parse_workflow_document,
                agent_configuration_catalog=DbosAgentConfigurationCatalog(
                    runtime.engine, runtime.agent_executor_registry
                ),
                agent_attempt_canceller=DbosAgentAttemptStore(
                    runtime.engine, runtime.settings.application_version
                ),
                catalog_resolver=DbosCatalogStore(runtime.engine),
                catalog_admissions=DbosCatalogStore(runtime.engine),
                published_revision_registry=DbosCatalogStore(runtime.engine),
                artifact_publisher=DbosArtifactStore(runtime.engine),
                host_configuration_channel=DbosHostConfigurationChannel(runtime.engine),
                queue_projection=DbosQueueProjectionStore(runtime.engine),
            ),
            limits=limits,
            event_poll_backoff=settings.event_poll_backoff,
            frontend_dist=settings.frontend_dist,
            served_project_id=settings.project_id,
        )
        runtime.launch()
        return app, runtime
    except BaseException:
        runtime.close()
        raise


def serve(settings: HostSettings) -> None:
    configure_process_logging()
    _log_unstartable_executors(settings)
    app, runtime = compose_application(settings)
    try:
        uvicorn.Server(
            uvicorn.Config(
                app,
                host=settings.host,
                port=settings.port,
                log_config=None,
                access_log=False,
            )
        ).run()
    finally:
        runtime.close()
