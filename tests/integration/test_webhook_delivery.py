"""Phase 1 (`#433`) end to end, behind a fake transport: no network, no loop.

The delivery decision (`deliver_attention_webhook`) is exercised against the
real DBOS store on both sides -- the attention feed it reads through the
unchanged `read_attention_events`, and the webhook delivery cursor it reads
and conditionally advances -- with only the outgoing call faked. The
same-second regression (`#627`) drives phase 2's `WebhookDeliveryLoop`
instead, because the exclusion set that breaks the livelock lives there.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from sqlalchemy.engine import Connection, Engine

from atelier2.adapters.dbos.instants import record_event_instant
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    initialize_schema,
    run_events,
    runs,
    workflow_revisions,
)
from atelier2.adapters.dbos.webhook_delivery import DbosWebhookDeliveryPublisher
from atelier2.application.deliver_attention_webhook import (
    AttentionWebhookDelivered,
    AttentionWebhookPermanentFailure,
    AttentionWebhookTransientFailure,
    NoAttentionEventsPending,
    deliver_attention_webhook,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventKind,
    WaitAnswerActor,
)
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.contracts.webhook_delivery import (
    WEBHOOK_DELIVERY_CURSOR_REVISION_INITIAL,
    AdvanceWebhookDeliveryCursor,
    CursorAdvanceConflict,
    WebhookDeliveryCursor,
    WebhookDeliveryCursorAdvanced,
    WebhookDeliveryCursorRevision,
    WebhookDeliveryCursorState,
    WebhookEventPayload,
)
from atelier2.contracts.when import RecordedAt
from atelier2.host.webhook_delivery import WebhookDeliveryLoop
from atelier2.ports.webhook_delivery import (
    AdvanceWebhookDeliveryCursorResult,
    ReadWebhookDeliveryCursorResult,
)
from atelier2.ports.webhook_transport import (
    Delivered,
    PermanentFailure,
    TransientFailure,
    WebhookDeliveryAttemptOutcome,
)
from tests.scenarios.api import durable_queries

SECRET = b"a-test-signing-secret"
FIRST_EVENT_AT = RecordedAt("2026-08-23T09:00:00Z")
NEXT_SECOND_AT = RecordedAt("2026-08-23T09:00:01Z")
WAIT_DOCUMENT = b"""format_version: 1
start: pause
nodes:
  - {id: pause, type: wait, prompt: Approve, output: approval, next: null}
"""


@dataclass(frozen=True)
class WebhookHarness:
    engine: Engine
    publisher: DbosWebhookDeliveryPublisher
    queries: DbosQueries


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[WebhookHarness]:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    try:
        yield WebhookHarness(
            engine, DbosWebhookDeliveryPublisher(engine), durable_queries(engine)
        )
    finally:
        engine.dispose()


@dataclass
class _RecordingWebhookTransport:
    """A scripted fake: answers each call from `outcomes`, in order, and
    records every payload it was asked to deliver."""

    outcomes: list[WebhookDeliveryAttemptOutcome]
    calls: list[WebhookEventPayload] = field(default_factory=list)

    def deliver(
        self, payload: WebhookEventPayload, payload_bytes: bytes, signature: bytes
    ) -> WebhookDeliveryAttemptOutcome:
        self.calls.append(payload)
        return self.outcomes[len(self.calls) - 1]


class _SimulatedCrash(RuntimeError):
    """The point a process crash would land: after a delivery is
    acknowledged, before its cursor write can happen."""


@dataclass
class _CrashesBeforeFirstCursorWrite:
    """Wraps a real publisher; the first `advance_cursor` call is never
    forwarded to the store -- the exact crash window between a delivery the
    transport already acknowledged and the cursor write that would record
    it -- so the wrapped store's durable state is provably unaffected."""

    real: DbosWebhookDeliveryPublisher

    def read_cursor(self) -> ReadWebhookDeliveryCursorResult:
        return self.real.read_cursor()

    def advance_cursor(
        self, command: AdvanceWebhookDeliveryCursor
    ) -> AdvanceWebhookDeliveryCursorResult:
        raise _SimulatedCrash("crashed before the cursor write could happen")


