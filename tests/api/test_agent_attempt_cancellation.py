from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.references import encode_public_run_reference
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agents import AgentExecutorOperationalIdentity
from atelier2.contracts.run_cancellations import RunCancelCommandId
from atelier2.contracts.run_projections import PublicAgentAttemptState
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationCommandConflict,
    AgentAttemptCancellationNotCurrent,
    AgentAttemptCancellationResult,
    AgentAttemptCancellationRunMissing,
    AgentAttemptCancellationStale,
    AgentAttemptCancellationTargetMissing,
    AgentAttemptCancellationTerminalConflict,
    AgentAttemptReplacementNotAllowed,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.run_queries import (
    GetReconciliationRetryTargetResult,
    GetRunResult,
    ListRunsResult,
    RunFound,
)
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
)
from tests.api.test_agent_attempts import v2_run_projection
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff


@dataclass
class _Canceller:
    result: AgentAttemptCancellationResult
    requests: list[CancelAgentAttemptRequest] = field(default_factory=list)

    def request_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationResult:
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
        return RunFound(v2_run_projection(PublicAgentAttemptState.PREPARED))

    def list_runs(
        self,
        after: object,
        limit: int,
        state: object = None,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ListRunsResult:
        del after, limit, state, projection_limit
        raise AssertionError("cancellation must not list runs")

    def get_reconciliation_retry_target(
        self,
        run_id: object,
        command_id: object,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetReconciliationRetryTargetResult:
        del run_id, command_id, projection_limit
        raise AssertionError("cancellation must not query reconciliation")


def _attempt() -> AgentAttempt:
    projection = v2_run_projection(PublicAgentAttemptState.PREPARED)
    projected = projection.current_agent_attempt
    assert projected is not None
    run = projection.run
    return AgentAttempt(
        projected.attempt_id,
        projected.node_execution_id,
        projected.request_hash,
        AgentExecutorOperationalIdentity("api-test-executor"),
        run.run_id,
        run.revision_hash,
        "build",
        1,
        AgentAttemptState.PREPARED,
        0,
    )


CASES = (
    ("accepted", AgentAttemptCancellationAccepted(_attempt(), False), 202, None),
    ("terminal-retry", AgentAttemptCancellationAccepted(_attempt(), True), 200, None),
    ("run-missing", AgentAttemptCancellationRunMissing(), 404, "run-not-found"),
    (
        "attempt-missing",
        AgentAttemptCancellationTargetMissing(),
        404,
        "agent-attempt-not-found",
    ),
    (
        "not-current",
        AgentAttemptCancellationNotCurrent(),
        409,
        "agent-attempt-not-current",
    ),
    (
        "stale",
        AgentAttemptCancellationStale(),
        409,
        "agent-attempt-cancellation-stale",
    ),
    (
        "terminal-conflict",
        AgentAttemptCancellationTerminalConflict(),
        409,
        "agent-attempt-terminal",
    ),
    (
        "command-conflict",
        AgentAttemptCancellationCommandConflict(),
        409,
        "cancellation-command-conflict",
    ),
    (
        "replacement-refused",
        AgentAttemptReplacementNotAllowed(),
        409,
        "replacement-not-allowed",
    ),
    ("unavailable", DurableWriteUnavailable(), 503, "temporarily-unavailable"),
    ("corrupt", DurableStateCorrupt(), 500, "durable-state-corrupt"),
)


@pytest.mark.parametrize(
    ("_name", "result", "expected_status", "problem_code"),
    CASES,
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_cancellation_result_has_one_exact_http_mapping(
    _name: str,
    result: AgentAttemptCancellationResult,
    expected_status: int,
    problem_code: str | None,
) -> None:
    canceller = _Canceller(result)
    ports = api_ports(run_queries=_RunQueries(), agent_attempt_canceller=canceller)
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=ports,
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )
    attempt = _attempt()

    response = client.post(
        "/atelier/api/v1/runs/"
        + encode_public_run_reference(attempt.run_id)
        + "/agent-attempts/"
        + attempt.attempt_id.value
        + "/cancellations",
        json={
            "command_id": "cancel-1",
            "expected_attempt_state_version": 0,
            "replacement": "ONE",
        },
    )

    assert response.status_code == expected_status
    assert len(canceller.requests) == 1
    assert canceller.requests[0] == CancelAgentAttemptRequest(
        attempt.run_id,
        attempt.attempt_id,
        "cancel-1",
        0,
        AgentAttemptReplacement.ONE,
    )
    if problem_code is None:
        assert response.headers["content-type"] == "application/json"
        assert response.json()["run_id"] == attempt.run_id.value
    else:
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["type"] == "urn:atelier2:problem:v1:" + problem_code


def test_cancellation_rejects_noncanonical_attempt_identity_before_the_port() -> None:
    canceller = _Canceller(AgentAttemptCancellationRunMissing())
    client = TestClient(
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

    response = client.post(
        "/atelier/api/v1/runs/run1.YXR0ZW1wdC9hcGk/agent-attempts/not-a-hash/cancellations",
        json={
            "command_id": "cancel-1",
            "expected_attempt_state_version": 0,
            "replacement": "NONE",
        },
    )

    assert response.status_code == 400
    assert response.json()["type"].endswith(":invalid-agent-attempt-id")
    assert canceller.requests == []


def test_cancellation_rejects_a_run_cancel_namespaced_command_id_before_the_port() -> (
    None
):
    """#439's namespace invariant, from the attempt-route side.

    A `command_id` an operator's run-cancel confirmation minted
    (`RunCancelCommandId.for_key`) can never legitimately arrive on this
    route: that mint only ever happens server-side, from a run-cancel
    idempotency key, never from a client-supplied attempt command id. The
    route refuses it before the port is even asked, the same way it already
    refuses a noncanonical attempt id.
    """
    canceller = _Canceller(AgentAttemptCancellationRunMissing())
    client = TestClient(
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
    attempt = _attempt()
    reserved_command_id = RunCancelCommandId.for_key("operator-key-1").value

    response = client.post(
        "/atelier/api/v1/runs/"
        + encode_public_run_reference(attempt.run_id)
        + "/agent-attempts/"
        + attempt.attempt_id.value
        + "/cancellations",
        json={
            "command_id": reserved_command_id,
            "expected_attempt_state_version": 0,
            "replacement": "NONE",
        },
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-request")
    assert response.json()["invalid_fields"] == [
        {
            "path": "body/command_id",
            "reason": "belongs to the reserved run-cancel command namespace",
        }
    ]
    assert canceller.requests == []
