from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import ApiPorts, create_app
from atelier2.api.limits import ApiLimitExceeded, ApiLimits, RequestBodyLimitMiddleware
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import (
    encode_canonical_base64,
    encode_event_cursor,
    encode_public_run_reference,
)
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.runs import Run, RunId, RunState, WorkflowRevision
from atelier2.ports.durable_runs import (
    DurableAnswerRunMissing,
    DurablePublishedRunStarter,
    DurableRunRevisionMissing,
    TransactionalWaitAnswerer,
)
from atelier2.ports.effects import TransactionalEffectReconcileCommander
from atelier2.ports.run_events import PersistedRunEvent, RunEventQueries
from atelier2.ports.run_queries import RunProjection, RunQueries
from atelier2.ports.workflow_revisions import (
    DurableRevisionCreated,
    WorkflowRevisionPublisher,
    WorkflowRevisionQueries,
)
from tests.scenarios.api import api_limits, event_poll_backoff


@dataclass
class RecordingMutationPorts:
    publications: list[WorkflowRevision] = field(default_factory=list)
    starts: list[object] = field(default_factory=list)
    answers: list[object] = field(default_factory=list)

    def publish(self, revision: WorkflowRevision) -> DurableRevisionCreated:
        self.publications.append(revision)
        return DurableRevisionCreated(revision)

    def start_published(self, request: object) -> DurableRunRevisionMissing:
        self.starts.append(request)
        return DurableRunRevisionMissing()

    def submit_result(self, request: object) -> DurableAnswerRunMissing:
        self.answers.append(request)
        return DurableAnswerRunMissing()


def client_for(mutations: RecordingMutationPorts, limits: ApiLimits) -> TestClient:
    unused = object()
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=ApiPorts(
                cast(WorkflowRevisionPublisher, mutations),
                cast(DurablePublishedRunStarter, mutations),
                cast(TransactionalWaitAnswerer, mutations),
                cast(TransactionalEffectReconcileCommander, unused),
                cast(WorkflowRevisionQueries, unused),
                cast(RunQueries, unused),
                cast(RunEventQueries, unused),
                parse_workflow_document,
            ),
            limits=limits,
            event_poll_backoff=event_poll_backoff(),
        )
    )


def workflow_document(*, job: str = "work", include_agent: bool = False) -> bytes:
    if include_agent:
        return (
            "format_version: 1\n"
            "start: agent\n"
            "nodes:\n"
            f"  - {{id: agent, type: agent, job: {job}, output: done, next: final}}\n"
            "  - {id: final, type: subworkflow, operation: add, operands: [1, 2], next: null}\n"
        ).encode()
    return (
        b"format_version: 1\n"
        b"start: final\n"
        b"nodes:\n"
        b"  - {id: final, type: subworkflow, operation: add, operands: [1, 2], next: null}\n"
    )


def assert_problem(response: Response, status: int, code: str) -> None:
    assert response.status_code == status
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"].endswith(":" + code)


def test_api_limits_reject_base64_capacity_that_cannot_encode_a_payload() -> None:
    with pytest.raises(ValueError, match="maximum_base64_characters"):
        api_limits(maximum_base64_characters=3)

    limits = api_limits(maximum_base64_characters=4)
    assert limits.maximum_base64_decoded_bytes == 3
    client_for(RecordingMutationPorts(), limits)


@pytest.mark.parametrize(
    ("limits", "payload", "detail"),
    [
        (
            api_limits(maximum_decoded_payload_bytes=2),
            b"123",
            "decoded payload exceeds its byte limit",
        ),
        (
            api_limits(
                maximum_base64_characters=4,
                maximum_decoded_payload_bytes=4,
            ),
            b"1234",
            "encoded payload exceeds its character limit",
        ),
    ],
)
def test_encoded_projection_limit_branches_are_explicit(
    limits: ApiLimits, payload: bytes, detail: str
) -> None:
    with pytest.raises(ValueError, match=detail):
        limits.require_encoded_payload(payload)


