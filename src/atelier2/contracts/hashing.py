from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass
from typing import Self

SHA256_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_FRAME_PREFIX = b"ATELIER2\x00"


def canonical_json(payload: object) -> bytes:
    """Encode one unambiguous JSON spelling: sorted keys, no spacing, ASCII only.

    Every Atelier identity, fingerprint, and control frame that is compared byte
    for byte is written through here, so two writers of the same payload cannot
    disagree about its bytes.
    """

    return json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def frame(domain: str, *fields: bytes) -> bytes:
    """Encode one unambiguous Atelier identity preimage.

    Domain names are UTF-8 and length-prefixed with an unsigned 32-bit integer;
    each exact field is length-prefixed with an unsigned 64-bit integer.
    """

    domain_bytes = domain.encode("utf-8")
    if len(domain_bytes) > 0xFFFFFFFF:
        raise ValueError("frame domain exceeds uint32 length")
    encoded = bytearray(_FRAME_PREFIX)
    encoded.extend(struct.pack(">I", len(domain_bytes)))
    encoded.extend(domain_bytes)
    for field in fields:
        if len(field) > 0xFFFFFFFFFFFFFFFF:
            raise ValueError("frame field exceeds uint64 length")
        encoded.extend(struct.pack(">Q", len(field)))
        encoded.extend(field)
    return bytes(encoded)


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
