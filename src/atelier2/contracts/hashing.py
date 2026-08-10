from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Self

SHA256_HEX_DIGEST = re.compile(r"[0-9a-f]{64}")


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