@pytest.mark.parametrize(
    ("path", "problem_code"),
    [
        (API_PREFIX + "/workflow-revisions", "invalid-workflow-document"),
        (API_PREFIX + "/runs", "invalid-request"),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"1"), (b"content-length", b"1")],
        [(b"content-length", b"one")],
        [(b"content-length", b"-1")],
    ],
)
def test_body_limit_rejects_noncanonical_content_length_directly(
    path: str,
    problem_code: str,
    headers: list[tuple[bytes, bytes]],
) -> None:
    async def scenario() -> tuple[int, bytes, bool]:
        reached_route = False
        messages: list[Message] = []

        async def route(_scope: Scope, _receive: Receive, send: Send) -> None:
            nonlocal reached_route
            reached_route = True
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = RequestBodyLimitMiddleware(
            cast(ASGIApp, route),
            maximum_body_bytes=4,
            api_prefix=API_PREFIX,
        )

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            messages.append(message)

        await middleware(
            cast(
                Scope,
                {
                    "type": "http",
                    "method": "POST",
                    "path": path,
                    "headers": headers,
                },
            ),
            receive,
            send,
        )
        start = messages[0]
        body = messages[1]
        return int(start["status"]), cast(bytes, body["body"]), reached_route

    status, body, reached_route = asyncio.run(scenario())

    assert status == 422
    assert problem_code.encode() in body
    assert not reached_route


def test_body_limit_bypasses_routes_that_do_not_read_a_body() -> None:
    async def scenario() -> tuple[int, bool]:
        reached_route = False
        messages: list[Message] = []

        async def route(_scope: Scope, _receive: Receive, send: Send) -> None:
            nonlocal reached_route
            reached_route = True
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        middleware = RequestBodyLimitMiddleware(
            cast(ASGIApp, route),
            maximum_body_bytes=4,
            api_prefix=API_PREFIX,
        )

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            messages.append(message)

        await middleware(
            cast(
                Scope,
                {
                    "type": "http",
                    "method": "GET",
                    "path": API_PREFIX + "/health",
                    "headers": [(b"content-length", b"invalid")],
                },
            ),
            receive,
            send,
        )
        return int(messages[0]["status"]), reached_route

    assert asyncio.run(scenario()) == (204, True)


def test_yaml_body_limit_accepts_exact_bytes_and_rejects_one_more_before_write() -> (
    None
):
    mutations = RecordingMutationPorts()
    document = workflow_document()
    client = client_for(
        mutations,
        api_limits(maximum_request_body_bytes=len(document)),
    )

    exact = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=document,
        headers={"content-type": "application/yaml"},
    )
    oversized = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=document + b" ",
        headers={"content-type": "application/yaml"},
    )

    assert exact.status_code == 201
    assert_problem(oversized, 422, "invalid-workflow-document")
    assert mutations.publications == [WorkflowRevision(document)]


def test_missing_content_length_is_bounded_while_receiving_chunks() -> None:
    mutations = RecordingMutationPorts()
    document = workflow_document()
    client = client_for(
        mutations,
        api_limits(maximum_request_body_bytes=len(document)),
    )

    response = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=iter((document, b" ")),
        headers={"content-type": "application/yaml"},
    )

    assert "content-length" not in response.request.headers
    assert_problem(response, 422, "invalid-workflow-document")
    assert response.json()["detail"] == "Request body exceeds its byte limit."
    assert mutations.publications == []


def test_declared_and_chunked_body_overflow_have_the_same_exact_detail() -> None:
    mutations = RecordingMutationPorts()
    document = workflow_document()
    client = client_for(
        mutations,
        api_limits(maximum_request_body_bytes=len(document)),
    )

    declared = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=document + b" ",
        headers={"content-type": "application/yaml"},
    )
    chunked = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=iter((document, b" ")),
        headers={"content-type": "application/yaml"},
    )

    assert declared.status_code == chunked.status_code == 422
    assert (
        declared.json()["detail"]
        == chunked.json()["detail"]
        == ("Request body exceeds its byte limit.")
    )


def test_overlong_decoded_reference_and_cursor_keep_their_existing_codes() -> None:
    mutations = RecordingMutationPorts()
    client = client_for(mutations, api_limits(maximum_field_characters=12))
    overlong_reference = encode_public_run_reference(RunId("1234567890123"))
    valid_reference = encode_public_run_reference(RunId("run"))

    reference = client.get(f"/atelier/api/v1/runs/{overlong_reference}")
    cursor = client.get(
        f"/atelier/api/v1/runs/{valid_reference}/events",
        headers={
            "accept": "text/event-stream",
            "last-event-id": "event1.cnVu.123",
        },
    )

    assert_problem(reference, 400, "invalid-public-run-reference")
    assert_problem(cursor, 400, "invalid-event-cursor")


