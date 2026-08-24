"""`#433` phase 2: the delivery loop, its settings, and its lifespan.

Phase 1 owns the durable delivery decision against the real store; this module
owns what phase 2 added around it -- the background loop that drives that
decision through the real signing and HTTP edge, the all-or-nothing settings
that turn it on, the once-at-startup signing-key read, and the lifespan that
starts and stops the loop with the served instance. The store is faked
in-memory here so the loop, the real transport and the real HMAC signing are
what each test exercises; no socket is opened (an `httpx.MockTransport` scripts
the receiver and captures every request).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from atelier2.adapters.http_webhook_transport import (
    SIGNATURE_HEADER,
    HttpWebhookTransport,
)
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.run_events import PersistedRunEvent
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.contracts.webhook_delivery import (
    WEBHOOK_DELIVERY_CURSOR_REVISION_INITIAL,
    AdvanceWebhookDeliveryCursor,
    CursorAdvanceConflict,
    WebhookDeliveryCursor,
    WebhookDeliveryCursorAdvanced,
    WebhookDeliveryCursorState,
)
from atelier2.contracts.when import RecordedAt
from atelier2.host.webhook_delivery import (
    WebhookDeliveryLoop,
    WebhookDeliverySettings,
    resolve_signing_key,
    webhook_delivery_lifespan,
)
from atelier2.ports.run_events import (
    AttentionEvent,
    AttentionEventPage,
    PrepareRunEventStreamResult,
    ReadAttentionEventPageResult,
    ReadRunEventPageResult,
)
from atelier2.ports.webhook_delivery import (
    AdvanceWebhookDeliveryCursorResult,
    ReadWebhookDeliveryCursorResult,
)
from tests.host.test_local_host import main, serve_arguments

TARGET_URL = "https://receiver.example/atelier-webhook"
# Scenario data, not a real signing key: this test owns both ends, so it can
# assert the exact HMAC the loop must produce for these bytes.
SIGNING_KEY = b"loop-scenario-signing-key"
_DELIVERY_THREAD_NAME = "atelier2-webhook-delivery"
_WAIT_DOCUMENT = b"""format_version: 1
start: pause
nodes:
  - {id: pause, type: wait, prompt: Approve, output: approval, next: null}
"""


def _attention_event(
    run_id: str, kind: RunEventKind, recorded_at: str
) -> AttentionEvent:
    revision = WorkflowRevision(_WAIT_DOCUMENT)
    node_id = "pause"
    identity = RunId(run_id)
    event = RunEvent(
        identity,
        revision.revision_hash,
        1,
        node_id,
        NodeExecutionId.for_node(identity, revision.revision_hash, node_id),
        kind,
        b"",
    )
    return AttentionEvent(PersistedRunEvent(event, None), RecordedAt(recorded_at))


class _InMemoryCursorPublisher:
    """The durable cursor's CAS behaviour, held in memory for the loop tests."""

    def __init__(self) -> None:
        self.state = WebhookDeliveryCursorState(
            None, WEBHOOK_DELIVERY_CURSOR_REVISION_INITIAL
        )

    def read_cursor(self) -> ReadWebhookDeliveryCursorResult:
        return self.state

    def advance_cursor(
        self, command: AdvanceWebhookDeliveryCursor
    ) -> AdvanceWebhookDeliveryCursorResult:
        outcome = self.state.advance(command)
        if isinstance(outcome, WebhookDeliveryCursorAdvanced):
            self.state = WebhookDeliveryCursorState(outcome.cursor, outcome.revision)
        return outcome


class _ConflictingCursorPublisher:
    """A second writer wins every CAS: every advance loses the race."""

    def __init__(self) -> None:
        self._state = WebhookDeliveryCursorState(
            None, WEBHOOK_DELIVERY_CURSOR_REVISION_INITIAL
        )

    def read_cursor(self) -> ReadWebhookDeliveryCursorResult:
        return self._state

    def advance_cursor(
        self, command: AdvanceWebhookDeliveryCursor
    ) -> AdvanceWebhookDeliveryCursorResult:
        return CursorAdvanceConflict(
            command.expected_revision, WEBHOOK_DELIVERY_CURSOR_REVISION_INITIAL
        )


