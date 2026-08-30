from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, Never

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.artifact_store import DbosArtifactStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.markdown_agent_definitions import (
    parse_agent_definition,
    render_agent_definition,
)
from atelier2.adapters.yaml_workflows import (
    parse_executable_workflow_document,
    parse_workflow_document,
)
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.limits import ApiLimits
from atelier2.api.openapi import API_PREFIX
from atelier2.api.stream import EventPollBackoff
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits
from atelier2.application.read_run_events import ReadRunEventsResult, read_run_events
from atelier2.contracts.agents import (
    AgentBindingSet,
    AgentConfigurationRevision,
    AuthProfileRevision,
)
from atelier2.contracts.host_configuration import ProjectId, ProviderModelCheck
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.run_projections import (
    RunProjection,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.host_configuration import (
    ProviderModelDiscovery,
    ProviderModelDiscoveryResult,
    ProviderModelInspector,
    ProviderModelValidationResult,
)
from atelier2.ports.issue_observation import TrackerItemSource
from atelier2.ports.run_events import (
    RunEventQueries,
)
from atelier2.ports.run_queries import (
    RunFound,
)
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
)

RECONCILIATION_REVISION_HASH = (
    "c93767cc7790bdb39258bb6d9bdfb3168218705038932119e6628c6312c6e34e"
)
RECONCILIATION_AGENT_OUTPUT_HASH = (
    "1f58b9145b24d108d7ac38887338b3ea3229833b9c1e418250343f907bfd1047"
)
RECONCILIATION_REQUEST = (
    b'{"body":"request","head_branch":"atelier2-open-pr-1f58b9145b24"}'
)
RECONCILIATION_REQUEST_HASH = (
    "d4d42b951a6678c6a14de919e1e7a3b661ff986cf11dd019d664b78fcd9b972d"
)
RECONCILIATION_APPLIED_RESULT_HASH = (
    "fbf5b216105e471c4f89e92a1ec12897ee9f2b439eb200a4f7855901d2889e7e"
)
RECONCILIATION_LOGICAL_KEY = "atelier2-node-effect-a7fc7d5652a8af5fa287dcc8e8896abf3db9360cdfb4cd65d4e7ac272df9a956"
SSE_PUBLIC_RUN_REFERENCE = "run1.c3NlL3J1bg"
SSE_CURSOR_AFTER_THREE = "event1.c3NlL3J1bg.3"
SSE_COMPLETE_HISTORY: list[dict[str, object]] = [
    {
        "id": "event1.c3NlL3J1bg.1",
        "data": {
            "cursor": "event1.c3NlL3J1bg.1",
            "sequence": 1,
            "public_run_reference": "run1.c3NlL3J1bg",
            "workflow_revision_hash": RECONCILIATION_REVISION_HASH,
            "node_id": "agent",
            "node_execution_id": (
                "915723ef58999337490999ccff3eb761d002076102834f782b2af3406057e586"
            ),
            "event_hash": (
                "51ea73aa18b3698282e9e1b46f9c05ecd027fa32aa39f14e287028e8690fafb6"
            ),
            "event": "AGENT_COMPLETED",
            "output": "request",
            "payload_hash": RECONCILIATION_AGENT_OUTPUT_HASH,
        },
    },
    {
        "id": "event1.c3NlL3J1bg.2",
        "data": {
            "cursor": "event1.c3NlL3J1bg.2",
            "sequence": 2,
            "public_run_reference": "run1.c3NlL3J1bg",
            "workflow_revision_hash": RECONCILIATION_REVISION_HASH,
            "node_id": "action",
            "node_execution_id": (
                "c834c9c196ada921f7bb8f03e1a5d11db01b87b68186e043e424995a4c608840"
            ),
            "event_hash": (
                "82925553c1a9595672797f0efc4c8bc0bda9fe14f100d3b82cbc5ee6aa8029f2"
            ),
            "event": "ACTION_RECONCILIATION_REQUIRED",
            "request_base64": (
                "eyJib2R5IjoicmVxdWVzdCIsImhlYWRfYnJhbmNoIjoiYXRlbGllcjItb3Blbi1wci0x"
                "ZjU4YjkxNDViMjQifQ=="
            ),
            "request_hash": RECONCILIATION_REQUEST_HASH,
        },
    },
    {
        "id": "event1.c3NlL3J1bg.3",
        "data": {
            "cursor": "event1.c3NlL3J1bg.3",
            "sequence": 3,
            "public_run_reference": "run1.c3NlL3J1bg",
            "workflow_revision_hash": RECONCILIATION_REVISION_HASH,
            "node_id": "action",
            "node_execution_id": (
                "c834c9c196ada921f7bb8f03e1a5d11db01b87b68186e043e424995a4c608840"
            ),
            "event_hash": (
                "b72a3badaaea21599788fd0d0c471cfb674ff992720aeb30eb5cca8461634f2c"
            ),
            "event": "ACTION_RECONCILIATION_RESOLVED",
            "receipt": {
                "logical_effect_key": (
                    "atelier2-node-effect-"
                    "cdf8eea48e67b5278f63f3cd403c41e3d4ac042bfb226f994f7284d66f785292"
                ),
                "request_hash": RECONCILIATION_REQUEST_HASH,
                "effect_id": "effect",
                "result_hash": (
                    "f6a214f7a5fcda0c2cee9660b7fc29f5649e3c68aad48e20e950137c98913a68"
                ),
                "result_base64": "cmVzdWx0",
                "confirmation_source": "OPERATOR_FOUND",
                "reconcile_command_id": "command",
            },
        },
    },
    {
        "id": "event1.c3NlL3J1bg.4",
        "data": {
            "cursor": "event1.c3NlL3J1bg.4",
            "sequence": 4,
            "public_run_reference": "run1.c3NlL3J1bg",
            "workflow_revision_hash": RECONCILIATION_REVISION_HASH,
            "node_id": "action",
            "node_execution_id": (
                "c834c9c196ada921f7bb8f03e1a5d11db01b87b68186e043e424995a4c608840"
            ),
            "event_hash": (
                "3fa72101e27035a8387ce3fbb7c17a61095552e9f0d66d16e72107bf9743743f"
            ),
            "event": "ACTION_COMPLETED",
            "receipt": {
                "logical_effect_key": (
                    "atelier2-node-effect-"
                    "cdf8eea48e67b5278f63f3cd403c41e3d4ac042bfb226f994f7284d66f785292"
                ),
                "request_hash": RECONCILIATION_REQUEST_HASH,
                "effect_id": "effect",
                "result_hash": (
                    "f6a214f7a5fcda0c2cee9660b7fc29f5649e3c68aad48e20e950137c98913a68"
                ),
                "result_base64": "cmVzdWx0",
                "confirmation_source": "OPERATOR_FOUND",
                "reconcile_command_id": "command",
            },
        },
    },
    {
        "id": "event1.c3NlL3J1bg.5",
        "data": {
            "cursor": "event1.c3NlL3J1bg.5",
            "sequence": 5,
            "public_run_reference": "run1.c3NlL3J1bg",
            "workflow_revision_hash": RECONCILIATION_REVISION_HASH,
            "node_id": "wait",
            "node_execution_id": (
                "ecaab546cc7bafe653997f74b20de1eb744e25fa4b5a3fc47c274a6e11950df9"
            ),
            "event_hash": (
                "5f095d226b3b24eb4d1cd3097e29236471c124a2157fc2319674a3aa208735c6"
            ),
            "event": "WAITING_INPUT",
            "answer_type": "integer",
        },
    },
    {
        "id": "event1.c3NlL3J1bg.6",
        "data": {
            "cursor": "event1.c3NlL3J1bg.6",
            "sequence": 6,
            "public_run_reference": "run1.c3NlL3J1bg",
            "workflow_revision_hash": RECONCILIATION_REVISION_HASH,
            "node_id": "wait",
            "node_execution_id": (
                "ecaab546cc7bafe653997f74b20de1eb744e25fa4b5a3fc47c274a6e11950df9"
            ),
            "event_hash": (
                "836c19cd3943a6707d0a19b8bbcdeb147c049964c0eb5db58802d9d4f9d72ac0"
            ),
            "event": "WAIT_ANSWERED",
            "answer": "17",
            "answer_hash": (
                "4523540f1504cd17100c4835e85b7eefd49911580f8efff0599a8f283be6b9e3"
            ),
        },
    },
    {
        "id": "event1.c3NlL3J1bg.7",
        "data": {
            "cursor": "event1.c3NlL3J1bg.7",
            "sequence": 7,
            "public_run_reference": "run1.c3NlL3J1bg",
            "workflow_revision_hash": RECONCILIATION_REVISION_HASH,
            "node_id": "final",
            "node_execution_id": (
                "52dccbde016bd02dd99e78d058ce5cc87339b73137f636ac88ded5e657312269"
            ),
            "event_hash": (
                "122bade68295c2d129edb4224b2887b36fff42e524d54a46916cd067e5625b9c"
            ),
            "event": "SUBWORKFLOW_COMPLETED",
            "result": 5,
            "result_hash": (
                "ef2d127de37b942baad06145e54b0c619a1f22327b2ebbcfbec78f5564afe39d"
            ),
        },
    },
]


