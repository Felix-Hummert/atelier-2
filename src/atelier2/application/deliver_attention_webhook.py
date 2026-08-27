"""Delivering one attention event to a webhook subscriber, as a decision.

Phase 1 of `#433` builds this decision behind a fake transport: no network,
no loop, no configuration. What one call here does is read where delivery
stands, read the one attention event after it through the existing feed
(`read_attention_events`, unchanged), build and sign that event's payload,
hand it to a transport, and move the cursor on if and only if the transport
answered `Delivered`. A loop that calls this repeatedly, and a real
transport, are phase 2's.

At-least-once follows from that ordering alone: the cursor never advances
before delivery succeeds, so a crash between a successful delivery and the
cursor write reappears next call as the same event, identified the same way,
delivered again.

The cursor holds exactly one identity, so within one recorded-at second it
cannot tell delivered siblings from pending ones: the caller passes the
identities it already delivered at the cursor's instant as
`excluded_identities` -- the same-instant exclusion the SSE stream uses.
Losing that set (a restart) redelivers same-second siblings, a duplicate
at-least-once already permits, never a loss.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.read_attention_events import (
    AttentionCursorUnknown,
    AttentionEventPageOversized,
    AttentionEventsRead,
    read_attention_events,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.runs import RunId
from atelier2.contracts.webhook_delivery import (
    AdvanceWebhookDeliveryCursor,
    CursorAdvanceConflict,
    WebhookDeliveryCursor,
    WebhookDeliveryCursorAdvanced,
    WebhookDeliveryCursorState,
    WebhookEventPayload,
    sign_webhook_payload,
    webhook_payload_bytes,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import (
    DurableWriteUnavailable as PortDurableWriteUnavailable,
)
from atelier2.ports.run_events import (
    AttentionEvent,
    AttentionEventCorrupt,
    RunEventQueries,
)
from atelier2.ports.webhook_delivery import WebhookDeliveryPublisher
from atelier2.ports.webhook_transport import (
    Delivered,
    PermanentFailure,
    TransientFailure,
    WebhookTransport,
)


@dataclass(frozen=True, slots=True)
class AttentionWebhookDelivered:
    """This call delivered exactly this payload and advanced the cursor to it."""

    payload: WebhookEventPayload


@dataclass(frozen=True, slots=True)
class NoAttentionEventsPending:
    """The cursor is already at the head of the attention feed."""


@dataclass(frozen=True, slots=True)
class AttentionWebhookTransientFailure:
    """Delivery did not land; the cursor did not move, so a retry redelivers it."""

    payload: WebhookEventPayload
    detail: str


@dataclass(frozen=True, slots=True)
class AttentionWebhookPermanentFailure:
    """The subscriber refused this payload outright; the cursor did not move.

    Phase 1 names no dead-letter surface, so this is the same head-of-line
    blocking a transient failure leaves behind -- ratified as this slice's
    accepted behaviour rather than left as an accident.
    """

    payload: WebhookEventPayload
    detail: str


@dataclass(frozen=True, slots=True)
class AttentionWebhookCursorConflict:
    """This call delivered its event, but another writer advanced the cursor
    first. The delivery already happened; nothing here was lost."""


type DeliverAttentionWebhookResult = (
    AttentionWebhookDelivered
    | NoAttentionEventsPending
    | AttentionWebhookTransientFailure
    | AttentionWebhookPermanentFailure
    | AttentionWebhookCursorConflict
    | ReadUnavailable
    | ProjectionTooLarge
    | DurableStateCorrupt
    | WriteUnavailable
)


def deliver_attention_webhook(
    cursor_publisher: WebhookDeliveryPublisher,
    queries: RunEventQueries,
    transport: WebhookTransport,
    signing_key: bytes,
    excluded_identities: tuple[tuple[RunId, int], ...] = (),
) -> DeliverAttentionWebhookResult:
    """Deliver the one attention event after the durable cursor, or say why not.

    `excluded_identities` are events the caller already delivered at the
    cursor's recorded-at instant; without them, two events sharing one second
    would pingpong the single-identity cursor forever.
    """

    cursor_read = cursor_publisher.read_cursor()
    match cursor_read:
        case WebhookDeliveryCursorState() as cursor_state:
            pass
        case PortDurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    after_run_id = cursor_state.cursor.run_id if cursor_state.cursor else None
    after_sequence = cursor_state.cursor.event_sequence if cursor_state.cursor else None

    # One decision delivers one event; a repeated call is phase 2's loop.
    page = read_attention_events(
        after_run_id, after_sequence, 1, queries, excluded_identities
    )
    match page:
        case AttentionEventsRead(events):
            pass
        case AttentionCursorUnknown():
            return DurableStateCorrupt()
        case AttentionEventPageOversized():
            return DurableStateCorrupt()
        case ReadUnavailable():
            return page
        case ProjectionTooLarge():
            return page
        case DurableStateCorrupt():
            return page
        case _ as unreachable:
            assert_never(unreachable)
    if not events:
        return NoAttentionEventsPending()

    match events[0]:
        case AttentionEvent(event=persisted, recorded_at=recorded_at):
            event = persisted.event
        case AttentionEventCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    payload = WebhookEventPayload(
        run_id=event.run_id,
        event_sequence=event.event_sequence,
        event_kind=event.event_kind,
        node_id=event.node_id,
        recorded_at=recorded_at,
    )
    payload_bytes = webhook_payload_bytes(payload)
    signature = sign_webhook_payload(signing_key, payload_bytes)

    attempt = transport.deliver(payload, payload_bytes, signature)
    match attempt:
        case Delivered():
            pass
        case TransientFailure(detail):
            return AttentionWebhookTransientFailure(payload, detail)
        case PermanentFailure(detail):
            return AttentionWebhookPermanentFailure(payload, detail)
        case _ as unreachable:
            assert_never(unreachable)

    advance = cursor_publisher.advance_cursor(
        AdvanceWebhookDeliveryCursor(
            to=WebhookDeliveryCursor(event.run_id, event.event_sequence),
            expected_revision=cursor_state.revision,
        )
    )
    match advance:
        case WebhookDeliveryCursorAdvanced():
            return AttentionWebhookDelivered(payload)
        case CursorAdvanceConflict():
            return AttentionWebhookCursorConflict()
        case PortDurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
