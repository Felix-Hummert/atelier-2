"""The real httpx webhook edge maps wire outcomes onto the three retry answers.

`#433` phase 2. The delivery decision classifies a retry by `Delivered`,
`TransientFailure` or `PermanentFailure`; this module pins which HTTP result
becomes which, because that mapping is what decides whether the durable cursor
advances. No socket is opened: an `httpx.MockTransport` scripts each response
and captures the request so the body and signature header are read back.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from atelier2.adapters.http_webhook_transport import (
    SIGNATURE_HEADER,
    HttpWebhookTransport,
)
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.runs import RunId
from atelier2.contracts.webhook_delivery import (
    WebhookEventPayload,
    webhook_payload_bytes,
)
from atelier2.contracts.when import RecordedAt
from atelier2.ports.webhook_transport import (
    Delivered,
    PermanentFailure,
    TransientFailure,
)

TARGET_URL = "https://receiver.example/atelier-webhook"
SIGNATURE = bytes(range(32))

_PAYLOAD = WebhookEventPayload(
    run_id=RunId("run-a"),
    event_sequence=1,
    event_kind=RunEventKind.WAITING_INPUT,
    node_id="pause",
    recorded_at=RecordedAt("2026-08-23T09:00:00Z"),
)


def _transport_answering(
    handler: Callable[[httpx.Request], httpx.Response],
) -> HttpWebhookTransport:
    return HttpWebhookTransport(
        httpx.Client(transport=httpx.MockTransport(handler)), TARGET_URL
    )


def _deliver_against_status(status_code: int) -> object:
    transport = _transport_answering(lambda _request: httpx.Response(status_code))
    return transport.deliver(_PAYLOAD, webhook_payload_bytes(_PAYLOAD), SIGNATURE)


def test_a_2xx_response_is_delivered() -> None:
    assert _deliver_against_status(204) == Delivered()


@pytest.mark.parametrize("status_code", (400, 401, 403, 404, 410, 422))
def test_a_client_refusal_is_permanent(status_code: int) -> None:
    assert isinstance(_deliver_against_status(status_code), PermanentFailure)


@pytest.mark.parametrize("status_code", (408, 429, 500, 502, 503, 504))
def test_a_retryable_status_is_transient(status_code: int) -> None:
    assert isinstance(_deliver_against_status(status_code), TransientFailure)


def test_a_redirect_is_permanent_because_the_target_is_not_followed() -> None:
    assert isinstance(_deliver_against_status(307), PermanentFailure)


def test_a_connection_error_is_transient() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    transport = _transport_answering(refuse)

    outcome = transport.deliver(_PAYLOAD, webhook_payload_bytes(_PAYLOAD), SIGNATURE)

    assert isinstance(outcome, TransientFailure)


def test_a_timeout_is_transient() -> None:
    def time_out(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("receiver timed out")

    transport = _transport_answering(time_out)

    outcome = transport.deliver(_PAYLOAD, webhook_payload_bytes(_PAYLOAD), SIGNATURE)

    assert isinstance(outcome, TransientFailure)


def test_the_request_posts_the_exact_bytes_and_carries_the_signature_header() -> None:
    captured: list[httpx.Request] = []

    def capture(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    payload_bytes = webhook_payload_bytes(_PAYLOAD)
    transport = _transport_answering(capture)

    transport.deliver(_PAYLOAD, payload_bytes, SIGNATURE)

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert str(request.url) == TARGET_URL
    assert request.content == payload_bytes
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers[SIGNATURE_HEADER] == f"sha256={SIGNATURE.hex()}"