class UnusedPort:
    """A port the route under test must not reach."""

    def __getattr__(self, name: str) -> Never:
        raise AssertionError(f"the route under test reached the {name} port")


def unused_attention_event_page(
    after_run_id: object,
    after_sequence: object,
    limit: object,
    excluded_identities: object = (),
) -> Never:
    del after_run_id, after_sequence, limit, excluded_identities
    raise AssertionError("attention feed was not under test")


STREAM_DOCUMENT = b"""format_version: 3
name: One agent, streamed
nodes:
  - id: agent
    type: agent
    role: builder
    mode: headless
    instruction: Produce the payload this stream carries.
    outputs:
      - name: payload
        schema: {ref: workspace_candidate, revision: schema-candidate}
"""


def stream_run_projection(run_id: str) -> RunProjection:
    """The run an SSE scenario streams, so every frame can say where it stands.

    The event endpoint reads the run once and folds the streamed events onto it.
    A stream scenario therefore needs a run, not only an event store, and this is
    the one place that decides which run that is.
    """

    revision = WorkflowRevision(STREAM_DOCUMENT)
    return RunProjection(
        RunV3(
            RunId(run_id),
            revision.revision_hash,
            AgentBindingSet(()).binding_set_hash,
            (),
            RunState.STARTED,
            "agent",
            0,
            0,
            RunConfigurationRevisionHash("c" * 64),
        ),
        parse_executable_workflow_document(STREAM_DOCUMENT),
        None,
    )