def test_public_reference_is_bounded_before_base64_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutations = RecordingMutationPorts()
    client = client_for(mutations, api_limits(maximum_field_characters=12))

    def unexpected_decode(*_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("over-limit public reference reached base64 decoding")

    monkeypatch.setattr("atelier2.api.references.base64.b64decode", unexpected_decode)

    response = client.get("/atelier/api/v1/runs/run1." + "a" * 13)

    assert_problem(response, 400, "invalid-public-run-reference")


def test_wire_reference_and_cursor_are_bounded_by_their_own_encoding() -> None:
    document = workflow_document()
    revision = WorkflowRevision(document)
    graph = parse_workflow_document(document)
    run = Run(
        RunId("x"),
        revision.revision_hash,
        RunState.STARTED,
        "final",
        0,
        1,
    )
    projection = RunProjection(run, graph, None)
    event = PersistedRunEvent(
        RunEvent(
            run.run_id,
            run.revision_hash,
            1,
            "final",
            NodeExecutionId.for_node(run.run_id, run.revision_hash, "final"),
            RunEventKind.SUBWORKFLOW_COMPLETED,
            b"3",
        ),
        None,
    )
    reference = encode_public_run_reference(run.run_id)
    cursor = encode_event_cursor(run.run_id, 1)
    assert len(reference) < len(cursor)

    with pytest.raises(ApiLimitExceeded, match="public run reference"):
        api_limits(maximum_field_characters=len(reference) - 1).require_run_projection(
            projection
        )
    with pytest.raises(ApiLimitExceeded, match="event cursor"):
        api_limits(maximum_field_characters=len(cursor) - 1).require_event_projection(
            event
        )


def test_json_body_limit_runs_before_fastapi_buffers_or_validates_the_body() -> None:
    mutations = RecordingMutationPorts()
    exact_body = b"{" + b" " * 15
    client = client_for(
        mutations,
        api_limits(maximum_request_body_bytes=len(exact_body)),
    )

    within_limit = client.post(
        "/atelier/api/v1/runs",
        content=exact_body,
        headers={"content-type": "application/json"},
    )
    oversized = client.post(
        "/atelier/api/v1/runs",
        content=exact_body + b" ",
        headers={"content-type": "application/json"},
    )

    assert_problem(within_limit, 422, "invalid-request")
    assert_problem(oversized, 422, "invalid-request")
    assert mutations.starts == []


@pytest.mark.parametrize(
    ("maximum_field_characters", "run_id"),
    [
        (12, "abcdefghijkl"),
        (10, "x"),
    ],
    ids=("public-reference-overflow", "maximum-cursor-only-overflow"),
)
def test_start_run_wire_identity_limits_reject_before_durable_work(
    maximum_field_characters: int, run_id: str
) -> None:
    mutations = RecordingMutationPorts()
    limited_fields = client_for(
        mutations,
        api_limits(maximum_field_characters=maximum_field_characters),
    )
    response = limited_fields.post(
        "/atelier/api/v1/runs",
        json={"run_id": run_id, "workflow_revision_hash": "0" * 64},
    )

    assert_problem(response, 422, "invalid-request")
    assert mutations.starts == []
    assert mutations.publications == []
    assert mutations.answers == []


def test_field_and_workflow_node_limits_reject_before_write() -> None:
    mutations = RecordingMutationPorts()

    node_limited = client_for(
        mutations,
        api_limits(maximum_workflow_nodes=1),
    )
    accepted = node_limited.post(
        "/atelier/api/v1/workflow-revisions",
        content=workflow_document(),
        headers={"content-type": "application/yaml"},
    )
    rejected = node_limited.post(
        "/atelier/api/v1/workflow-revisions",
        content=workflow_document(include_agent=True),
        headers={"content-type": "application/yaml"},
    )

    assert accepted.status_code == 201
    assert_problem(rejected, 422, "invalid-workflow-document")
    assert len(mutations.publications) == 1


def test_base64_and_decoded_payload_limits_reject_before_answer_write() -> None:
    mutations = RecordingMutationPorts()
    limits = api_limits(
        maximum_base64_characters=4,
        maximum_decoded_payload_bytes=2,
    )
    client = client_for(mutations, limits)
    path = (
        "/atelier/api/v1/runs/" + encode_public_run_reference(RunId("run")) + "/answers"
    )
    body = {
        "revision_hash": "0" * 64,
        "node_id": "wait",
        "answer_base64": encode_canonical_base64(b"12"),
    }

    exact = client.post(path, json=body)
    decoded_oversized = client.post(
        path,
        json={**body, "answer_base64": encode_canonical_base64(b"123")},
    )
    encoded_oversized = client.post(
        path,
        json={**body, "answer_base64": "AAAAA"},
    )

    assert exact.status_code == 404
    assert_problem(decoded_oversized, 422, "invalid-request")
    assert_problem(encoded_oversized, 422, "invalid-request")
    assert len(mutations.answers) == 1
