from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import ApiPorts, create_app
from atelier2.api.problems import (
    PROBLEM_DEFINITIONS,
    PROBLEM_TYPE_PREFIX,
    problem_resource,
)
from atelier2.contracts.runs import RunId
from atelier2.ports.run_queries import GetRunResult, ListRunsResult
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

EXPECTED_PROBLEMS = {
    "invalid-agent-attempt-id": (
        400,
        "Invalid agent attempt id",
        "Use exactly 64 lowercase hexadecimal characters.",
    ),
    "agent-attempt-not-found": (
        404,
        "Agent attempt not found",
        "Cancel an attempt that belongs to the referenced run.",
    ),
    "agent-attempt-not-current": (
        409,
        "Agent attempt is not current",
        "Reload the run and cancel only its current attempt.",
    ),
    "agent-attempt-cancellation-stale": (
        409,
        "Agent attempt cancellation is stale",
        "Reload the run and bind the command to its current attempt state version.",
    ),
    "agent-attempt-terminal": (
        409,
        "Agent attempt is terminal",
        "A completed attempt can no longer be cancelled.",
    ),
    "cancellation-command-conflict": (
        409,
        "Cancellation command conflict",
        "Use a new command_id or retry the exact original cancellation command.",
    ),
    "replacement-not-allowed": (
        409,
        "Replacement is not allowed",
        "Only ordinal one may request the single replacement attempt.",
    ),
    "auth-profile-revision-conflict": (
        409,
        "Auth profile revision conflict",
        "Use a new revision_number or retry the exact original auth profile revision.",
    ),
    "auth-profile-revision-collision": (
        409,
        "Auth profile revision collision",
        "Stop mutation and inspect durable auth profile revision integrity.",
    ),
    "auth-profile-revision-not-found": (
        404,
        "Auth profile revision not found",
        "Publish the exact auth profile revision before publishing an agent configuration.",
    ),
    "agent-executor-binding-unavailable": (
        409,
        "Agent executor binding unavailable",
        "Register the exact provider and executor revision before publishing or starting this configuration.",
    ),
    "agent-configuration-revision-collision": (
        409,
        "Agent configuration revision collision",
        "Stop mutation and inspect durable agent configuration revision integrity.",
    ),
    "agent-configuration-revision-not-found": (
        404,
        "Agent configuration revision not found",
        "Publish every exact agent configuration revision before starting the run.",
    ),
    "invalid-agent-bindings": (
        422,
        "Invalid agent bindings",
        "Bind every workflow agent role exactly once and no other role.",
    ),
    "invalid-public-run-reference": (
        400,
        "Invalid public run reference",
        "Use a canonical run1 public reference.",
    ),
    "invalid-event-cursor": (
        400,
        "Invalid event cursor",
        "Use a canonical event1 cursor returned by this API.",
    ),
    "invalid-revision-hash": (
        400,
        "Invalid revision hash",
        "Use exactly 64 lowercase hexadecimal characters.",
    ),
    "event-cursor-run-mismatch": (
        409,
        "Event cursor belongs to another run",
        "Reconnect with a cursor returned for this run.",
    ),
    "event-cursor-ahead": (
        409,
        "Event cursor is ahead of durable history",
        "Reconnect from a cursor at or below the durable head.",
    ),
    "invalid-request": (
        422,
        "Invalid request",
        "Correct the request fields and submit it again.",
    ),
    "invalid-base64": (
        422,
        "Invalid base64",
        "Use canonical RFC 4648 base64 with required padding.",
    ),
    "invalid-workflow-document": (
        422,
        "Invalid workflow document",
        "Submit exact bytes for a safe closed workflow graph.",
    ),
    "unsupported-media-type": (
        415,
        "Unsupported media type",
        "Use the media type documented for this operation.",
    ),
    "not-acceptable": (
        406,
        "Not acceptable",
        "Accept text/event-stream or */*.",
    ),
    "workflow-revision-not-found": (
        404,
        "Workflow revision not found",
        "Publish the exact workflow revision before starting a run.",
    ),
    "run-not-found": (
        404,
        "Run not found",
        "Use a public reference for a durable run that exists.",
    ),
    "node-not-found": (
        404,
        "Node not found",
        "Answer a node in the referenced workflow graph.",
    ),
    "revision-collision": (
        409,
        "Workflow revision collision",
        "Stop and inspect durable revision integrity.",
    ),
    "workflow-format-not-executable": (
        409,
        "Workflow format is not executable",
        "This revision is published and no runtime here runs its format version yet.",
    ),
    "run-identity-conflict": (
        409,
        "Run identity conflict",
        "Use a new run_id or retry the exact original revision.",
    ),
    "answer-revision-conflict": (
        409,
        "Answer revision conflict",
        "Retry with the run's exact workflow revision.",
    ),
    "answer-state-conflict": (
        409,
        "Answer state conflict",
        "Answer only the run's current waiting input node.",
    ),
    "answer-bytes-conflict": (
        409,
        "Answer bytes conflict",
        "Retry the exact original answer bytes or use another command.",
    ),
    "reconciliation-target-missing": (
        409,
        "Reconciliation target missing",
        "Reconcile only the run's current unresolved Action.",
    ),
    "reconciliation-stale": (
        409,
        "Reconciliation is stale",
        "Reload the run and bind a command to its current intent state version.",
    ),
    "reconciliation-command-conflict": (
        409,
        "Reconciliation command conflict",
        "Use a new command_id or retry the exact original command.",
    ),
    "reconciliation-determination-conflict": (
        409,
        "Reconciliation determination conflict",
        "Use a new command_id for a changed determination.",
    ),
    "reconciliation-rejected": (
        409,
        "Reconciliation was rejected",
        "Reload the run before issuing another accountable command.",
    ),
    "route-not-found": (
        404,
        "Route not found",
        "Use a path described by this API's OpenAPI document.",
    ),
    "method-not-allowed": (
        405,
        "Method not allowed",
        "Use the HTTP method described for this path.",
    ),
    "temporarily-unavailable": (
        503,
        "Temporarily unavailable",
        "Retry after the durable store becomes available.",
    ),
    "durable-state-corrupt": (
        500,
        "Durable state is corrupt",
        "Stop mutation and inspect the durable store.",
    ),
    "internal-error": (
        500,
        "Internal error",
        "Retry only after the server fault has been inspected.",
    ),
}


