"""The real outgoing edge of `ports.webhook_transport`: one signed HTTP POST.

Phase 2 of `#433` gives the phase-1 protocol a network body. It POSTs the
exact payload bytes the delivery decision already built and signed, carries
the HMAC-SHA256 digest as a header, and maps the wire outcome onto the three
answers the decision classifies a retry by. Nothing about the delivery cursor,
the signing key, or which event is next lives here -- this module knows only a
target URL, an HTTP client, and how to read a response.

The signing key never reaches this adapter: `deliver_attention_webhook` signs
the payload and hands this edge the finished `signature`, so the credential
stays with the composition that resolved it (ADR 0009 §6) and no transport
implementation is ever a place it could leak from.
"""

from __future__ import annotations

from typing import Final

import httpx

from atelier2.contracts.webhook_delivery import WebhookEventPayload
from atelier2.ports.webhook_transport import (
    Delivered,
    PermanentFailure,
    TransientFailure,
    WebhookDeliveryAttemptOutcome,
)

# The header the receiver reads the digest from, and the algorithm it names, so
# a subscriber verifies the same HMAC-SHA256 the decision computed. `sha256=`
# states the scheme inline rather than leaving the receiver to assume one.
SIGNATURE_HEADER: Final = "X-Atelier-Webhook-Signature"
SIGNATURE_SCHEME: Final = "sha256"
_CONTENT_TYPE: Final = "application/json"
DEFAULT_TIMEOUT_SECONDS: Final = 10.0

# The two 4xx answers a later retry of the same payload may still turn into a
# 2xx: a request-timeout and a rate-limit are the receiver saying "not now",
# not "never". Every other 4xx is the receiver refusing this exact payload, and
# resending it unchanged cannot help.
_RETRYABLE_CLIENT_STATUSES: Final = frozenset({408, 429})


def signature_header_value(signature: bytes) -> str:
    """The header a delivery carries its digest in, algorithm named inline."""

    return f"{SIGNATURE_SCHEME}={signature.hex()}"


class HttpWebhookTransport:
    """One connected webhook target, delivered to over a real HTTP client.

    The client is injected so a test drives this exact mapping through an
    `httpx.MockTransport` without a socket; `open_webhook_transport` builds the
    production client with a real timeout budget.
    """

    def __init__(self, client: httpx.Client, target_url: str) -> None:
        self._client = client
        self._target_url = target_url

    def deliver(
        self,
        payload: WebhookEventPayload,
        payload_bytes: bytes,
        signature: bytes,
    ) -> WebhookDeliveryAttemptOutcome:
        try:
            response = self._client.post(
                self._target_url,
                content=payload_bytes,
                headers={
                    "Content-Type": _CONTENT_TYPE,
                    SIGNATURE_HEADER: signature_header_value(signature),
                },
            )
        except httpx.TransportError as error:
            # Timeouts, connection refusals and mid-flight network drops: the
            # payload never provably landed, so the same bytes may land later.
            return TransientFailure(f"webhook transport error: {error}")
        return _outcome_for_status(response.status_code)

    def close(self) -> None:
        self._client.close()


def _outcome_for_status(status_code: int) -> WebhookDeliveryAttemptOutcome:
    if 200 <= status_code < 300:
        return Delivered()
    if status_code in _RETRYABLE_CLIENT_STATUSES or 500 <= status_code < 600:
        return TransientFailure(f"webhook receiver answered {status_code}")
    return PermanentFailure(f"webhook receiver answered {status_code}")


def open_webhook_transport(
    target_url: str, *, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
) -> HttpWebhookTransport:
    """The production edge: a real HTTP client bounded by one timeout budget.

    Redirects are not followed -- a webhook target that answers 3xx is
    misconfigured, and chasing it would deliver to a URL the operator never
    named -- so that response falls to `PermanentFailure` like any other
    unusable answer.
    """

    client = httpx.Client(timeout=timeout_seconds, follow_redirects=False)
    return HttpWebhookTransport(client, target_url)
