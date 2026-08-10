from __future__ import annotations

import hashlib

import pytest

from atelier2.contracts.effects import EffectRequestHash
from atelier2.contracts.hashing import Sha256Hash
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