def _insert_attention_event(
    connection: Connection,
    run_id: RunId,
    revision: WorkflowRevision,
    *,
    at: RecordedAt,
    kind: RunEventKind = RunEventKind.WAITING_INPUT,
) -> RunEvent:
    node_id = "pause"
    event = RunEvent(
        run_id,
        revision.revision_hash,
        1,
        node_id,
        NodeExecutionId.for_node(run_id, revision.revision_hash, node_id),
        kind,
        b"",
        wait_answer_actor=(
            WaitAnswerActor.OPERATOR if kind is RunEventKind.WAITING_INPUT else None
        ),
    )
    connection.execute(
        runs.insert().values(
            run_id=run_id.value,
            bootstrap_workflow_id=f"workflow-{run_id.value}",
            revision_hash=revision.revision_hash.value,
            workflow_format_version=1,
            agent_binding_set_hash=None,
            current_node_id=node_id,
            current_round_ordinal=FIRST_ROUND_ORDINAL,
            state=(
                RunState.WAITING_INPUT.value
                if kind is RunEventKind.WAITING_INPUT
                else RunState.FAILED.value
            ),
            state_version=1,
            last_event_sequence=1,
            terminal_hash=(None if kind is RunEventKind.WAITING_INPUT else "0" * 64),
        )
    )
    connection.execute(
        run_events.insert().values(
            run_id=event.run_id.value,
            revision_hash=event.revision_hash.value,
            event_sequence=event.event_sequence,
            node_id=event.node_id,
            node_execution_id=event.node_execution_id.value,
            round_ordinal=event.round_ordinal,
            event_kind=event.event_kind.value,
            wait_answer_actor=(
                None
                if event.wait_answer_actor is None
                else event.wait_answer_actor.value
            ),
            payload=event.payload,
            payload_hash=event.payload_hash.value,
            receipt_logical_key=None,
            receipt_result_hash=None,
            event_hash=event.event_hash.value,
            agent_attempt_id=None,
            attempt_ordinal=None,
            cancellation_command_id=None,
            replacement=None,
        )
    )
    record_event_instant(connection, run_id.value, event.event_sequence, at=at)
    return event


def _seed_two_attention_events(
    engine: Engine,
    *,
    first_at: RecordedAt = FIRST_EVENT_AT,
    second_at: RecordedAt = NEXT_SECOND_AT,
) -> tuple[RunId, RunId]:
    revision = WorkflowRevision(WAIT_DOCUMENT)
    run_a, run_b = RunId("run-a"), RunId("run-b")
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision.revision_hash.value, document=revision.document
            )
        )
        _insert_attention_event(connection, run_a, revision, at=first_at)
        _insert_attention_event(connection, run_b, revision, at=second_at)
    return run_a, run_b


def _identity(payload: WebhookEventPayload) -> tuple[RunId, int]:
    return (payload.run_id, payload.event_sequence)


def test_two_new_events_are_each_delivered_once_and_the_cursor_lands_on_the_second(
    harness: WebhookHarness,
) -> None:
    run_a, run_b = _seed_two_attention_events(harness.engine)
    transport = _RecordingWebhookTransport([Delivered(), Delivered()])

    first = deliver_attention_webhook(
        harness.publisher, harness.queries, transport, SECRET
    )
    second = deliver_attention_webhook(
        harness.publisher, harness.queries, transport, SECRET
    )
    third = deliver_attention_webhook(
        harness.publisher, harness.queries, transport, SECRET
    )

    assert isinstance(first, AttentionWebhookDelivered)
    assert _identity(first.payload) == (run_a, 1)
    assert isinstance(second, AttentionWebhookDelivered)
    assert _identity(second.payload) == (run_b, 1)
    assert third == NoAttentionEventsPending()
    assert [_identity(payload) for payload in transport.calls] == [
        (run_a, 1),
        (run_b, 1),
    ]
    assert harness.publisher.read_cursor() == WebhookDeliveryCursorState(
        WebhookDeliveryCursor(run_b, 1), WebhookDeliveryCursorRevision(2)
    )


def test_two_events_sharing_one_second_are_each_delivered_once_and_the_feed_continues(
    harness: WebhookHarness,
) -> None:
    run_a, run_b = _seed_two_attention_events(harness.engine, second_at=FIRST_EVENT_AT)
    transport = _RecordingWebhookTransport([Delivered(), Delivered()])
    loop = WebhookDeliveryLoop(harness.publisher, harness.queries, transport, SECRET)

    loop.deliver_pending()
    # An idle poll pass must not redeliver the same-second sibling either.
    loop.deliver_pending()

    assert [_identity(payload) for payload in transport.calls] == [
        (run_a, 1),
        (run_b, 1),
    ]
    assert harness.publisher.read_cursor() == WebhookDeliveryCursorState(
        WebhookDeliveryCursor(run_b, 1), WebhookDeliveryCursorRevision(2)
    )

    run_c = RunId("run-c")
    with harness.engine.begin() as connection:
        _insert_attention_event(
            connection,
            run_c,
            WorkflowRevision(WAIT_DOCUMENT),
            at=RecordedAt("2026-08-23T09:00:02Z"),
        )
    transport.outcomes.append(Delivered())
    loop.deliver_pending()

    assert [_identity(payload) for payload in transport.calls] == [
        (run_a, 1),
        (run_b, 1),
        (run_c, 1),
    ]
    assert harness.publisher.read_cursor() == WebhookDeliveryCursorState(
        WebhookDeliveryCursor(run_c, 1), WebhookDeliveryCursorRevision(3)
    )


