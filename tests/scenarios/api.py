from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, Never

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.yaml_workflows import (
    parse_executable_workflow_document,
    parse_workflow_document,
)
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.limits import ApiLimits
from atelier2.api.stream import EventPollBackoff
from atelier2.application.publish_workflow_revision import WorkflowPublicationLimits
from atelier2.application.read_run_events import ReadRunEventsResult, read_run_events
from atelier2.contracts.run_projections import (
    RunProjection,
)
from atelier2.contracts.runs import Run, RunId, RunState, WorkflowRevision
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
RECONCILIATION_REQUEST_HASH = (
    "1f58b9145b24d108d7ac38887338b3ea3229833b9c1e418250343f907bfd1047"
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
            "payload_hash": RECONCILIATION_REQUEST_HASH,
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
                "915d3c895503c1c82c08f6cdd74bd6aa9e24f375297e04050c15a5abcb77725b"
            ),
            "event": "ACTION_RECONCILIATION_REQUIRED",
            "request_base64": "cmVxdWVzdA==",
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


STREAM_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: agent, type: agent, job: test, output: payload, next: final}
"""


def stream_run_projection(run_id: str) -> RunProjection:
    """The run an SSE scenario streams, so every frame can say where it stands.

    The event endpoint reads the run once and folds the streamed events onto it.
    A stream scenario therefore needs a run, not only an event store, and this is
    the one place that decides which run that is.
    """

    revision = WorkflowRevision(STREAM_DOCUMENT)
    return RunProjection(
        Run(RunId(run_id), revision.revision_hash, RunState.STARTED, "agent", 0, 0),
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
        "agent_configuration_catalog": unused,
        "agent_attempt_canceller": unused,
        "catalog_resolver": unused,
        "catalog_admissions": unused,
        "published_revision_registry": unused,
    }
    ports.update(overrides)
    return ApiPorts(**ports)


def api_limits(**changes: int) -> ApiLimits:
    configured = ApiLimits(
        maximum_request_body_bytes=65_536,
        maximum_field_characters=1_024,
        maximum_base64_characters=65_536,
        maximum_decoded_payload_bytes=49_152,
        maximum_workflow_nodes=100,
        maximum_enriched_page_nodes=100,
        maximum_enriched_page_document_bytes=65_536,
        event_page_size=50,
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


def durable_api_client(
    runtime: DbosRuntime, limits: ApiLimits | None = None
) -> TestClient:
    """The real HTTP boundary in front of one real durable runtime.

    Whether a request is refused before anything durable exists is a property
    of the composed server, not of a starter called by hand, so a test that
    claims an HTTP answer asks for it here.
    """

    queries = durable_queries(runtime.engine)
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=ApiPorts(
                workflow_revision_publisher=DbosWorkflowRevisionPublisher(
                    runtime.engine
                ),
                published_run_starter=DbosDurableRunStarter(
                    runtime.engine, runtime.settings, runtime.agent_executor_registry
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
            ),
            limits=api_limits() if limits is None else limits,
            event_poll_backoff=event_poll_backoff(),
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