def empty_ports() -> ApiPorts:
    class UnusedRunQueries:
        def get_run(self, run_id: RunId) -> GetRunResult:
            del run_id
            raise AssertionError("invalid request reached run queries")

        def list_runs(self, after: RunId | None, limit: int) -> ListRunsResult:
            del after, limit
            raise AssertionError("invalid request reached run queries")

    return api_ports(run_queries=UnusedRunQueries())


@pytest.mark.parametrize("code", sorted(EXPECTED_PROBLEMS))
def test_problem_matrix_has_stable_exact_shape(code: str) -> None:
    assert set(PROBLEM_DEFINITIONS) == set(EXPECTED_PROBLEMS)
    definition = PROBLEM_DEFINITIONS[code]
    status, title, detail = EXPECTED_PROBLEMS[code]

    resource = problem_resource(code)

    assert resource.model_dump() == {
        "type": PROBLEM_TYPE_PREFIX + code,
        "title": title,
        "status": status,
        "detail": detail,
    }
    assert (definition.status, definition.title, definition.detail) == (
        status,
        title,
        detail,
    )


@pytest.mark.parametrize(
    ("path", "method", "status", "code"),
    [
        ("/missing", "get", 404, "route-not-found"),
        ("/atelier/api/v1/health", "post", 405, "method-not-allowed"),
        ("/atelier/api/v1/runs", "post", 415, "unsupported-media-type"),
        ("/atelier/api/v1/runs", "get", 422, "invalid-request"),
    ],
)
def test_framework_and_media_errors_are_normalized(
    path: str, method: str, status: int, code: str
) -> None:
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=empty_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        ),
        raise_server_exceptions=False,
    )
    response = (
        client.get(path + "?limit=false")
        if code == "invalid-request"
        else client.request(method, path)
    )

    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"] == PROBLEM_TYPE_PREFIX + code
    assert set(response.json()) == {"type", "title", "status", "detail"}


def test_app_requires_both_source_identities_at_construction() -> None:
    with pytest.raises(ValueError, match="source_commit"):
        create_app(
            source_commit="",
            source_tree="tree",
            ports=empty_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    with pytest.raises(ValueError, match="source_tree"):
        create_app(
            source_commit="commit",
            source_tree="",
            ports=empty_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )


def test_health_returns_only_injected_source_identities() -> None:
    client = TestClient(
        create_app(
            source_commit="source-commit",
            source_tree="source-tree",
            ports=empty_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )

    response = client.get("/atelier/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "serving",
        "source_commit": "source-commit",
        "source_tree": "source-tree",
    }
