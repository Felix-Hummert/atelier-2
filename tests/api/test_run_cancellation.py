"""#439 P4: the operator run-cancel HTTP route, at its own layer.

`POST /runs/{public_ref}/cancellations` carries only the operator's opaque
`idempotency_key` and D2's `expected_node_execution_id`; the durable command id
is minted server-side, so no request field can force or bypass the reserved
namespace. These heads pin the thin HTTP shell -- the exact status of every
`RunCancellationResult`, the media guard, and the structural impossibility of a
client-named command id. The run's durable convergence to `CANCELLED` and the
exactly-once behaviour are proven against the real store in
`tests/integration/test_run_cancellation.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import get_args

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.api.app import create_app
from atelier2.api.projection.runs import run_resource
from atelier2.api.references import encode_public_run_reference
from atelier2.api.wire.resources import (
    RunCancellabilityResource,
    RunNotCancellableReasonName,
    RunResourceV3,
)
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptId,
    AgentAttemptState,
)
from atelier2.contracts.agents import (
    AgentBindingSet,
    AgentExecutionRequestHash,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_cancellations import CancelRunRequest
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.run_projections import (
    AgentAttemptProjection,
    NodeState,
    PublicAgentAttemptState,
    RunCancellationRefusal,
    RunProjection,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    NodeOutput,
    WaitNodeV3,
    WorkflowGraphV3,
)
from atelier2.ports.agent_attempts import (
    RunCancellationAccepted,
    RunCancellationCommandConflict,
    RunCancellationNotCancellable,
    RunCancellationOvertakenBySuccess,
    RunCancellationResult,
    RunCancellationRunMissing,
    RunCancellationTerminalRetry,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.run_queries import (
    GetReconciliationRetryTargetResult,
    GetRunResult,
    ListRunsResult,
    RunFound,
)
from atelier2.ports.workflow_revisions import DurableProjectionLimit
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

RUN_ID = RunId("run-cancel-route")
REVISION_HASH = WorkflowRevisionHash("a" * 64)
REQUEST_HASH = AgentExecutionRequestHash("b" * 64)
NODE_ID = "implement"
NODE_EXECUTION_ID = NodeExecutionId.for_node(RUN_ID, REVISION_HASH, NODE_ID)


def _graph() -> WorkflowGraphV3:
    return WorkflowGraphV3(
        format_version=3,
        name="One agent this cancel can stop",
        nodes=(
            AgentNodeV3(
                id=NODE_ID,
                type="agent",
                role="builder",
                mode="headless",
                instruction="Do the one thing this chain is for.",
            ),
        ),
    )


def _attempt_projection() -> AgentAttemptProjection:
    return AgentAttemptProjection(
        AgentAttemptId.for_execution(NODE_EXECUTION_ID, REQUEST_HASH, 1),
        NODE_EXECUTION_ID,
        REQUEST_HASH,
        1,
        PublicAgentAttemptState.PREPARED,
        None,
    )


def _started_projection() -> RunProjection:
    """A STARTED V3 run on a live agent node -- the one honestly cancellable shape."""
    return RunProjection(
        RunV3(
            RUN_ID,
            REVISION_HASH,
            AgentBindingSet(()).binding_set_hash,
            (),
            RunState.STARTED,
            NODE_ID,
            0,
            0,
            RunConfigurationRevisionHash("c" * 64),
        ),
        _graph(),
        None,
        (_attempt_projection(),),
    )


def _attempt() -> AgentAttempt:
    return AgentAttempt(
        AgentAttemptId.for_execution(NODE_EXECUTION_ID, REQUEST_HASH, 1),
        NODE_EXECUTION_ID,
        REQUEST_HASH,
        AgentExecutorOperationalIdentity("api-test-executor"),
        RUN_ID,
        REVISION_HASH,
        "builder",
        1,
        AgentAttemptState.PREPARED,
        0,
    )


@dataclass
class _Canceller:
    result: RunCancellationResult
    requests: list[CancelRunRequest] = field(default_factory=list)

    def request_run_cancellation(
        self, request: CancelRunRequest
    ) -> RunCancellationResult:
        self.requests.append(request)
        return self.result


@dataclass(frozen=True)
class _RunQueries:
    def get_run(
        self,
        run_id: object,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetRunResult:
        del run_id, projection_limit
        return RunFound(_started_projection())

    def list_runs(
        self,
        after: object,
        limit: int,
        state: object = None,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ListRunsResult:
        del after, limit, state, projection_limit
        raise AssertionError("run cancellation must not list runs")

    def get_reconciliation_retry_target(
        self,
        run_id: object,
        command_id: object,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetReconciliationRetryTargetResult:
        del run_id, command_id, projection_limit
        raise AssertionError("run cancellation must not query reconciliation")


def _client(canceller: _Canceller) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                run_queries=_RunQueries(), agent_attempt_canceller=canceller
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def _post(client: TestClient, key: str = "operator-key-1") -> Response:
    return client.post(
        "/atelier/api/v1/runs/"
        + encode_public_run_reference(RUN_ID)
        + "/cancellations",
        json={
            "idempotency_key": key,
            "expected_node_execution_id": NODE_EXECUTION_ID.value,
        },
    )


CASES = (
    ("accepted", RunCancellationAccepted(_attempt()), 202, None),
    (
        "terminal-retry",
        RunCancellationTerminalRetry(_started_projection().run),
        200,
        None,
    ),
    (
        "overtaken-by-success",
        RunCancellationOvertakenBySuccess(_started_projection().run),
        409,
        "run-cancellation-overtaken-by-success",
    ),
    (
        "not-cancellable",
        RunCancellationNotCancellable(RunCancellationRefusal.BETWEEN_NODES),
        409,
        "run-not-cancellable",
    ),
    (
        "command-conflict",
        RunCancellationCommandConflict(),
        409,
        "run-cancellation-command-conflict",
    ),
    ("run-missing", RunCancellationRunMissing(), 404, "run-not-found"),
    ("unavailable", DurableWriteUnavailable(), 503, "temporarily-unavailable"),
    ("corrupt", DurableStateCorrupt(), 500, "durable-state-corrupt"),
)


@pytest.mark.parametrize(
    ("_name", "result", "expected_status", "problem_code"),
    CASES,
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_run_cancellation_result_has_one_exact_http_mapping(
    _name: str,
    result: RunCancellationResult,
    expected_status: int,
    problem_code: str | None,
) -> None:
    canceller = _Canceller(result)

    response = _post(_client(canceller))

    assert response.status_code == expected_status
    assert canceller.requests == [
        CancelRunRequest(RUN_ID, "operator-key-1", NODE_EXECUTION_ID)
    ]
    if problem_code is None:
        assert response.headers["content-type"] == "application/json"
        assert response.json()["run_id"] == RUN_ID.value
    else:
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["type"] == "urn:atelier2:problem:v1:" + problem_code


def test_run_not_cancellable_names_the_reason_it_refused() -> None:
    """The refusal carries the closed reason token, not a bare 409."""
    canceller = _Canceller(
        RunCancellationNotCancellable(RunCancellationRefusal.WAITING_FOR_YOU)
    )

    response = _post(_client(canceller))

    assert response.status_code == 409
    assert RunCancellationRefusal.WAITING_FOR_YOU.value in response.json()["detail"]


def test_the_route_hands_the_store_only_the_operators_opaque_key() -> None:
    """The route mints no command id and forwards only what the operator keyed.

    #439's namespace invariant from the run-route side: the durable command id
    is framed server-side, inside the store, from this key alone. The route
    passes the key and D2's fence and nothing that could name a command.
    """
    canceller = _Canceller(RunCancellationAccepted(_attempt()))

    _post(_client(canceller), key="a-fresh-operator-key")

    assert canceller.requests == [
        CancelRunRequest(RUN_ID, "a-fresh-operator-key", NODE_EXECUTION_ID)
    ]


def test_no_request_field_can_name_a_command_and_force_the_namespace() -> None:
    """A body that tries to supply a command id is refused before the store.

    The wire shape has exactly two fields; the operator cannot hand the route a
    `command_id`, so a request can never reach past the mint the server owns.
    """
    canceller = _Canceller(RunCancellationAccepted(_attempt()))
    client = _client(canceller)

    response = client.post(
        "/atelier/api/v1/runs/"
        + encode_public_run_reference(RUN_ID)
        + "/cancellations",
        json={
            "idempotency_key": "operator-key-1",
            "expected_node_execution_id": NODE_EXECUTION_ID.value,
            "command_id": "atelier2-operator-run-cancel:" + "a" * 64,
        },
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-request")
    assert canceller.requests == []


def test_a_malformed_idempotency_key_is_refused_at_the_boundary() -> None:
    canceller = _Canceller(RunCancellationAccepted(_attempt()))
    client = _client(canceller)

    response = client.post(
        "/atelier/api/v1/runs/"
        + encode_public_run_reference(RUN_ID)
        + "/cancellations",
        json={
            "idempotency_key": "",
            "expected_node_execution_id": NODE_EXECUTION_ID.value,
        },
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-request")
    assert canceller.requests == []


def _projection(
    state: RunState,
    attempt_state: PublicAgentAttemptState | None,
    *,
    terminal_hash: Sha256Hash | None = None,
) -> RunProjection:
    attempts = (
        ()
        if attempt_state is None
        else (
            AgentAttemptProjection(
                AgentAttemptId.for_execution(NODE_EXECUTION_ID, REQUEST_HASH, 1),
                NODE_EXECUTION_ID,
                REQUEST_HASH,
                1,
                attempt_state,
                None,
            ),
        )
    )
    return RunProjection(
        RunV3(
            RUN_ID,
            REVISION_HASH,
            AgentBindingSet(()).binding_set_hash,
            (),
            state,
            NODE_ID,
            0,
            0,
            RunConfigurationRevisionHash("c" * 64),
            terminal_hash,
        ),
        _graph(),
        None,
        attempts,
    )


WAIT_NODE_ID = "approve"


def _wait_graph() -> WorkflowGraphV3:
    """A one-node graph parked on a Wait -- a live run here runs no agent to stop."""
    return WorkflowGraphV3(
        format_version=3,
        name="One wait no cancel can reach",
        nodes=(
            WaitNodeV3(
                id=WAIT_NODE_ID,
                type="wait",
                prompt="Approve this candidate before it lands.",
                outputs=(
                    NodeOutput.model_validate(
                        {"name": "ok", "schema": {"ref": "approval", "revision": "1"}}
                    ),
                ),
            ),
        ),
    )


def on_the_wait_node(
    state: RunState,
    *,
    terminal_hash: Sha256Hash | None = None,
    last_event_sequence: int = 0,
) -> RunProjection:
    """A V3 run standing at its Wait node, in the state the caller names.

    STARTED there runs no agent a cancel could stop; WAITING_INPUT there is the
    resting pause #668 made cancellable; CANCELLED there is that pause after an
    operator ended it. One builder for all three, because the only thing that
    differs is the word on the run and the hash an ended one carries.
    """
    return RunProjection(
        RunV3(
            RUN_ID,
            REVISION_HASH,
            AgentBindingSet(()).binding_set_hash,
            (),
            state,
            WAIT_NODE_ID,
            0,
            last_event_sequence,
            RunConfigurationRevisionHash("c" * 64),
            terminal_hash,
        ),
        _wait_graph(),
        None,
        (),
    )


def _cancellation(projection: RunProjection) -> RunCancellabilityResource:
    resource = run_resource(projection)
    assert isinstance(resource, RunResourceV3)
    return resource.cancellation


def test_a_live_started_agent_run_is_shown_cancellable_with_its_fence() -> None:
    cancellation = _cancellation(
        _projection(RunState.STARTED, PublicAgentAttemptState.PREPARED)
    )

    assert cancellation.cancellable is True
    assert cancellation.reason is None
    assert cancellation.target_node_execution_id == NODE_EXECUTION_ID.value


def test_a_run_resting_at_a_wait_is_cancellable_and_fences_on_that_pause() -> None:
    """#668: the pause itself is the fence, not whatever agent ran before it."""
    cancellation = _cancellation(on_the_wait_node(RunState.WAITING_INPUT))

    assert cancellation.cancellable is True
    assert cancellation.reason is None
    assert (
        cancellation.target_node_execution_id
        == NodeExecutionId.for_node(RUN_ID, REVISION_HASH, WAIT_NODE_ID).value
    )