class _InMemoryAttentionQueries:
    """The attention feed after a cursor, ordered as the events were handed in."""

    def __init__(self, events: tuple[AttentionEvent, ...]) -> None:
        self._events = events

    def read_attention_event_page(
        self,
        after_run_id: RunId | None,
        after_sequence: int | None,
        limit: int,
        excluded_identities: tuple[tuple[RunId, int], ...],
    ) -> ReadAttentionEventPageResult:
        start = 0
        if after_run_id is not None:
            for index, attention in enumerate(self._events):
                event = attention.event.event
                if (event.run_id, event.event_sequence) == (
                    after_run_id,
                    after_sequence,
                ):
                    start = index + 1
                    break
        return AttentionEventPage(self._events[start : start + limit])

    def prepare_run_event_stream(
        self, run_id: RunId, after_sequence: int
    ) -> PrepareRunEventStreamResult:
        raise NotImplementedError

    def read_run_event_page(
        self, run_id: RunId, after_sequence: int, limit: int
    ) -> ReadRunEventPageResult:
        raise NotImplementedError


def _capturing_transport(
    captured: list[httpx.Request], status_code: int = 200
) -> HttpWebhookTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(status_code)

    return HttpWebhookTransport(
        httpx.Client(transport=httpx.MockTransport(handler)), TARGET_URL
    )


