from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from atelier2.adapters.systemd_generation_records import (
    DirectSystemdGenerationRecords,
    DirectSystemdIntent,
    DirectSystemdInvocationId,
    DirectSystemdRecordOutdated,
    DirectSystemdRecoveryState,
    DirectSystemdResult,
    DirectSystemdResultOutcome,
    DirectSystemdStarted,
    decode_direct_systemd_intent,
    decode_direct_systemd_result,
    decode_direct_systemd_started,
    encode_canonical_systemd_json,
    encode_direct_systemd_intent,
    encode_direct_systemd_result,
    encode_direct_systemd_started,
)
from atelier2.contracts.agent_attempts import AgentAttemptId, WatchdogGenerationId
from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.contracts.hashing import Sha256Hash
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
)


def intent() -> DirectSystemdIntent:
    return DirectSystemdIntent(
        AgentAttemptId.of(b"attempt-17"),
        WatchdogGenerationId("generation-4"),
        r"atelier2:_.-\attempt.service",
        Sha256Hash.of(b"launch-envelope"),
        17,
        Path("/leased/attempt-17"),
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


def test_corrupt_intent_output_limit_is_bounded_before_result_allocation(
    tmp_path: Path,
) -> None:
    records = DirectSystemdGenerationRecords(tmp_path)
    oversized = encode_direct_systemd_intent(intent()).replace(
        b'"standard_output_limit":17',
        (
            f'"standard_output_limit":{MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES + 1}'
        ).encode("ascii"),
    )
    records.intent_path.write_bytes(oversized)

    with pytest.raises(ValueError, match="output limit"):
        records.inspect()


def test_every_port_valid_intent_shape_fits_its_derived_record_bound(
    tmp_path: Path,
) -> None:
    records = DirectSystemdGenerationRecords(tmp_path)
    maximum = DirectSystemdIntent(
        AgentAttemptId.of(b"maximum-intent"),
        WatchdogGenerationId("\U0010ffff" * 1024),
        f"{'\\' * 247}.service",
        Sha256Hash.of(b"maximum-envelope"),
        MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
        Path("/" + "w" * 4_000),
    )

    records.publish_intent(maximum)

    assert records.read_intent() == maximum


@pytest.mark.parametrize(
    "unit_name",
    tuple(
        f"bad{character}name.service" for character in ('"', "\n", "/", " ", "ä", "@")
    )
    + ("missing-suffix", f"{'u' * 248}.service"),
)
def test_intent_refuses_names_outside_the_systemd_unit_charset(
    unit_name: str,
) -> None:
    with pytest.raises(ValueError, match="service name"):
        replace(intent(), unit_name=unit_name)


def test_result_bound_admits_largest_valid_shape_and_refuses_one_byte_more(
    tmp_path: Path,
) -> None:
    records = DirectSystemdGenerationRecords(tmp_path)
    maximum_intent = DirectSystemdIntent(
        AgentAttemptId.of(b"maximum-result"),
        WatchdogGenerationId("maximum-result"),
        "maximum-result.service",
        Sha256Hash.of(b"maximum-envelope"),
        MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
        Path("/leased/maximum-result"),
    )
    records.publish_intent(maximum_intent)
    exact_started = started(maximum_intent)
    records.publish_started(exact_started)
    maximum_result = DirectSystemdResult(
        Sha256Hash.of(encode_direct_systemd_started(exact_started)),
        exact_started.invocation_id,
        DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED,
        -MAXIMUM_SIGNED_INT64 - 1,
        b"o" * MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
        b"e" * MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
        True,
        False,
    )

    records.publish_result(maximum_result)

    inspection = records.inspect()
    assert inspection.state is DirectSystemdRecoveryState.RESULT_PRESENT
    assert inspection.result == maximum_result
    records.result_path.write_bytes(
        encode_direct_systemd_result(
            replace(
                maximum_result,
                standard_output=maximum_result.standard_output + b"x",
            )
        )
    )

    inspection = records.inspect()
    assert inspection.state is DirectSystemdRecoveryState.POSSIBLY_RAN
    assert inspection.result is None


@pytest.mark.parametrize(
    "return_code", [-MAXIMUM_SIGNED_INT64 - 2, MAXIMUM_SIGNED_INT64 + 1]
)
def test_result_refuses_return_code_outside_the_signed_int64_domain(
    return_code: int,
) -> None:
    with pytest.raises(ValueError, match="return code"):
        DirectSystemdResult(
            Sha256Hash.of(b"started"),
            DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
            DirectSystemdResultOutcome.COMPLETED,
            return_code,
            b"",
            b"",
            False,
            False,
        )


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
                original.working_directory,
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
    assert records.started_path.stat().st_mode & 0o777 == 0o600
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
    assert records.result_path.stat().st_mode & 0o777 == 0o600

    inspection = records.inspect()

    assert inspection.state is DirectSystemdRecoveryState.RESULT_PRESENT
    assert inspection.result is not None
    assert inspection.result.standard_output == b"answer"
    records.started_path.unlink()
    with pytest.raises(RuntimeError, match="RESULT exists without STARTED"):
        records.inspect()
    assert records.result_path.read_bytes() == encode_direct_systemd_result(
        inspection.result
    )


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


def test_an_intent_written_before_this_field_existed_is_refused_by_its_name(
    tmp_path: Path,
) -> None:
    """A record older than its reader is named, never guessed at.

    This record decides whether a live provider process may be stopped: the
    unit's identity attestation compares systemd's own `WorkingDirectory`
    against what the intent says was meant. Defaulting a missing field would
    attest an identity nobody ever wrote, so the reader refuses and says which
    field it lacked -- and it changes nothing on the way out.
    """

    records = DirectSystemdGenerationRecords(tmp_path)
    current = json.loads(encode_direct_systemd_intent(intent()))
    del current["working_directory"]
    older = encode_canonical_systemd_json(current)
    records.intent_path.write_bytes(older)

    with pytest.raises(DirectSystemdRecordOutdated, match="working_directory"):
        records.read_intent()

    assert records.intent_path.read_bytes() == older
