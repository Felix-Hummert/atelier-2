from __future__ import annotations

import pytest

from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.runs import RunId
from atelier2.contracts.webhook_delivery import (
    AdvanceWebhookDeliveryCursor,
    CursorAdvanceConflict,
    WebhookDeliveryCursor,
    WebhookDeliveryCursorAdvanced,
    WebhookDeliveryCursorRevision,
    WebhookDeliveryCursorState,
    WebhookEventPayload,
    sign_webhook_payload,
    webhook_payload_bytes,
)
from atelier2.contracts.when import RecordedAt


def _payload(
    run_id: str = "run-1",
    event_sequence: int = 1,
    event_kind: RunEventKind = RunEventKind.WAITING_INPUT,
    node_id: str = "pause",
    recorded_at: str = "2026-08-23T09:00:00Z",
) -> WebhookEventPayload:
    return WebhookEventPayload(
        RunId(run_id), event_sequence, event_kind, node_id, RecordedAt(recorded_at)
    )


def test_a_cursor_names_a_positive_event_sequence() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        WebhookDeliveryCursor(RunId("run-1"), 0)


def test_a_payload_names_a_positive_event_sequence() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        _payload(event_sequence=0)


def test_a_payload_names_a_nonempty_node_id() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        _payload(node_id="")


def test_advancing_from_the_expected_revision_moves_the_cursor_and_counts_it() -> None:
    state = WebhookDeliveryCursorState(None, WebhookDeliveryCursorRevision(0))
    to = WebhookDeliveryCursor(RunId("run-1"), 1)
    command = AdvanceWebhookDeliveryCursor(to, WebhookDeliveryCursorRevision(0))

    outcome = state.advance(command)

    assert outcome == WebhookDeliveryCursorAdvanced(
        to, WebhookDeliveryCursorRevision(1)
    )


def test_advancing_against_a_stale_revision_is_a_conflict_not_an_overwrite() -> None:
    state = WebhookDeliveryCursorState(
        WebhookDeliveryCursor(RunId("run-1"), 1), WebhookDeliveryCursorRevision(1)
    )
    stale_expected = WebhookDeliveryCursorRevision(0)
    command = AdvanceWebhookDeliveryCursor(
        WebhookDeliveryCursor(RunId("run-1"), 2), stale_expected
    )

    outcome = state.advance(command)

    assert outcome == CursorAdvanceConflict(stale_expected, state.revision)


def test_the_same_payload_serializes_to_the_same_bytes_every_time() -> None:
    first = webhook_payload_bytes(_payload())
    second = webhook_payload_bytes(_payload())

    assert first == second


@pytest.mark.parametrize(
    "other",
    [
        _payload(run_id="run-2"),
        _payload(event_sequence=2),
        _payload(event_kind=RunEventKind.AGENT_FAILED),
        _payload(node_id="other-node"),
        _payload(recorded_at="2026-08-23T09:00:01Z"),
    ],
    ids=["run_id", "event_sequence", "event_kind", "node_id", "recorded_at"],
)
def test_a_changed_field_serializes_to_different_bytes(
    other: WebhookEventPayload,
) -> None:
    assert webhook_payload_bytes(_payload()) != webhook_payload_bytes(other)


def test_signing_the_same_bytes_with_the_same_secret_is_deterministic() -> None:
    payload_bytes = webhook_payload_bytes(_payload())

    first = sign_webhook_payload(b"a-shared-secret", payload_bytes)
    second = sign_webhook_payload(b"a-shared-secret", payload_bytes)

    assert first == second


def test_one_changed_byte_changes_the_signature() -> None:
    payload_bytes = webhook_payload_bytes(_payload())
    tampered = payload_bytes[:-1] + bytes([payload_bytes[-1] ^ 0x01])

    assert sign_webhook_payload(b"a-shared-secret", payload_bytes) != (
        sign_webhook_payload(b"a-shared-secret", tampered)
    )


def test_a_different_secret_changes_the_signature() -> None:
    payload_bytes = webhook_payload_bytes(_payload())

    assert sign_webhook_payload(b"secret-one", payload_bytes) != sign_webhook_payload(
        b"secret-two", payload_bytes
    )
