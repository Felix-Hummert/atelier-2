"""The outgoing webhook call, as a protocol only -- phase 2 gives it a real edge.

Phase 1 (`#433`) proves the delivery decision end-to-end behind a fake
implementation of this protocol: no URL, no header, no timeout budget, no
retry policy lives here, because none of that is this port's job. What is
this port's job is naming the three outcomes one delivery attempt can have,
so `application.deliver_attention_webhook` can classify a failure as worth
retrying or not without knowing anything about HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.webhook_delivery import WebhookEventPayload


@dataclass(frozen=True, slots=True)
class Delivered:
    """The subscriber answered 2xx. The only outcome that advances the cursor."""


@dataclass(frozen=True, slots=True)
class TransientFailure:
    """The attempt did not land, and a later retry of the same payload may."""

    detail: str


@dataclass(frozen=True, slots=True)
class PermanentFailure:
    """The subscriber's own answer says a retry of this exact payload will not help."""

    detail: str


type WebhookDeliveryAttemptOutcome = Delivered | TransientFailure | PermanentFailure


class WebhookTransport(Protocol):
    """Where one signed attention-event payload is handed to the wire.

    `signature` is `contracts.webhook_delivery.sign_webhook_payload`'s digest
    over `payload_bytes` -- computed once by the caller and carried here,
    never recomputed, so the signing secret stays with whoever composed the
    call instead of reaching into every transport implementation.
    """

    def deliver(
        self,
        payload: WebhookEventPayload,
        payload_bytes: bytes,
        signature: bytes,
    ) -> WebhookDeliveryAttemptOutcome: ...