class OneRunQueries:
    """A run store that answers with one projection, whichever run is asked for."""

    def __init__(self, projection: RunProjection) -> None:
        self._projection = projection

    def get_run(self, run_id: object, projection_limit: object = None) -> RunFound:
        del run_id, projection_limit
        return RunFound(self._projection)


class ExactConfiguredModelInspector:
    """The deterministic provider boundary used by API integration scenarios."""

    def discover_models(
        self,
        configuration: AgentConfigurationRevision,
        auth_profile: AuthProfileRevision,
    ) -> ProviderModelDiscoveryResult:
        del auth_profile
        return ProviderModelDiscovery(frozenset({configuration.model}))

    def validate_model(
        self,
        configuration: AgentConfigurationRevision,
        auth_profile: AuthProfileRevision,
    ) -> ProviderModelValidationResult:
        del configuration, auth_profile
        return ProviderModelCheck.CHECKED


def api_ports(**overrides: object) -> ApiPorts:
    """The full port set with only the ports a test names actually wired."""
    unused = UnusedPort()
    ports: dict[str, Any] = {
        "workflow_revision_publisher": unused,
        "published_run_starter": unused,
        "wait_answerer": unused,
        "reconcile_commander": unused,
        "workflow_revision_queries": unused,
        "run_queries": unused,
        "run_event_queries": unused,
        "workflow_document_parser": parse_workflow_document,
        "agent_definition_parser": parse_agent_definition,
        "agent_definition_renderer": render_agent_definition,
        "agent_configuration_catalog": unused,
        "agent_attempt_canceller": unused,
        "catalog_resolver": unused,
        "catalog_admissions": unused,
        "library_additions": unused,
        "catalog_intakes": unused,
        "published_revision_registry": unused,
        "published_revision_listing": unused,
        "artifact_publisher": unused,
        "host_configuration_channel": unused,
        "project_source_connection_channel": unused,
        "project_source_connector": unused,
        "project_source_credential_store": unused,
        "queue_projection": unused,
        "model_registry_inspector": ExactConfiguredModelInspector(),
    }
    ports.update(overrides)
    return ApiPorts(**ports)


