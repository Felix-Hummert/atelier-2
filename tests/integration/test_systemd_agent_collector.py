from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from atelier2.adapters.systemd_agent_collector import (
    DirectSystemdCollectorBarriers,
    collect_direct_systemd_agent_process,
    decode_direct_systemd_launch_envelope,
    encode_direct_systemd_launch_envelope,
)
from atelier2.adapters.systemd_generation_records import (
    DirectSystemdGenerationRecords,
    DirectSystemdIntent,
    DirectSystemdInvocationId,
    DirectSystemdRecoveryState,
    DirectSystemdResultOutcome,
)
from atelier2.contracts.agent_attempts import AgentAttemptId, WatchdogGenerationId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    AgentProcessInvocation,
)

_WRITE_STREAMS = """
import os, sys
for descriptor, byte, count in ((1, b'o', int(sys.argv[1])), (2, b'e', int(sys.argv[2]))):
    remaining = byte * count
    while remaining:
        remaining = remaining[os.write(descriptor, remaining):]
"""


def invocation(
    standard_output_bytes: int = 17,
    standard_error_bytes: int = 0,
    *,
    output_limit: int = 17,
) -> AgentProcessInvocation:
    return AgentProcessInvocation(
        (
            sys.executable,
            "-c",
            _WRITE_STREAMS,
            str(standard_output_bytes),
            str(standard_error_bytes),
        ),
        Path.cwd(),
        standard_output_frame_bytes=output_limit,
    )


def prepared_records(
    tmp_path: Path, process_invocation: AgentProcessInvocation
) -> tuple[DirectSystemdGenerationRecords, Path]:
    records = DirectSystemdGenerationRecords(tmp_path)
    launch_envelope_path = tmp_path / "launch-envelope"
    envelope = encode_direct_systemd_launch_envelope(process_invocation)
    records.publish_intent(
        DirectSystemdIntent(
            AgentAttemptId.of(b"collector-attempt"),
            WatchdogGenerationId("collector-generation"),
            "atelier2-collector-attempt.service",
            Sha256Hash.of(envelope),
            process_invocation.standard_output_frame_bytes,
        )
    )
    launch_envelope_path.write_bytes(envelope)
    os.chmod(launch_envelope_path, 0o600)
    return records, launch_envelope_path


def test_launch_envelope_roundtrips_exact_live_only_invocation_bytes() -> None:
    process_invocation = AgentProcessInvocation(
        ("/bin/provider", "argument"),
        Path("/workspace"),
        standard_input=b"input\x00\xff",
        standard_output_frame_bytes=29,
    )

    encoded = encode_direct_systemd_launch_envelope(process_invocation)

    assert decode_direct_systemd_launch_envelope(encoded) == process_invocation
    with pytest.raises(ValueError, match="canonical"):
        decode_direct_systemd_launch_envelope(encoded.replace(b",", b", ", 1))


def test_launch_envelope_refuses_every_provider_environment_value() -> None:
    process_invocation = AgentProcessInvocation(
        ("/bin/provider",),
        Path("/workspace"),
        (("API_KEY", "secret"),),
        standard_output_frame_bytes=29,
    )

    with pytest.raises(ValueError, match="cannot carry provider environment"):
        encode_direct_systemd_launch_envelope(process_invocation)


@pytest.mark.parametrize(
    ("standard_output_bytes", "standard_error_bytes", "outcome"),
    [
        (16, 49_151, DirectSystemdResultOutcome.COMPLETED),
        (17, 49_152, DirectSystemdResultOutcome.COMPLETED),
        (18, 0, DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED),
        (0, 49_153, DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED),
    ],
)
def test_collector_reads_each_stream_to_exactly_its_bound_plus_one(
    tmp_path: Path,
    standard_output_bytes: int,
    standard_error_bytes: int,
    outcome: DirectSystemdResultOutcome,
) -> None:
    process_invocation = invocation(standard_output_bytes, standard_error_bytes)
    records, launch_envelope_path = prepared_records(tmp_path, process_invocation)

    result = collect_direct_systemd_agent_process(
        records,
        DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
        launch_envelope_path=launch_envelope_path,
    )

    assert result.outcome is outcome
    assert len(result.standard_output) <= process_invocation.standard_output_frame_bytes
    assert len(result.standard_error) <= MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
    assert result.standard_output_overflow is (
        standard_output_bytes > process_invocation.standard_output_frame_bytes
    )
    assert result.standard_error_overflow is (
        standard_error_bytes > MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
    )
    assert not launch_envelope_path.exists()
    assert records.inspect().state is DirectSystemdRecoveryState.RESULT_PRESENT


def test_overflow_has_no_completion_and_cannot_reach_the_provider_decoder(
    tmp_path: Path,
) -> None:
    process_invocation = invocation(18, output_limit=17)
    records, launch_envelope_path = prepared_records(tmp_path, process_invocation)
    decoded: list[object] = []

    result = collect_direct_systemd_agent_process(
        records,
        DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
        launch_envelope_path=launch_envelope_path,
    )
    completion = result.process_completion
    if completion is not None:
        decoded.append(completion)

    assert result.outcome is DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED
    assert completion is None
    assert decoded == []


