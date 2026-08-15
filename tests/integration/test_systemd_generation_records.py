from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from atelier2.adapters.systemd_generation_records import (
    DirectSystemdGenerationRecords,
    DirectSystemdIntent,
    DirectSystemdInvocationId,
    DirectSystemdRecoveryState,
    DirectSystemdResult,
    DirectSystemdResultOutcome,
    DirectSystemdStarted,
    decode_direct_systemd_intent,
    decode_direct_systemd_result,
    decode_direct_systemd_started,
    encode_direct_systemd_intent,
    encode_direct_systemd_result,
    encode_direct_systemd_started,
)
from atelier2.contracts.agent_attempts import AgentAttemptId, WatchdogGenerationId
from atelier2.contracts.hashing import Sha256Hash


def intent() -> DirectSystemdIntent:
    return DirectSystemdIntent(
        AgentAttemptId.of(b"attempt-17"),
        WatchdogGenerationId("generation-4"),
        "atelier2-attempt-17-generation-4.service",
        Sha256Hash.of(b"launch-envelope"),
        17,
    )


def started(value: DirectSystemdIntent | None = None) -> DirectSystemdStarted:
    bound_intent = value or intent()
    return DirectSystemdStarted(
        Sha256Hash.of(encode_direct_systemd_intent(bound_intent)),
        DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
    )


@pytest.mark.parametrize(
    ("value", "encode", "decode"),
    [
        (intent(), encode_direct_systemd_intent, decode_direct_systemd_intent),
        (started(), encode_direct_systemd_started, decode_direct_systemd_started),
        (
            DirectSystemdResult(
                Sha256Hash.of(encode_direct_systemd_started(started())),
                started().invocation_id,
                DirectSystemdResultOutcome.COMPLETED,
                0,
                b"output\x00\xff",
                b"warning",
                False,
                False,
            ),
            encode_direct_systemd_result,
            decode_direct_systemd_result,
        ),
    ],
)
def test_generation_records_have_one_exact_canonical_encoding(
    value: Any,
    encode: Callable[[Any], bytes],
    decode: Callable[[bytes], Any],
) -> None:
    encoded = encode(value)

    assert encoded.endswith(b"\n")
    assert decode(encoded) == value

    parsed = json.loads(encoded)
    noncanonical = json.dumps(parsed, indent=2).encode("ascii")
    with pytest.raises(ValueError, match="canonical"):
        decode(noncanonical)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.replace(b'"version":1', b'"version":2'),
        lambda payload: payload.replace(b'"unit_name":', b'"extra":0,"unit_name":'),
        lambda payload: payload.replace(
            b'"attempt_id":', b'"attempt_id":"bad","attempt_id":'
        ),
        lambda payload: b"\xef\xbb\xbf" + payload,
        lambda payload: payload[:-1],
    ],
)
def test_intent_decoder_rejects_changed_or_noncanonical_bytes(
    mutate: Callable[[bytes], bytes],
) -> None:
    canonical = encode_direct_systemd_intent(intent())

    with pytest.raises(ValueError):
        decode_direct_systemd_intent(mutate(canonical))


def test_exclusive_record_publication_never_repairs_or_replaces_evidence(
    tmp_path: Path,
) -> None:
    records = DirectSystemdGenerationRecords(tmp_path)
    original = intent()
    records.publish_intent(original)
    original_bytes = records.intent_path.read_bytes()

    with pytest.raises(FileExistsError):
        records.publish_intent(
            DirectSystemdIntent(
                original.attempt_id,
                original.generation_id,
                original.unit_name,
                Sha256Hash.of(b"changed"),
                original.standard_output_limit,
            )
        )

    assert records.intent_path.read_bytes() == original_bytes
    assert records.intent_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("started_bytes", [b"{", b"partial", b""])