def durable_ports(
    engine: Engine,
    settings: DbosRuntimeSettings,
    agent_executor_registry: AgentExecutorRegistry,
    queries: DbosQueries | None = None,
    **overrides: object,
) -> ApiPorts:
    """The real DBOS-backed port set every durable test composes identically.

    This is the one production shape `atelier2.host.serving` wires, built for a
    test engine instead. `queries` is the one port call sites genuinely
    disagree on -- some share a plain reader across all three query ports,
    one bounds it to a publication limit -- so it is the one named parameter;
    anything else a caller passes overrides the matching field verbatim.
    """
    resolved_queries = queries if queries is not None else durable_queries(engine)
    catalog = DbosCatalogStore(engine)
    unused = UnusedPort()
    ports: dict[str, Any] = {
        "workflow_revision_publisher": DbosWorkflowRevisionPublisher(engine),
        "published_run_starter": DbosDurableRunStarter(
            engine,
            settings,
            agent_executor_registry,
        ),
        "wait_answerer": DbosWaitAnswerer(engine, settings.application_version),
        "reconcile_commander": DbosEffectReconcileCommander(engine, settings),
        "workflow_revision_queries": resolved_queries,
        "run_queries": resolved_queries,
        "run_event_queries": resolved_queries,
        "workflow_document_parser": parse_workflow_document,
        "agent_definition_parser": parse_agent_definition,
        "agent_definition_renderer": render_agent_definition,
        "agent_configuration_catalog": DbosAgentConfigurationCatalog(
            engine, agent_executor_registry
        ),
        "agent_attempt_canceller": DbosAgentAttemptStore(
            engine, settings.application_version
        ),
        "catalog_resolver": catalog,
        "catalog_admissions": catalog,
        "library_additions": catalog,
        "catalog_intakes": catalog,
        "published_revision_registry": catalog,
        "published_revision_listing": catalog,
        "artifact_publisher": DbosArtifactStore(engine),
        "host_configuration_channel": DbosHostConfigurationChannel(engine),
        "project_source_connection_channel": DbosHostConfigurationChannel(engine),
        "project_source_connector": unused,
        "project_source_credential_store": unused,
        "queue_projection": DbosQueueProjectionStore(engine),
        "model_registry_inspector": ExactConfiguredModelInspector(),
    }
    ports.update(overrides)
    return ApiPorts(**ports)


def api_limits(**changes: int) -> ApiLimits:
    event_page_size = changes.pop("event_page_size", 50)
    configured = ApiLimits(
        maximum_request_body_bytes=65_536,
        maximum_field_characters=1_024,
        maximum_base64_characters=65_536,
        maximum_decoded_payload_bytes=49_152,
        maximum_workflow_nodes=100,
        maximum_enriched_page_nodes=100,
        maximum_enriched_page_document_bytes=65_536,
        event_page_size=PageLimit(event_page_size),
        maximum_control_queries=8,
        maximum_event_poll_queries=2,
        maximum_query_admission_wait_milliseconds=1_000,
    )
    return replace(configured, **changes)


def event_poll_backoff() -> EventPollBackoff:
    return EventPollBackoff(0.01, 0.25, 2)


def event_stream_client(queries: RunEventQueries) -> TestClient:
    """The composed HTTP boundary in front of one event store and nothing else.

    What the event endpoint answers is a property of the composed server, so a
    test that claims an HTTP status, a problem body, or stream bytes asks for
    it here instead of calling the generator by hand.
    """

    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                run_event_queries=queries,
                run_queries=OneRunQueries(stream_run_projection("sse/run")),
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def described_api_client() -> TestClient:
    """The composed HTTP boundary in front of no store at all.

    What the API says about itself -- its document, and the refusal that names
    where the document is -- is answered before any port is reached, so a test
    that reads the description wires none.
    """

    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def named_document_path(refusal_detail: str) -> str:
    """The one path a refusal names, read out of the sentence that names it."""

    named = [
        word.rstrip(".") for word in refusal_detail.split() if word.startswith("/")
    ]
    return named[-1]


def discovered_openapi_document(client: TestClient, guessed_path: str) -> Any:
    """The description found the way a consumer must find it: knock, then read.

    A first contact holds a base URL and nothing else. The path it guesses is
    refused, the refusal names where the description lives, and that is the only
    route by which a test here learns the document's own address.
    """

    refusal = client.get(guessed_path)
    return client.get(named_document_path(refusal.json()["detail"])).json()