def test_a_pause_a_cancel_ended_reads_as_cancelled_rather_than_still_working() -> None:
    """The rail on the answer an operator actually gets back, not on a stream frame.

    A run resource projects its rail from the snapshot with no events at all, so
    an ended pause has nothing but the run's own word to be read from. Before
    #668 that word was unhandled and every reader of a cancelled Wait was told
    the node was `working` -- a run that had stopped, still posing as busy.
    """
    resource = run_resource(
        on_the_wait_node(
            RunState.CANCELLED,
            terminal_hash=Sha256Hash.of(b"stopped at the pause"),
            last_event_sequence=1,
        )
    )

    assert isinstance(resource, RunResourceV3)
    assert [(entry.node_id, entry.state) for entry in resource.node_rail] == [
        (WAIT_NODE_ID, NodeState.CANCELLED)
    ]
    assert resource.cancellation.reason == RunCancellationRefusal.ALREADY_ENDED.value


@pytest.mark.parametrize(
    ("projection", "reason"),
    [
        (
            _projection(RunState.STARTED, None),
            RunCancellationRefusal.BETWEEN_NODES,
        ),
        (
            _projection(RunState.STARTED, PublicAgentAttemptState.CANCEL_REQUESTED),
            RunCancellationRefusal.ALREADY_CANCELLING,
        ),
        (
            _projection(RunState.WAITING_RECONCILIATION, None),
            RunCancellationRefusal.WAITING_FOR_YOU,
        ),
        (
            on_the_wait_node(RunState.STARTED),
            RunCancellationRefusal.NODE_RUNS_NO_AGENT,
        ),
        (
            _projection(
                RunState.CANCELLED,
                PublicAgentAttemptState.CANCELLED,
                terminal_hash=Sha256Hash.of(b"ended"),
            ),
            RunCancellationRefusal.ALREADY_ENDED,
        ),
    ],
    ids=[
        "between-nodes",
        "already-cancelling",
        "waiting-for-you",
        "node-runs-no-agent",
        "already-ended",
    ],
)
def test_a_non_cancellable_run_is_shown_as_such_with_its_reason(
    projection: RunProjection,
    reason: RunCancellationRefusal,
) -> None:
    cancellation = _cancellation(projection)

    assert cancellation.cancellable is False
    assert cancellation.target_node_execution_id is None
    assert cancellation.reason == reason.value


def test_the_wire_reason_literal_and_the_refusal_enum_cannot_drift() -> None:
    """Every refusal the enum owns has exactly one wire spelling, and no more.

    The projection casts `RunCancellationRefusal` values into the wire's
    `RunNotCancellableReasonName` Literal, so pyright cannot see a rename or a new
    reason drift the two apart. This set equality is that missing guard: extend or
    rename either side without the other and this fails before serve time, where a
    run in the un-spelled reason would 500 on read.
    """
    assert set(get_args(RunNotCancellableReasonName)) == {
        refusal.value for refusal in RunCancellationRefusal
    }


def test_the_route_requires_the_json_media_type() -> None:
    canceller = _Canceller(RunCancellationAccepted(_attempt()))
    client = _client(canceller)

    response = client.post(
        "/atelier/api/v1/runs/"
        + encode_public_run_reference(RUN_ID)
        + "/cancellations",
        content=b"{}",
        headers={"content-type": "text/plain"},
    )

    assert response.status_code == 415
    assert response.json()["type"].endswith(":unsupported-media-type")
    assert canceller.requests == []
