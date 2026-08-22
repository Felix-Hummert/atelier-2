from __future__ import annotations

import ipaddress
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
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.artifact_store import DbosArtifactStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.queries import DbosQueries
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
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.grok_subscription import (
    GrokSubscriptionExecutorFactory,
    GrokSubscriptionSettings,
    GrokWorkspaceToolExecutorFactory,
)
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
from atelier2.host.address import DEFAULT_HOST, DEFAULT_PORT
from atelier2.host.logging import configure_process_logging
from atelier2.ports.agent_executions import (
    AgentExecutorFactoryV2,
    AgentExecutorRegistration,
)

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
        )

    def __post_init__(self) -> None:
        database_path = self.database_path.resolve()
        effect_store_path = self.effect_store_path.resolve()
        frontend_dist = self.frontend_dist.resolve()
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "effect_store_path", effect_store_path)
        object.__setattr__(self, "frontend_dist", frontend_dist)
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
        if billed and not _is_loopback(self.host):
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


def _is_loopback(host: str) -> bool:
    """Only a literal loopback address proves the bind stays on this machine.

    A name resolves elsewhere at bind time, so a host this function cannot read
    as an address is refused rather than trusted.
    """

    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


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


def compose_application(settings: HostSettings) -> tuple[FastAPI, DbosRuntime]:
    subscription_executors = _subscription_executor_registrations(settings)
    runtime = DbosRuntime(
        settings.runtime_settings(),
        LoopbackEffectAdapterFactory(
            settings.effect_store_path,
            AdapterRevision(settings.effect_adapter_revision),
            EffectDestination(settings.effect_destination),
        ),
        ExactOutputAgentExecutorFactory(),
        subscription_executors,
    )
    try:
        # One expression feeds both the reader's bound and the API's own, so the
        # promise that they cannot describe different numbers holds by
        # construction rather than by two readings agreeing today.
        limits = settings.limits
        queries = DbosQueries(runtime.engine, durable_projection_limit(limits))
        app = create_app(
            source_commit=settings.source_commit,
            source_tree=settings.source_tree,
            ports=ApiPorts(
                workflow_revision_publisher=DbosWorkflowRevisionPublisher(
                    runtime.engine
                ),
                published_run_starter=DbosDurableRunStarter(
                    runtime.engine,
                    runtime.settings,
                    runtime.agent_executor_registry,
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
