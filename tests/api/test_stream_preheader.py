from __future__ import annotations

import http.client
import json
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from threading import Thread
from typing import cast

import pytest
import uvicorn

from atelier2.api.app import ApiPorts, create_app
from atelier2.api.references import encode_event_cursor, encode_public_run_reference
from atelier2.application.start_published_run import DurablePublishedRunStarter
from atelier2.contracts.runs import RunId
from atelier2.ports.durable_runs import TransactionalWaitAnswerer
from atelier2.ports.effects import TransactionalEffectReconcileCommander
from atelier2.ports.run_events import (
    CursorAhead,
    EventHistoryCorrupt,
    PrepareRunEventStreamResult,
    ReadRunEventPageResult,
    RunEventQueries,
    StreamReady,
)
from atelier2.ports.run_queries import RunQueries, RunQueryMissing
from atelier2.ports.workflow_revisions import (
    WorkflowRevisionPublisher,
    WorkflowRevisionQueries,
)
from tests.scenarios.api import api_limits, event_poll_backoff


class FakeEventQueries:
    def __init__(self, prepared: PrepareRunEventStreamResult) -> None:
        self.prepared = prepared
        self.prepare_calls = 0
        self.page_calls = 0

    def prepare_run_event_stream(
        self, run_id: RunId, after_sequence: int
    ) -> PrepareRunEventStreamResult:
        del run_id, after_sequence
        self.prepare_calls += 1
        return self.prepared

    def read_run_event_page(
        self, run_id: RunId, after_sequence: int, limit: int
    ) -> ReadRunEventPageResult:
        del run_id, after_sequence, limit
        self.page_calls += 1
        raise AssertionError("pre-header rejection started event paging")


def app_for(queries: FakeEventQueries):
    unused = object()
    return create_app(
        source_commit="commit",
        source_tree="tree",
        ports=ApiPorts(
            cast(WorkflowRevisionPublisher, unused),
            cast(DurablePublishedRunStarter, unused),
            cast(TransactionalWaitAnswerer, unused),
            cast(TransactionalEffectReconcileCommander, unused),
            cast(WorkflowRevisionQueries, unused),
            cast(RunQueries, unused),
            cast(RunEventQueries, queries),
        ),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
    )


@contextmanager
def live_server(queries: FakeEventQueries) -> Iterator[int]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(
                app_for(queries),
                host="127.0.0.1",
                port=0,
                log_level="critical",
                access_log=False,
                lifespan="off",
            )
        )
        thread = Thread(
            target=server.run,
            kwargs={"sockets": [listener]},
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=5)
            raise AssertionError("Uvicorn did not start for raw pre-header proof")
        try:
            yield port
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def raw_get(
    port: int, path: str, headers: tuple[tuple[str, str], ...] = ()
) -> tuple[int, str, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest("GET", path)
    for name, value in headers:
        connection.putheader(name, value)
    connection.endheaders()
    response = connection.getresponse()
    result = (response.status, response.headers.get_content_type(), response.read())
    connection.close()
    return result


@pytest.mark.parametrize(
    ("path", "headers", "prepared", "status", "code"),
    [
        (
            "not-a-reference",
            {},
            StreamReady(0, True, 0),
            400,
            "invalid-public-run-reference",
        ),
        (
            encode_public_run_reference(RunId("run")),
            {},
            RunQueryMissing(),
            404,
            "run-not-found",
        ),
        (
            encode_public_run_reference(RunId("run")),
            {"last-event-id": "not-a-cursor"},
            StreamReady(0, True, 0),
            400,
            "invalid-event-cursor",
        ),
        (
            encode_public_run_reference(RunId("run")),
            {"last-event-id": encode_event_cursor(RunId("other"), 1)},
            StreamReady(1, True, 1),
            409,
            "event-cursor-run-mismatch",
        ),
        (
            encode_public_run_reference(RunId("run")),
            {"last-event-id": encode_event_cursor(RunId("run"), 2)},
            CursorAhead(),
            409,
            "event-cursor-ahead",
        ),
        (
            encode_public_run_reference(RunId("run")),
            {},
            EventHistoryCorrupt(),
            500,
            "durable-state-corrupt",
        ),
        (
            encode_public_run_reference(RunId("run")),
            {"accept": "application/json"},
            StreamReady(0, True, 0),
            406,
            "not-acceptable",
        ),
        (
            encode_public_run_reference(RunId("run")),
            {"accept": "text/event-stream;q=0.000"},
            StreamReady(0, True, 0),
            406,
            "not-acceptable",
        ),
    ],
)
def test_stream_errors_are_decided_before_event_paging(
    path: str,
    headers: dict[str, str],
    prepared: PrepareRunEventStreamResult,
    status: int,
    code: str,
) -> None:
    queries = FakeEventQueries(prepared)

    with live_server(queries) as port:
        response_status, content_type, body = raw_get(
            port,
            f"/atelier/api/v1/runs/{path}/events",
            tuple(headers.items()),
        )

    assert response_status == status
    assert content_type == "application/problem+json"
    assert json.loads(body)["type"].endswith(":" + code)
    assert queries.page_calls == 0


def test_multiple_last_event_id_headers_are_rejected_before_prepare() -> None:
    run_id = RunId("run")
    queries = FakeEventQueries(StreamReady(0, True, 0))
    path = f"/atelier/api/v1/runs/{encode_public_run_reference(run_id)}/events"

    with live_server(queries) as port:
        status, content_type, body = raw_get(
            port,
            path,
            (
                ("Last-Event-ID", encode_event_cursor(run_id, 1)),
                ("Last-Event-ID", encode_event_cursor(run_id, 2)),
            ),
        )

    assert status == 400
    assert content_type == "application/problem+json"
    assert json.loads(body)["type"].endswith(":invalid-event-cursor")
    assert queries.prepare_calls == 0
    assert queries.page_calls == 0


def test_terminal_head_cursor_returns_empty_event_stream() -> None:
    run_id = RunId("run")
    queries = FakeEventQueries(StreamReady(2, True, 2))

    with live_server(queries) as port:
        status, content_type, body = raw_get(
            port,
            f"/atelier/api/v1/runs/{encode_public_run_reference(run_id)}/events",
            (("Last-Event-ID", encode_event_cursor(run_id, 2)),),
        )

    assert status == 200
    assert content_type == "text/event-stream"
    assert body == b""
    assert queries.page_calls == 0
