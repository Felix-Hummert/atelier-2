from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass
from typing import Self

SHA256_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_FRAME_PREFIX = b"ATELIER2\x00"
_DOMAIN_LENGTH_FORMAT = ">I"
_FIELD_LENGTH_FORMAT = ">Q"
_DOMAIN_LENGTH_BYTES = struct.calcsize(_DOMAIN_LENGTH_FORMAT)
_FIELD_LENGTH_BYTES = struct.calcsize(_FIELD_LENGTH_FORMAT)


class FrameError(ValueError):
    """The payload is not what `frame` produces for the expected domain."""


class FrameDomainMismatch(FrameError):
    """The payload carries a foreign prefix, or another domain's frame."""


class FrameTruncated(FrameError):
    """A length the payload declared runs past the payload's own end.

    `declared_field_length` is the field size the payload announced, or `None`
    when the payload ended inside a length header before declaring anything.
    A caller that bounds its own records names an absurd declaration by that
    bound; the frame reader only knows the bytes are not there.
    """

    def __init__(self, message: str, declared_field_length: int | None = None) -> None:
        super().__init__(message)
        self.declared_field_length = declared_field_length


def frame(domain: str, *fields: bytes) -> bytes:
    """Encode one unambiguous Atelier identity preimage.

    Domain names are UTF-8 and length-prefixed with an unsigned 32-bit integer;
    each exact field is length-prefixed with an unsigned 64-bit integer.

    Integers are not a frame concern. An identity owner encodes them as bytes
    before calling: decimal ASCII (`str(n).encode("ascii")`) for the persisted
    `node-event-hash` family (`event_sequence` and V2 `attempt_ordinal`), and
    `struct.pack(">Q", n)` for minted ids (`AgentAttemptId.attempt_ordinal`,
    `AuthProfileRevision.revision_number`, and the same revision number inside
    the agent-execution-request and agent-receipt preimages). The two encodings
    of the same integer are different bytes. Switching a landed family rewrites
    every stored hash of that family, so new identities use pack and the
    event-hash family stays decimal ASCII.
    """

    domain_bytes = domain.encode("utf-8")
    if len(domain_bytes) > 0xFFFFFFFF:
        raise ValueError("frame domain exceeds uint32 length")
    encoded = bytearray(_FRAME_PREFIX)
    encoded.extend(struct.pack(_DOMAIN_LENGTH_FORMAT, len(domain_bytes)))
    encoded.extend(domain_bytes)
    for field in fields:
        if len(field) > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("frame field exceeds uint64 length")
        encoded.extend(struct.pack(_FIELD_LENGTH_FORMAT, len(field)))
        encoded.extend(field)
    return bytes(encoded)


def unframe(payload: bytes, domain: str) -> tuple[bytes, ...]:
    """Read back the exact fields `frame` encoded under this domain.

    The inverse of `frame`: `unframe(frame(domain, *fields), domain) == fields`,
    and no other byte sequence is admitted. This is the only reader of the
    layout, so a payload framed under a different domain is refused here rather
    than mistaken for a short frame of this one.

    Callers keep their own refusal vocabulary: `FrameError` says the bytes are
    not this domain's frame, never what a particular protocol calls that.
    """

    if not payload.startswith(_FRAME_PREFIX):
        raise FrameDomainMismatch("payload carries no Atelier frame prefix")
    cursor = len(_FRAME_PREFIX)
    if len(payload) - cursor < _DOMAIN_LENGTH_BYTES:
        raise FrameTruncated("payload ends inside its frame domain length")
    domain_length = struct.unpack_from(_DOMAIN_LENGTH_FORMAT, payload, cursor)[0]
    cursor += _DOMAIN_LENGTH_BYTES
    domain_end = cursor + domain_length
    if payload[cursor:domain_end] != domain.encode("utf-8"):
        raise FrameDomainMismatch(f"payload is not framed under {domain}")
    cursor = domain_end
    fields: list[bytes] = []
    while cursor < len(payload):
        if len(payload) - cursor < _FIELD_LENGTH_BYTES:
            raise FrameTruncated("payload ends inside a frame field length")
        field_length = struct.unpack_from(_FIELD_LENGTH_FORMAT, payload, cursor)[0]
        cursor += _FIELD_LENGTH_BYTES
        field_end = cursor + field_length
        if field_end > len(payload):
            raise FrameTruncated("frame field runs past its payload", field_length)
        fields.append(payload[cursor:field_end])
        cursor = field_end
    return tuple(fields)


@dataclass(frozen=True)
class Sha256Hash:
    """One SHA-256 digest in the exact form Atelier stores, compares, and reads back."""

    value: str

    def __post_init__(self) -> None:
        if SHA256_HEX_DIGEST.fullmatch(self.value) is None:
            raise ValueError(
                f"{type(self).__name__} must be 64 lowercase hexadecimal characters"
            )

    @classmethod
    def of(cls, payload: bytes) -> Self:
        return cls(hashlib.sha256(payload).hexdigest())