def test_any_visible_malformed_started_forbids_replay(
    tmp_path: Path, started_bytes: bytes
) -> None:
    records = DirectSystemdGenerationRecords(tmp_path)
    records.publish_intent(intent())
    records.started_path.write_bytes(started_bytes)

    inspection = records.inspect()

    assert inspection.state is DirectSystemdRecoveryState.POSSIBLY_RAN
    assert inspection.started is None
    assert records.started_path.read_bytes() == started_bytes


def test_dangling_started_symlink_is_visible_and_forbids_replay(tmp_path: Path) -> None:
    records = DirectSystemdGenerationRecords(tmp_path)
    records.publish_intent(intent())
    records.started_path.symlink_to(tmp_path / "missing-target")

    inspection = records.inspect()

    assert inspection.state is DirectSystemdRecoveryState.POSSIBLY_RAN
    assert inspection.started is None
    assert records.started_path.is_symlink()


def test_started_fifo_is_malformed_evidence_and_never_blocks_recovery(
    tmp_path: Path,
) -> None:
    records = DirectSystemdGenerationRecords(tmp_path)
    records.publish_intent(intent())
    os.mkfifo(records.started_path)

    inspection = records.inspect()

    assert inspection.state is DirectSystemdRecoveryState.POSSIBLY_RAN
    assert inspection.started is None


def test_absent_started_is_the_only_record_state_that_allows_retry(
    tmp_path: Path,
) -> None:
    records = DirectSystemdGenerationRecords(tmp_path)
    records.publish_intent(intent())

    assert records.inspect().state is DirectSystemdRecoveryState.SAFE_TO_RETRY

    valid_started = started()
    records.publish_started(valid_started)
    assert records.inspect().state is DirectSystemdRecoveryState.POSSIBLY_RAN


def test_valid_result_must_bind_the_exact_started_and_invocation(
    tmp_path: Path,
) -> None:
    records = DirectSystemdGenerationRecords(tmp_path)
    records.publish_intent(intent())
    exact_started = started()
    records.publish_started(exact_started)
    records.publish_result(
        DirectSystemdResult(
            Sha256Hash.of(encode_direct_systemd_started(exact_started)),
            exact_started.invocation_id,
            DirectSystemdResultOutcome.COMPLETED,
            0,
            b"answer",
            b"",
            False,
            False,
        )
    )

    inspection = records.inspect()

    assert inspection.state is DirectSystemdRecoveryState.RESULT_PRESENT
    assert inspection.result is not None
    assert inspection.result.standard_output == b"answer"


def test_valid_but_mismatching_started_fails_loud_and_preserves_evidence(
    tmp_path: Path,
) -> None:
    records = DirectSystemdGenerationRecords(tmp_path)
    records.publish_intent(intent())
    mismatch = DirectSystemdStarted(
        Sha256Hash.of(b"other-intent"),
        DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
    )
    records.publish_started(mismatch)
    original_bytes = records.started_path.read_bytes()

    with pytest.raises(RuntimeError, match="INTENT"):
        records.inspect()

    assert records.started_path.read_bytes() == original_bytes


@pytest.mark.parametrize("mismatch_field", ["started-hash", "invocation-id"])
def test_valid_but_mismatching_result_fails_loud_and_preserves_evidence(
    tmp_path: Path, mismatch_field: str
) -> None:
    records = DirectSystemdGenerationRecords(tmp_path)
    records.publish_intent(intent())
    exact_started = started()
    records.publish_started(exact_started)
    mismatch = DirectSystemdResult(
        Sha256Hash.of(b"other-started")
        if mismatch_field == "started-hash"
        else Sha256Hash.of(encode_direct_systemd_started(exact_started)),
        DirectSystemdInvocationId("fedcba9876543210fedcba9876543210")
        if mismatch_field == "invocation-id"
        else exact_started.invocation_id,
        DirectSystemdResultOutcome.PROCESS_BOUNDARY_FAILED,
        None,
        b"",
        b"",
        False,
        False,
    )
    records.publish_result(mismatch)
    original_bytes = records.result_path.read_bytes()

    with pytest.raises(RuntimeError, match="STARTED|invocation"):
        records.inspect()

    assert records.result_path.read_bytes() == original_bytes
