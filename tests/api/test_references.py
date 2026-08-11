from __future__ import annotations

import pytest

from atelier2.api.references import (
    MAX_SIGNED_INT64,
    EventCursor,
    InvalidEventCursor,
    InvalidPublicRunReference,
    InvalidRevisionHash,
    decode_canonical_base64,
    decode_public_run_reference,
    encode_event_cursor,
    encode_public_run_reference,
    parse_event_cursor,
    parse_revision_hash,
)
from atelier2.contracts.runs import RunId


@pytest.mark.parametrize("run_id", ["slash/run", "nul\0run", "Grüße-東京"])
def test_public_run_reference_losslessly_round_trips_exact_utf8(run_id: str) -> None:
    encoded = encode_public_run_reference(RunId(run_id))

    assert encoded.startswith("run1.")
    assert "=" not in encoded
    assert decode_public_run_reference(encoded) == RunId(run_id)


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "run2.YQ",
        "run1.",
        "run1.YQ==",
        "run1.@@",
        "run1._w",
        "run1.YQ=",
    ],
)
def test_public_run_reference_rejects_noncanonical_or_invalid_values(
    reference: str,
) -> None:
    with pytest.raises(InvalidPublicRunReference):
        decode_public_run_reference(reference)


@pytest.mark.parametrize("sequence", [1, MAX_SIGNED_INT64])
def test_event_cursor_round_trips_signed_int64_boundaries(sequence: int) -> None:
    cursor = encode_event_cursor(RunId("slash/\0東京"), sequence)

    assert parse_event_cursor(cursor) == EventCursor(RunId("slash/\0東京"), sequence)


@pytest.mark.parametrize(
    "cursor",
    [
        "event1.YQ.0",
        "event1.YQ.01",
        "event1.YQ.-1",
        "event1.YQ.+1",
        f"event1.YQ.{MAX_SIGNED_INT64 + 1}",
        "event2.YQ.1",
        "event1.YQ==.1",
        "event1..1",
    ],
)
def test_event_cursor_rejects_noncanonical_or_out_of_range_values(cursor: str) -> None:
    with pytest.raises(InvalidEventCursor):
        parse_event_cursor(cursor)


@pytest.mark.parametrize(
    "value",
    ["", "A" * 64, "g" * 64, "0" * 63, "0" * 65],
)
def test_revision_hash_requires_exact_lowercase_sha256(value: str) -> None:
    with pytest.raises(InvalidRevisionHash):
        parse_revision_hash(value)


@pytest.mark.parametrize("value", ["YQ", "YQ===", "Y Q==", "YQ-_", "===="])
def test_standard_base64_requires_canonical_padding_and_alphabet(value: str) -> None:
    with pytest.raises(ValueError):
        decode_canonical_base64(value)


def test_standard_base64_decodes_exact_bytes() -> None:
    assert decode_canonical_base64("AP+AYQ==") == b"\x00\xff\x80a"
