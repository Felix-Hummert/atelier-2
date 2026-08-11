from __future__ import annotations

from typing import cast

import pytest
from fastapi.testclient import TestClient

from atelier2.api.app import ApiPorts, create_app
from atelier2.api.problems import (
    PROBLEM_DEFINITIONS,
    PROBLEM_TYPE_PREFIX,
    problem_resource,
)
from atelier2.application.start_published_run import DurablePublishedRunStarter
from atelier2.contracts.runs import RunId
from atelier2.ports.durable_runs import TransactionalWaitAnswerer
from atelier2.ports.effects import TransactionalEffectReconcileCommander
from atelier2.ports.run_events import RunEventQueries
from atelier2.ports.run_queries import GetRunResult, ListRunsResult, RunQueries
from atelier2.ports.workflow_revisions import (
    WorkflowRevisionPublisher,
    WorkflowRevisionQueries,
)


def empty_ports() -> ApiPorts:
    missing = object()

    class UnusedRunQueries:
        def get_run(self, run_id: RunId) -> GetRunResult:
            del run_id
            raise AssertionError("invalid request reached run queries")

        def list_runs(self, after: RunId | None, limit: int) -> ListRunsResult:
            del after, limit
            raise AssertionError("invalid request reached run queries")

    return ApiPorts(
        workflow_revision_publisher=cast(WorkflowRevisionPublisher, missing),
        published_run_starter=cast(DurablePublishedRunStarter, missing),
        wait_answerer=cast(TransactionalWaitAnswerer, missing),
        reconcile_commander=cast(TransactionalEffectReconcileCommander, missing),
        workflow_revision_queries=cast(WorkflowRevisionQueries, missing),
        run_queries=cast(RunQueries, UnusedRunQueries()),
        run_event_queries=cast(RunEventQueries, missing),
    )


@pytest.mark.parametrize("code", sorted(PROBLEM_DEFINITIONS))
def test_problem_matrix_has_stable_exact_shape(code: str) -> None:
    definition = PROBLEM_DEFINITIONS[code]

    resource = problem_resource(code)

    assert resource.model_dump() == {
        "type": PROBLEM_TYPE_PREFIX + code,
        "title": definition.title,
        "status": definition.status,
        "detail": definition.detail,
    }


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
        create_app(source_commit="commit", source_tree="tree", ports=empty_ports()),
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
        create_app(source_commit="", source_tree="tree", ports=empty_ports())
    with pytest.raises(ValueError, match="source_tree"):
        create_app(source_commit="commit", source_tree="", ports=empty_ports())


def test_health_returns_only_injected_source_identities() -> None:
    client = TestClient(
        create_app(
            source_commit="source-commit",
            source_tree="source-tree",
            ports=empty_ports(),
        )
    )

    response = client.get("/atelier/api/v1/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "serving",
        "source_commit": "source-commit",
        "source_tree": "source-tree",
    }