def published_workflow_grammar_reference(openapi_document: Any) -> Any:
    """What the publication body names as the document it takes.

    Followed the way a consumer has to follow it -- publication path, its one
    declared body, the reference that body names -- so nothing a test knows
    about the shape came from the repository the API is served from.
    """

    body = openapi_document["paths"][API_PREFIX + "/workflow-revisions"]["post"][
        "requestBody"
    ]
    (declared,) = body["content"].values()
    return declared["schema"]


def openapi_component(openapi_document: Any, reference: Any) -> Any:
    """One component of a served document, resolved through the reference to it."""

    name = reference["$ref"].rsplit("/", 1)[1]
    return openapi_document["components"]["schemas"][name]


def published_workflow_grammar(openapi_document: Any) -> Draft202012Validator:
    """The published shape, assembled into the check a consumer would run."""

    return Draft202012Validator(
        {**openapi_document, **published_workflow_grammar_reference(openapi_document)}
    )


def durable_asgi_app(
    runtime: DbosRuntime,
    limits: ApiLimits | None = None,
    poll_backoff: EventPollBackoff | None = None,
    served_project_id: ProjectId | None = None,
    tracker_item_source: TrackerItemSource | None = None,
    model_registry_inspector: ProviderModelInspector | None = None,
) -> FastAPI:
    """The ASGI app in front of one real durable runtime.

    `durable_api_client` wraps this for a single caller. A concurrent harness
    needs the app itself so one event loop can drive many requests.
    """

    return create_app(
        source_commit="commit",
        source_tree="tree",
        ports=durable_ports(
            runtime.engine,
            runtime.settings,
            runtime.agent_executor_registry,
            tracker_item_source=tracker_item_source,
            model_registry_inspector=(
                ExactConfiguredModelInspector()
                if model_registry_inspector is None
                else model_registry_inspector
            ),
        ),
        limits=api_limits() if limits is None else limits,
        event_poll_backoff=event_poll_backoff()
        if poll_backoff is None
        else poll_backoff,
        served_project_id=served_project_id,
    )


def durable_api_client(
    runtime: DbosRuntime,
    limits: ApiLimits | None = None,
    served_project_id: ProjectId | None = None,
    tracker_item_source: TrackerItemSource | None = None,
    model_registry_inspector: ProviderModelInspector | None = None,
) -> TestClient:
    """The real HTTP boundary in front of one real durable runtime.

    Whether a request is refused before anything durable exists is a property
    of the composed server, not of a starter called by hand, so a test that
    claims an HTTP answer asks for it here.
    """

    return TestClient(
        durable_asgi_app(
            runtime,
            limits,
            served_project_id=served_project_id,
            tracker_item_source=tracker_item_source,
            model_registry_inspector=model_registry_inspector,
        )
    )


def stream_projection_limit() -> WorkflowPublicationLimits:
    """A limit wide enough not to be what a stream test is about."""
    return WorkflowPublicationLimits(
        maximum_document_bytes=65_536,
        maximum_nodes=100,
        maximum_string_characters=1_024,
        maximum_payload_bytes=49_152,
    )


def stream_page_reader(
    queries: RunEventQueries,
    projection_limit: DurableProjectionLimit | None = None,
) -> Callable[[RunId, int, int], ReadRunEventsResult]:
    """The page decision a stream drives, bound to one store.

    Tests hand this to `stream_server_events` rather than the store itself, so the
    real `read_run_events` decision stays on the path they exercise instead of
    being stepped over by a fake shaped like the port. The limit is a real one
    rather than `None`: the use-case takes no fail-open default, which is the
    direction head D is moving the ports in anyway.
    """

    def read_page(
        run_id: RunId, after_sequence: int, page_size: int
    ) -> ReadRunEventsResult:
        return read_run_events(run_id, after_sequence, page_size, queries)

    return read_page


def permissive_projection_limit() -> WorkflowPublicationLimits:
    """A bound wide enough not to be what a test is about."""
    return WorkflowPublicationLimits(
        maximum_document_bytes=1_000_000,
        maximum_nodes=1_000,
        maximum_string_characters=100_000,
        maximum_payload_bytes=1_000_000,
    )


def durable_queries(
    engine: Engine, projection_limit: WorkflowPublicationLimits | None = None
) -> DbosQueries:
    """A durable reader holding a bound, because every reader now holds one.

    A test that is not about the bound takes a permissive one; a test that is
    about it passes its own. Neither can build a reader without one, which is the
    point of the change this helper follows.
    """
    return DbosQueries(engine, projection_limit or permissive_projection_limit())
