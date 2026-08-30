from __future__ import annotations

import hashlib

import pytest

from atelier2.contracts.effects import EffectRequestHash
from atelier2.contracts.hashing import (
    FrameDomainMismatch,
    FrameTruncated,
    Sha256Hash,
    frame,
    unframe,
)
from atelier2.contracts.runs import WorkflowRevisionHash

DIGEST_TYPES: list[type[Sha256Hash]] = [
    Sha256Hash,
    WorkflowRevisionHash,
    EffectRequestHash,
]


@pytest.mark.parametrize("digest_type", DIGEST_TYPES)
@pytest.mark.parametrize("payload", [b"", b"workflow-v1", b"\x00\xffbytes"])
def test_digest_is_the_sha256_of_the_exact_payload(
    digest_type: type[Sha256Hash], payload: bytes
) -> None:
    assert digest_type.of(payload).value == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("digest_type", DIGEST_TYPES)
@pytest.mark.parametrize(
    "value",
    ["", "0" * 63, "0" * 65, "g" * 64, hashlib.sha256(b"request").hexdigest().upper()],
)
def test_digest_rejects_a_durable_value_that_is_not_a_sha256_digest(
    digest_type: type[Sha256Hash], value: str
) -> None:
    with pytest.raises(ValueError, match="hexadecimal"):
        digest_type(value)


def test_digests_of_different_domain_meanings_never_compare_equal() -> None:
    document = b"workflow-v1"

    assert WorkflowRevisionHash.of(document) != EffectRequestHash.of(document)
    assert WorkflowRevisionHash.of(document) != Sha256Hash.of(document)


_FRAME_DOMAIN = "atelier-frame-test/v1"


@pytest.mark.parametrize(
    "fields",
    [
        (),
        (b"",),
        (b"one",),
        (b"", b"\x00\xff", b"wide" * 100),
    ],
)
def test_unframe_reads_back_exactly_the_fields_frame_encoded(
    fields: tuple[bytes, ...],
) -> None:
    assert unframe(frame(_FRAME_DOMAIN, *fields), _FRAME_DOMAIN) == fields


def test_the_frame_layout_every_stored_hash_stands_over_is_these_exact_bytes() -> None:
    """The prefix and the two length widths are frozen values, not a choice a
    reader may re-derive: every stored receipt, node receipt and run terminal
    hash was computed over them. This pins the bytes so giving the reader one
    owner cannot move the format underneath what is already stored."""

    assert frame("d/v1", b"ab", b"") == (
        b"ATELIER2\x00"
        b"\x00\x00\x00\x04"
        b"d/v1"
        b"\x00\x00\x00\x00\x00\x00\x00\x02"
        b"ab"
        b"\x00\x00\x00\x00\x00\x00\x00\x00"
    )


def _domain_length_inflated_by(domain: str, extra: int) -> bytes:
    """A header promising `extra` more domain bytes than the payload carries."""
    promised = frame(domain + "x" * extra)
    return promised[: len(promised) - extra]


@pytest.mark.parametrize(
    "payload",
    [
        b"not-a-frame-at-all",
        b"\x00" + frame(_FRAME_DOMAIN, b"field"),
        frame("another-domain/v1", b"field"),
        frame(_FRAME_DOMAIN)[:15],
        _domain_length_inflated_by(_FRAME_DOMAIN, 83),
    ],
    ids=(
        "no-prefix",
        "shifted-prefix",
        "another-domain",
        "cut-domain",
        "inflated-domain-length",
    ),
)
def test_unframe_refuses_bytes_no_frame_of_this_domain_could_be(
    payload: bytes,
) -> None:
    """Only `frame`'s own image is admitted. A header declaring more domain
    bytes than the payload holds is outside that image -- `frame` would have
    written the true length -- so it is refused rather than read against the
    bytes that happen to be there, which would otherwise let a payload with an
    absurd domain length decode as a well-formed frame carrying no fields."""

    with pytest.raises(FrameDomainMismatch):
        unframe(payload, _FRAME_DOMAIN)


@pytest.mark.parametrize(
    ("payload", "declared_field_length"),
    [
        (frame(_FRAME_DOMAIN)[:11], None),
        (frame(_FRAME_DOMAIN) + b"\x00\x00\x00", None),
        (frame(_FRAME_DOMAIN, b"abcdef")[:-2], 6),
    ],
)
def test_unframe_reports_the_length_a_truncated_payload_declared(
    payload: bytes, declared_field_length: int | None
) -> None:
    """A caller that bounds its own records needs the declared size to tell an
    absurd declaration from bytes that merely ran out; the reader supplies the
    number and leaves the naming to the caller."""

    with pytest.raises(FrameTruncated) as refusal:
        unframe(payload, _FRAME_DOMAIN)
    assert refusal.value.declared_field_length == declared_field_length