def _expected_signature_header(payload_bytes: bytes) -> str:
    digest = hmac.new(SIGNING_KEY, payload_bytes, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def test_the_loop_delivers_each_pending_attention_event_once_and_signs_it() -> None:
    waiting = _attention_event(
        "run-a", RunEventKind.WAITING_INPUT, "2026-08-23T09:00:00Z"
    )
    failed = _attention_event(
        "run-b", RunEventKind.AGENT_FAILED, "2026-08-23T09:00:01Z"
    )
    captured: list[httpx.Request] = []
    publisher = _InMemoryCursorPublisher()
    loop = WebhookDeliveryLoop(
        publisher,
        _InMemoryAttentionQueries((waiting, failed)),
        _capturing_transport(captured),
        SIGNING_KEY,
    )

    loop.deliver_pending()

    assert [request.url.path for request in captured] == [
        "/atelier-webhook",
        "/atelier-webhook",
    ]
    for request in captured:
        assert request.headers[SIGNATURE_HEADER] == _expected_signature_header(
            request.content
        )
    # Two deliveries advance the cursor twice, landing on the second event.
    assert publisher.state.cursor == WebhookDeliveryCursor(RunId("run-b"), 1)
    assert publisher.state.revision.value == 2


def test_a_transient_failure_holds_the_cursor_and_the_next_pass_redelivers() -> None:
    waiting = _attention_event(
        "run-a", RunEventKind.WAITING_INPUT, "2026-08-23T09:00:00Z"
    )
    publisher = _InMemoryCursorPublisher()
    failing: list[httpx.Request] = []
    stuck_loop = WebhookDeliveryLoop(
        publisher,
        _InMemoryAttentionQueries((waiting,)),
        _capturing_transport(failing, status_code=503),
        SIGNING_KEY,
    )

    stuck_loop.deliver_pending()

    assert len(failing) == 1
    assert publisher.state.cursor is None

    recovered: list[httpx.Request] = []
    healthy_loop = WebhookDeliveryLoop(
        publisher,
        _InMemoryAttentionQueries((waiting,)),
        _capturing_transport(recovered),
        SIGNING_KEY,
    )

    healthy_loop.deliver_pending()

    assert len(recovered) == 1
    assert publisher.state.cursor == WebhookDeliveryCursor(RunId("run-a"), 1)


def test_a_cursor_conflict_is_logged_as_a_racing_writer_not_unreadable_state(
    caplog: pytest.LogCaptureFixture,
) -> None:
    waiting = _attention_event(
        "run-a", RunEventKind.WAITING_INPUT, "2026-08-23T09:00:00Z"
    )
    loop = WebhookDeliveryLoop(
        _ConflictingCursorPublisher(),
        _InMemoryAttentionQueries((waiting,)),
        _capturing_transport([]),
        SIGNING_KEY,
    )

    with caplog.at_level(logging.INFO, logger="atelier2"):
        loop.deliver_pending()

    messages = [record.getMessage() for record in caplog.records]
    assert any("advanced by another writer" in message for message in messages)
    assert not any("could not read durable state" in message for message in messages)


def test_an_unexpected_error_on_one_pass_delays_delivery_but_does_not_kill_the_loop() -> (
    None
):
    waiting = _attention_event(
        "run-a", RunEventKind.WAITING_INPUT, "2026-08-23T09:00:00Z"
    )
    publisher = _InMemoryCursorPublisher()
    delivered = threading.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        delivered.set()
        return httpx.Response(200)

    transport = HttpWebhookTransport(
        httpx.Client(transport=httpx.MockTransport(handler)), TARGET_URL
    )

    class _FaultOnFirstReadQueries:
        """A transient adapter bug: the first read raises, later reads recover."""

        def __init__(self) -> None:
            self._delegate = _InMemoryAttentionQueries((waiting,))
            self.faulted = threading.Event()
            self.cursor_during_fault: WebhookDeliveryCursor | None = None

        def read_attention_event_page(
            self,
            after_run_id: RunId | None,
            after_sequence: int | None,
            limit: int,
            excluded_identities: tuple[tuple[RunId, int], ...],
        ) -> ReadAttentionEventPageResult:
            if not self.faulted.is_set():
                self.cursor_during_fault = publisher.state.cursor
                self.faulted.set()
                raise RuntimeError("transient adapter bug")
            return self._delegate.read_attention_event_page(
                after_run_id, after_sequence, limit, excluded_identities
            )

        def prepare_run_event_stream(
            self, run_id: RunId, after_sequence: int
        ) -> PrepareRunEventStreamResult:
            raise NotImplementedError

        def read_run_event_page(
            self, run_id: RunId, after_sequence: int, limit: int
        ) -> ReadRunEventPageResult:
            raise NotImplementedError

    queries = _FaultOnFirstReadQueries()
    loop = WebhookDeliveryLoop(
        publisher,
        queries,
        transport,
        SIGNING_KEY,
        poll_interval_seconds=0.01,
    )

    loop.start()
    try:
        assert delivered.wait(2.0)
    finally:
        loop.stop()

    assert queries.faulted.is_set()
    assert queries.cursor_during_fault is None
    assert publisher.state.cursor == WebhookDeliveryCursor(RunId("run-a"), 1)


def test_the_signing_key_never_appears_in_a_log_line_or_on_the_wire(
    caplog: pytest.LogCaptureFixture,
) -> None:
    waiting = _attention_event(
        "run-a", RunEventKind.WAITING_INPUT, "2026-08-23T09:00:00Z"
    )
    captured: list[httpx.Request] = []
    loop = WebhookDeliveryLoop(
        _InMemoryCursorPublisher(),
        _InMemoryAttentionQueries((waiting,)),
        _capturing_transport(captured, status_code=503),
        SIGNING_KEY,
    )

    with caplog.at_level(logging.WARNING, logger="atelier2"):
        loop.deliver_pending()

    assert caplog.records
    key_text = SIGNING_KEY.decode()
    for record in caplog.records:
        assert key_text not in record.getMessage()
    for request in captured:
        assert SIGNING_KEY not in request.content
        assert key_text not in "".join(request.headers.values())


def test_the_delivery_loop_starts_and_stops_without_leaving_an_orphaned_thread() -> (
    None
):
    delivered = threading.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        delivered.set()
        return httpx.Response(200)

    transport = HttpWebhookTransport(
        httpx.Client(transport=httpx.MockTransport(handler)), TARGET_URL
    )
    loop = WebhookDeliveryLoop(
        _InMemoryCursorPublisher(),
        _InMemoryAttentionQueries(
            (
                _attention_event(
                    "run-a", RunEventKind.WAITING_INPUT, "2026-08-23T09:00:00Z"
                ),
            )
        ),
        transport,
        SIGNING_KEY,
    )

    loop.start()
    assert delivered.wait(2.0)
    loop.stop()

    assert not _delivery_thread_alive()
    # A clean stop leaves the loop restartable rather than "already started".
    loop.start()
    loop.stop()
    assert not _delivery_thread_alive()


def test_starting_an_already_running_loop_is_refused() -> None:
    loop = WebhookDeliveryLoop(
        _InMemoryCursorPublisher(),
        _InMemoryAttentionQueries(()),
        _capturing_transport([]),
        SIGNING_KEY,
    )

    loop.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            loop.start()
    finally:
        loop.stop()


def test_the_lifespan_runs_the_loop_and_stops_it_when_the_app_shuts_down() -> None:
    delivered = threading.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        delivered.set()
        return httpx.Response(200)

    transport = HttpWebhookTransport(
        httpx.Client(transport=httpx.MockTransport(handler)), TARGET_URL
    )
    publisher = _InMemoryCursorPublisher()
    loop = WebhookDeliveryLoop(
        publisher,
        _InMemoryAttentionQueries(
            (
                _attention_event(
                    "run-a", RunEventKind.WAITING_INPUT, "2026-08-23T09:00:00Z"
                ),
            )
        ),
        transport,
        SIGNING_KEY,
    )
    lifespan = webhook_delivery_lifespan(loop, transport)

    async def drive() -> None:
        async with lifespan(FastAPI()):
            assert delivered.wait(2.0)

    asyncio.run(drive())

    assert not _delivery_thread_alive()
    assert publisher.state.cursor == WebhookDeliveryCursor(RunId("run-a"), 1)


def _delivery_thread_alive() -> bool:
    return any(
        thread.name == _DELIVERY_THREAD_NAME and thread.is_alive()
        for thread in threading.enumerate()
    )


@pytest.mark.parametrize("target_url", ("", "ftp://x", "not-a-url", "/relative/path"))
def test_a_target_url_that_is_not_an_absolute_http_url_is_refused(
    target_url: str, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="absolute http or https url"):
        WebhookDeliverySettings(target_url, tmp_path / "signing.key")


def test_resolve_signing_key_reads_the_file_once_and_returns_its_bytes(
    tmp_path: Path,
) -> None:
    key_file = tmp_path / "signing.key"
    key_file.write_text("a-key-value\n")

    assert resolve_signing_key(key_file) == b"a-key-value"


def test_an_empty_signing_key_file_fails_loud(tmp_path: Path) -> None:
    key_file = tmp_path / "signing.key"
    key_file.write_text("   \n")

    with pytest.raises(ValueError, match="empty"):
        resolve_signing_key(key_file)


def test_a_missing_signing_key_file_fails_loud(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="did not resolve"):
        resolve_signing_key(tmp_path / "absent.key")


@pytest.mark.parametrize(
    "webhook_flags",
    (
        ("--webhook-url", TARGET_URL),
        ("--webhook-signing-key-file", "signing.key"),
    ),
)
def test_a_half_configured_webhook_is_refused_at_the_command_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], webhook_flags: tuple[str, ...]
) -> None:
    with pytest.raises(SystemExit) as refusal:
        main(serve_arguments(tmp_path, *webhook_flags))

    assert refusal.value.code == 2
    assert "declared together or not at all" in capsys.readouterr().err
