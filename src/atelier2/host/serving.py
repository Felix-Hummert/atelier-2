from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path

import uvicorn
from fastapi import FastAPI

from atelier2.adapters.claude_subscription import (
    ClaudeSubscriptionExecutorFactory,
    ClaudeSubscriptionSettings,
)
from atelier2.adapters.codex_subscription import (
    CodexSubscriptionExecutorFactory,
    CodexSubscriptionSettings,
)
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
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
from atelier2.host.address import DEFAULT_HOST, DEFAULT_PORT

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
        event_page_size=event_page_size,
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
    claude_subscription: ClaudeSubscriptionSettings | None = None
    grok_subscription: GrokSubscriptionSettings | None = None
    codex_subscription: CodexSubscriptionSettings | None = None

    @property
    def billed_providers(self) -> tuple[str, ...]:
        """Name every configured provider whose attempts spend a subscription."""

        configured = (
            ("Claude", self.claude_subscription),
            ("Grok", self.grok_subscription),
            ("Codex", self.codex_subscription),
        )
        return tuple(name for name, settings in configured if settings is not None)

    def __post_init__(self) -> None:
        database_path = self.database_path.resolve()
        effect_store_path = self.effect_store_path.resolve()
        frontend_dist = self.frontend_dist.resolve()
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "effect_store_path", effect_store_path)
        object.__setattr__(self, "frontend_dist", frontend_dist)
        if database_path == effect_store_path:
            raise ValueError("durable database and effect store must be distinct")
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


def _is_loopback(host: str) -> bool:
    """Only a literal loopback address proves the bind stays on this machine.

    A name resolves elsewhere at bind time, so a host this function cannot read
    as an address is refused rather than trusted.
    """

    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def compose_application(settings: HostSettings) -> tuple[FastAPI, DbosRuntime]:
    claude_subscription = settings.claude_subscription
    grok_subscription = settings.grok_subscription
    codex_subscription = settings.codex_subscription
    subscription_executors = (
        *(
            ()
            if claude_subscription is None
            else (ClaudeSubscriptionExecutorFactory(claude_subscription),)
        ),
        *(
            ()
            if grok_subscription is None
            else (GrokSubscriptionExecutorFactory(grok_subscription),)
        ),
        *(
            ()
            if codex_subscription is None
            else (CodexSubscriptionExecutorFactory(codex_subscription),)
        ),
    )
    runtime = DbosRuntime(
        DbosRuntimeSettings(
            settings.database_path,
            settings.application_version,
            agent_scratch_root=settings.agent_scratch_root,
            agent_termination_grace_seconds=settings.agent_termination_grace_seconds,
            sqlite_lock_timeout_seconds=settings.sqlite_lock_timeout_seconds,
        ),
        LoopbackEffectAdapterFactory(
            settings.effect_store_path,
            AdapterRevision(settings.effect_adapter_revision),
            EffectDestination(settings.effect_destination),
        ),
        ExactOutputAgentExecutorFactory(),
        subscription_executors,
    )
    try:
        # One set of limits configures both the reader's bound and the API's own,
        # so the two cannot describe different numbers for the same deployment.
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
            ),
            limits=settings.limits,
            event_poll_backoff=settings.event_poll_backoff,
            frontend_dist=settings.frontend_dist,
        )
        runtime.launch()
        return app, runtime
    except BaseException:
        runtime.close()
        raise


def serve(settings: HostSettings) -> None:
    app, runtime = compose_application(settings)
    try:
        uvicorn.Server(
            uvicorn.Config(app, host=settings.host, port=settings.port)
        ).run()
    finally:
        runtime.close()
