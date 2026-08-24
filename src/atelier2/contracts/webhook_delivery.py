"""The webhook delivery decision's own durable identity, payload and signature.

`#433` splits the webhook feature into a delivery decision and a network edge.
This module is the decision's vocabulary: the cursor position one delivered
attention event leaves behind, what a subscriber is handed for one event, and
the byte rule that payload is signed under. It names no URL, no header and no
retry policy -- those belong to `ports.webhook_transport` (phase 1: a
protocol only) and to phase 2's real transport.

At-least-once is the only guarantee this contract makes: a cursor advances
after a delivery succeeds and never before, so a crash between "delivered"
and "cursor written" reappears as the same event delivered again, identified
by the pair a receiver dedups on -- `run_id` and `event_sequence`, the same
identity `read_attention_events` already resumes by.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Final

from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.runs import RunId
from atelier2.contracts.when import RecordedAt


@dataclass(frozen=True, slots=True)
class WebhookDeliveryCursor:
    """One attention event's identity, as the delivery cursor sits on it.

    The same `(run_id, event_sequence)` pair `read_attention_events` resumes
    a stream by, so a delivered position always names a real attention event
    the store can look up again.
    """

    run_id: RunId
    event_sequence: int

    def __post_init__(self) -> None:
        if type(self.event_sequence) is not int or self.event_sequence <= 0:
            raise ValueError(
                "a webhook delivery cursor's event sequence must be a positive integer"
            )


@dataclass(frozen=True, slots=True)
class WebhookDeliveryCursorRevision:
    """How many durable advances the webhook delivery cursor has made.

    The CAS token an advance names, so a caller that read a stale cursor is
    refused rather than silently overwriting an advance it never saw -- the
    same optimistic-concurrency shape `QueueProjectionRevision` already
    carries for the queue's own admission CAS.
    """

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int or self.value < 0:
            raise ValueError(
                "a webhook delivery cursor revision must be a nonnegative advance count"
            )


WEBHOOK_DELIVERY_CURSOR_REVISION_INITIAL: Final = WebhookDeliveryCursorRevision(0)


@dataclass(frozen=True, slots=True)
class WebhookDeliveryCursorState:
    """What reading the cursor answers: its position, or none before the
    first delivery, together with the revision an advance must name to win."""

    cursor: WebhookDeliveryCursor | None
    revision: WebhookDeliveryCursorRevision

    def advance(
        self, command: AdvanceWebhookDeliveryCursor
    ) -> AdvanceWebhookDeliveryCursorOutcome:
        """The one legal transition this cursor owns: move to a newly
        delivered event, guarded by the revision the caller read before it
        delivered.

        Pure and store-neutral: an adapter reads this state, calls this
        method, and only then attempts the CAS write the outcome names --
        exactly the shape `QueueItemSnapshot.admit` already established.
        """

        if self.revision != command.expected_revision:
            return CursorAdvanceConflict(command.expected_revision, self.revision)
        return WebhookDeliveryCursorAdvanced(
            command.to, WebhookDeliveryCursorRevision(self.revision.value + 1)
        )


@dataclass(frozen=True, slots=True)
class AdvanceWebhookDeliveryCursor:
    """One caller's request to move the cursor to a newly delivered event,
    against the revision it read before delivering."""

    to: WebhookDeliveryCursor
    expected_revision: WebhookDeliveryCursorRevision


@dataclass(frozen=True, slots=True)
class WebhookDeliveryCursorAdvanced:
    """The cursor moved to this position, at this new revision."""

    cursor: WebhookDeliveryCursor
    revision: WebhookDeliveryCursorRevision


@dataclass(frozen=True, slots=True)
class CursorAdvanceConflict:
    """Another advance landed first: the caller's expected revision is stale.

    Never a silent overwrite -- the caller already delivered its event, so
    losing this race means only that some other writer's advance already
    covers at least as much ground, and the caller's own delivery was
    redundant rather than lost.
    """

    expected: WebhookDeliveryCursorRevision
    actual: WebhookDeliveryCursorRevision


type AdvanceWebhookDeliveryCursorOutcome = (
    WebhookDeliveryCursorAdvanced | CursorAdvanceConflict
)


@dataclass(frozen=True, slots=True)
class WebhookEventPayload:
    """What one attention event is delivered as.

    Identity for the receiver's own dedup (`run_id`, `event_sequence`), the
    kind and node that raised attention, and when it was recorded -- never
    node-rail or wait-answer detail, which stays behind the read door a
    subscriber follows up through (the same hint-over-truth shape ADR 0010
    §4 already applies to a platform's own webhook: this delivery names an
    event, and a subscriber reads the rest through the API).
    """

    run_id: RunId
    event_sequence: int
    event_kind: RunEventKind
    node_id: str
    recorded_at: RecordedAt

    def __post_init__(self) -> None:
        if type(self.event_sequence) is not int or self.event_sequence <= 0:
            raise ValueError(
                "a webhook event payload's event sequence must be a positive integer"
            )
        if self.node_id == "":
            raise ValueError("a webhook event payload's node id must be nonempty")


def webhook_payload_bytes(payload: WebhookEventPayload) -> bytes:
    """The exact bytes one delivery sends, and the exact bytes its signature
    covers -- the same bytes for both, which is the whole point of naming a
    byte rule at all.

    Canonical JSON: sorted keys, no incidental whitespace, so the same
    payload always serializes to the same bytes regardless of dict
    insertion order or a caller's `json` defaults. Deterministic and pure --
    no clock, no randomness, no I/O.
    """

    document = {
        "event_kind": payload.event_kind.value,
        "event_sequence": payload.event_sequence,
        "node_id": payload.node_id,
        "recorded_at": payload.recorded_at.value,
        "run_id": payload.run_id.value,
    }
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_webhook_payload(signing_key: bytes, payload_bytes: bytes) -> bytes:
    """HMAC-SHA256 over the exact payload bytes -- the plan's committed rule.

    Pure: the key and the bytes are both call-site parameters, never read
    from a file or an environment here. Where the key comes from is
    phase 2's composition concern, per ADR 0009 §6's credential-by-reference
    pattern.
    """

    return hmac.new(signing_key, payload_bytes, hashlib.sha256).digest()
