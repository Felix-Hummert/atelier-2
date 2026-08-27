from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.references import encode_public_run_reference
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.run_forks import RunFork, RunForkCommandId, successor_run_id_for
from atelier2.contracts.run_projections import RunForkOriginProjection, RunProjection
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflows_v3 import NodeOutput, WaitNodeV3, WorkflowGraphV3
from atelier2.ports.durable_run_forks import (
    DurableRunForkCapabilityUnavailable,
    DurableRunForkCommandConflict,
    DurableRunForkCreated,
    DurableRunForkExecutorUnavailable,
    DurableRunForkExisting,
    DurableRunForkLoopUnsupported,
    DurableRunForkNodeMissing,
    DurableRunForkOriginMissing,
    DurableRunForkOriginNotTerminal,
    DurableRunForkPrefixNotReusable,
    DurableRunForkResult,
    DurableRunForkStateCorrupt,
    DurableRunForkWriteUnavailable,
    ForkRunRequest,
)
from atelier2.ports.run_queries import RunFound
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

ORIGIN = RunId("fork-api-origin")
IDEMPOTENCY_KEY = "retry-review"
COMMAND_ID = RunForkCommandId.for_request(ORIGIN, IDEMPOTENCY_KEY)
SUCCESSOR = successor_run_id_for(COMMAND_ID)
REVISION = WorkflowRevisionHash("a" * 64)
CONFIGURATION = RunConfigurationRevisionHash("b" * 64)
ORIGIN_TERMINAL_HASH = Sha256Hash("c" * 64)
GRAPH = WorkflowGraphV3(
    format_version=3,
    name="One forkable wait",
    nodes=(
        WaitNodeV3(
            id="review",
            type="wait",
            prompt="Review the successor.",
            outputs=(
                NodeOutput.model_validate(
                    {"name": "ok", "schema": {"ref": "approval", "revision": "1"}}
                ),
            ),
        ),
    ),
)
RUN = RunV3(
    SUCCESSOR,
    REVISION,
    AgentBindingSet(()).binding_set_hash,
    (),
    RunState.STARTED,
    "review",
    0,
    0,
    CONFIGURATION,
)
FORK = RunFork(
    COMMAND_ID,
    ORIGIN,
    ORIGIN_TERMINAL_HASH,
    SUCCESSOR,
    REVISION,
    CONFIGURATION,
    "review",
    (),
    (),
)
PROJECTION = RunProjection(
    RUN,
    GRAPH,
    None,
    fork_origin=RunForkOriginProjection(
        ORIGIN,
        ORIGIN_TERMINAL_HASH,
        "review",
        FORK.fork_hash,
    ),
)


@dataclass
class _Forker:
    result: DurableRunForkResult
    requests: list[ForkRunRequest] = field(default_factory=list)

    def fork_run(self, request: ForkRunRequest) -> DurableRunForkResult:
        self.requests.append(request)
        return self.result


@dataclass(frozen=True)
class _RunQueries:
    def get_run(self, run_id: object, projection_limit: object = None) -> RunFound:
        del projection_limit
        assert run_id == SUCCESSOR
        return RunFound(PROJECTION)


def _client(forker: _Forker) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                published_run_starter=forker,
                run_queries=_RunQueries(),
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def _post(client: TestClient, body: dict[str, str] | None = None):
    return client.post(
        "/atelier/api/v1/runs/" + encode_public_run_reference(ORIGIN) + "/forks",
        json=(
            {
                "idempotency_key": IDEMPOTENCY_KEY,
                "restart_from_node_id": "review",
            }
            if body is None
            else body
        ),
    )


@pytest.mark.parametrize(
    ("result", "status"),
    (
        (DurableRunForkCreated(FORK, RUN), 201),
        (DurableRunForkExisting(FORK, RUN), 200),
    ),
)
def test_created_and_exact_retry_have_distinct_success_statuses(
    result: DurableRunForkResult, status: int
) -> None:
    forker = _Forker(result)

    response = _post(_client(forker))

    assert response.status_code == status
    assert response.json()["run_id"] == SUCCESSOR.value
    assert response.json()["fork_origin"] == {
        "public_run_reference": encode_public_run_reference(ORIGIN),
        "terminal_hash": ORIGIN_TERMINAL_HASH.value,
        "restart_from_node_id": "review",
        "fork_hash": FORK.fork_hash.value,
    }
    assert forker.requests == [ForkRunRequest(ORIGIN, IDEMPOTENCY_KEY, "review")]


@pytest.mark.parametrize(
    ("result", "status", "problem"),
    (
        (DurableRunForkOriginMissing(), 404, "run-not-found"),
        (
            DurableRunForkOriginNotTerminal(),
            409,
            "run-fork-origin-not-terminal",
        ),
        (DurableRunForkNodeMissing(), 409, "run-fork-node-missing"),
        (DurableRunForkLoopUnsupported(), 409, "run-fork-loop-unsupported"),
        (
            DurableRunForkPrefixNotReusable(),
            409,
            "run-fork-prefix-not-reusable",
        ),
        (DurableRunForkCommandConflict(), 409, "run-fork-command-conflict"),
        (
            DurableRunForkExecutorUnavailable(),
            409,
            "agent-executor-binding-unavailable",
        ),
        (
            DurableRunForkCapabilityUnavailable(),
            409,
            "agent-executor-binding-unavailable",
        ),
        (DurableRunForkWriteUnavailable("busy"), 503, "temporarily-unavailable"),
        (DurableRunForkStateCorrupt(), 500, "durable-state-corrupt"),
    ),
)
def test_every_fork_refusal_has_one_closed_http_problem(
    result: DurableRunForkResult, status: int, problem: str
) -> None:
    response = _post(_client(_Forker(result)))

    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == "urn:atelier2:problem:v1:" + problem


@pytest.mark.parametrize(
    "body",
    (
        {"idempotency_key": "", "restart_from_node_id": "review"},
        {"idempotency_key": IDEMPOTENCY_KEY, "restart_from_node_id": ""},
        {
            "idempotency_key": IDEMPOTENCY_KEY,
            "restart_from_node_id": "review",
            "successor_run_id": "client-owned",
        },
        {
            "idempotency_key": IDEMPOTENCY_KEY,
            "restart_from_node_id": "review",
            "workflow_revision_hash": REVISION.value,
        },
    ),
)
def test_the_closed_request_refuses_empty_or_client_owned_identity_fields(
    body: dict[str, str],
) -> None:
    forker = _Forker(DurableRunForkCreated(FORK, RUN))

    response = _post(_client(forker), body)

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-request")
    assert forker.requests == []