def test_a_transient_failure_holds_the_cursor_and_a_retry_redelivers_the_same_event(
    harness: WebhookHarness,
) -> None:
    run_a, run_b = _seed_two_attention_events(harness.engine)
    transport = _RecordingWebhookTransport(
        [Delivered(), TransientFailure("subscriber timed out")]
    )

    first = deliver_attention_webhook(
        harness.publisher, harness.queries, transport, SECRET
    )
    second = deliver_attention_webhook(
        harness.publisher, harness.queries, transport, SECRET
    )

    assert isinstance(first, AttentionWebhookDelivered)
    assert _identity(first.payload) == (run_a, 1)
    assert isinstance(second, AttentionWebhookTransientFailure)
    assert second == AttentionWebhookTransientFailure(
        transport.calls[1], "subscriber timed out"
    )
    assert _identity(second.payload) == (run_b, 1)
    assert harness.publisher.read_cursor() == WebhookDeliveryCursorState(
        WebhookDeliveryCursor(run_a, 1), WebhookDeliveryCursorRevision(1)
    )

    transport.outcomes.append(Delivered())
    third = deliver_attention_webhook(
        harness.publisher, harness.queries, transport, SECRET
    )

    assert isinstance(third, AttentionWebhookDelivered)
    assert _identity(third.payload) == (run_b, 1)
    assert [_identity(payload) for payload in transport.calls] == [
        (run_a, 1),
        (run_b, 1),
        (run_b, 1),
    ]
    assert harness.publisher.read_cursor() == WebhookDeliveryCursorState(
        WebhookDeliveryCursor(run_b, 1), WebhookDeliveryCursorRevision(2)
    )


def test_a_permanent_failure_also_holds_the_cursor(harness: WebhookHarness) -> None:
    run_a, _run_b = _seed_two_attention_events(harness.engine)
    transport = _RecordingWebhookTransport([PermanentFailure("payload rejected")])

    outcome = deliver_attention_webhook(
        harness.publisher, harness.queries, transport, SECRET
    )

    assert isinstance(outcome, AttentionWebhookPermanentFailure)
    assert outcome == AttentionWebhookPermanentFailure(
        transport.calls[0], "payload rejected"
    )
    assert _identity(outcome.payload) == (run_a, 1)
    assert harness.publisher.read_cursor() == WebhookDeliveryCursorState(
        None, WEBHOOK_DELIVERY_CURSOR_REVISION_INITIAL
    )


def test_a_crash_between_a_delivered_answer_and_the_cursor_write_redelivers_the_same_event(
    harness: WebhookHarness,
) -> None:
    run_a, _run_b = _seed_two_attention_events(harness.engine)
    transport = _RecordingWebhookTransport([Delivered(), Delivered()])
    crashing = _CrashesBeforeFirstCursorWrite(harness.publisher)

    with pytest.raises(_SimulatedCrash):
        deliver_attention_webhook(crashing, harness.queries, transport, SECRET)

    assert [_identity(payload) for payload in transport.calls] == [(run_a, 1)]
    assert harness.publisher.read_cursor() == WebhookDeliveryCursorState(
        None, WEBHOOK_DELIVERY_CURSOR_REVISION_INITIAL
    )

    restarted = deliver_attention_webhook(
        harness.publisher, harness.queries, transport, SECRET
    )

    assert isinstance(restarted, AttentionWebhookDelivered)
    assert _identity(restarted.payload) == (run_a, 1)
    assert [_identity(payload) for payload in transport.calls] == [
        (run_a, 1),
        (run_a, 1),
    ]
    assert harness.publisher.read_cursor() == WebhookDeliveryCursorState(
        WebhookDeliveryCursor(run_a, 1), WebhookDeliveryCursorRevision(1)
    )


def test_two_advance_attempts_from_the_same_revision_leave_exactly_one_winner(
    harness: WebhookHarness,
) -> None:
    run_a, _run_b = _seed_two_attention_events(harness.engine)
    to = WebhookDeliveryCursor(run_a, 1)
    command = AdvanceWebhookDeliveryCursor(to, WebhookDeliveryCursorRevision(0))

    winner = harness.publisher.advance_cursor(command)
    loser = harness.publisher.advance_cursor(command)

    assert winner == WebhookDeliveryCursorAdvanced(to, WebhookDeliveryCursorRevision(1))
    assert loser == CursorAdvanceConflict(
        WebhookDeliveryCursorRevision(0), WebhookDeliveryCursorRevision(1)
    )
    assert harness.publisher.read_cursor() == WebhookDeliveryCursorState(
        to, WebhookDeliveryCursorRevision(1)
    )