@pytest.mark.parametrize(
    "barrier_name",
    [
        "after_started_file_fsync",
        "after_started_directory_fsync",
        "after_envelope_directory_fsync",
    ],
)
def test_no_popen_path_exists_before_started_directory_fsync_and_envelope_removal(
    tmp_path: Path, barrier_name: str
) -> None:
    process_invocation = invocation()
    records, launch_envelope_path = prepared_records(tmp_path, process_invocation)
    popen_calls: list[tuple[object, ...]] = []

    def crash() -> None:
        raise InjectedCrash

    def forbidden_popen(
        arguments: tuple[str, ...], **_keywords: object
    ) -> subprocess.Popen[bytes]:
        popen_calls.append(arguments)
        raise AssertionError("Popen ran before the durable launch barrier")

    barriers = _crashing_barriers(barrier_name, crash)

    with pytest.raises(InjectedCrash):
        collect_direct_systemd_agent_process(
            records,
            DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
            launch_envelope_path=launch_envelope_path,
            barriers=barriers,
            popen=forbidden_popen,
        )

    assert popen_calls == []
    assert records.started_path.exists()
    assert records.inspect().state is DirectSystemdRecoveryState.POSSIBLY_RAN
    with pytest.raises(FileExistsError):
        collect_direct_systemd_agent_process(
            records,
            DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
            launch_envelope_path=launch_envelope_path,
            popen=forbidden_popen,
        )
    assert popen_calls == []


def test_popen_runs_only_after_started_is_durable_and_envelope_unlink_is_durable(
    tmp_path: Path,
) -> None:
    process_invocation = invocation()
    records, launch_envelope_path = prepared_records(tmp_path, process_invocation)
    observed: list[tuple[bool, bool]] = []

    def observing_popen(
        arguments: tuple[str, ...], **keywords: Any
    ) -> subprocess.Popen[bytes]:
        observed.append((records.started_path.exists(), launch_envelope_path.exists()))
        return cast(subprocess.Popen[bytes], subprocess.Popen(arguments, **keywords))

    collect_direct_systemd_agent_process(
        records,
        DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
        launch_envelope_path=launch_envelope_path,
        popen=observing_popen,
    )

    assert observed == [(True, False)]


def test_changed_or_malformed_launch_envelope_never_publishes_started_or_runs_popen(
    tmp_path: Path,
) -> None:
    process_invocation = invocation()
    records, launch_envelope_path = prepared_records(tmp_path, process_invocation)
    launch_envelope_path.write_bytes(b"changed")
    popen_called = False

    def forbidden_popen(
        arguments: tuple[str, ...], **_keywords: Any
    ) -> subprocess.Popen[bytes]:
        del arguments
        nonlocal popen_called
        popen_called = True
        raise AssertionError

    with pytest.raises(ValueError, match="launch envelope"):
        collect_direct_systemd_agent_process(
            records,
            DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
            launch_envelope_path=launch_envelope_path,
            popen=forbidden_popen,
        )

    assert not records.started_path.exists()
    assert not popen_called


def test_launch_fifo_is_rejected_without_blocking_or_publishing_started(
    tmp_path: Path,
) -> None:
    process_invocation = invocation()
    records, launch_envelope_path = prepared_records(tmp_path, process_invocation)
    launch_envelope_path.unlink()
    os.mkfifo(launch_envelope_path, mode=0o600)

    with pytest.raises(ValueError, match="mode-0600 file"):
        collect_direct_systemd_agent_process(
            records,
            DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
            launch_envelope_path=launch_envelope_path,
        )

    assert not records.started_path.exists()


def test_popen_refusal_writes_one_invocation_bound_process_boundary_result(
    tmp_path: Path,
) -> None:
    process_invocation = invocation()
    records, launch_envelope_path = prepared_records(tmp_path, process_invocation)

    def refusing_popen(
        arguments: tuple[str, ...], **_keywords: Any
    ) -> subprocess.Popen[bytes]:
        del arguments
        raise OSError("refused")

    result = collect_direct_systemd_agent_process(
        records,
        DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
        launch_envelope_path=launch_envelope_path,
        popen=refusing_popen,
    )

    assert result.outcome is DirectSystemdResultOutcome.PROCESS_BOUNDARY_FAILED
    assert result.process_completion is None
    assert records.inspect().result == result


class InjectedCrash(Exception):
    pass


def _crashing_barriers(
    name: str, crash: Callable[[], None]
) -> DirectSystemdCollectorBarriers:
    if name == "after_started_file_fsync":
        return DirectSystemdCollectorBarriers(after_started_file_fsync=crash)
    if name == "after_started_directory_fsync":
        return DirectSystemdCollectorBarriers(after_started_directory_fsync=crash)
    if name == "after_envelope_directory_fsync":
        return DirectSystemdCollectorBarriers(after_envelope_directory_fsync=crash)
    raise AssertionError(f"unknown barrier {name}")
