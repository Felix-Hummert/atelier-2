from __future__ import annotations

import io
import os
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

import atelier2.adapters.systemd_agent_collector as collector_module
from atelier2.adapters.systemd_agent_collector import (
    DirectSystemdCollectorBarriers,
    DirectSystemdLaunch,
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
    AgentAttemptWorkspaceLease,
    AgentProcessCommand,
    AgentProcessInvocation,
)

_WRITE_STREAMS = """
import os, sys
for descriptor, byte, count in ((1, b'o', int(sys.argv[1])), (2, b'e', int(sys.argv[2]))):
    remaining = byte * count
    while remaining:
        remaining = remaining[os.write(descriptor, remaining):]
"""

_WRITE_BOTH_STREAMS_AFTER_STDOUT_CLOSE = """
import os, select, threading

stdout_closed = threading.Event()

def write_all(descriptor, payload):
    while payload:
        payload = payload[os.write(descriptor, payload):]

def write_stdout():
    write_all(1, b'o' * 18)
    closed = select.poll()
    closed.register(1, select.POLLERR | select.POLLHUP)
    if not closed.poll(5_000):
        os._exit(91)
    stdout_closed.set()

def write_stderr():
    if not stdout_closed.wait(5):
        os._exit(92)
    try:
        write_all(2, b'e' * 49_153)
    except BrokenPipeError:
        pass

threads = (threading.Thread(target=write_stdout), threading.Thread(target=write_stderr))
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
"""

_EXIT_WITH_PIPE_INHERITING_DESCENDANT = """
import pathlib, subprocess, sys, time
child = '''
import pathlib, sys, time
release = pathlib.Path(sys.argv[1])
pathlib.Path(sys.argv[2]).touch()
while not release.exists():
    time.sleep(0.01)
'''
subprocess.Popen((sys.executable, '-c', child, sys.argv[1], sys.argv[2]), stdout=sys.stdout, stderr=sys.stderr)
while not pathlib.Path(sys.argv[2]).exists():
    time.sleep(0.01)
sys.stdout.write('done')
"""
_PRINT_ENV = "import os;print(os.getenv('DECLARED'),os.getenv('INHERITED'))"


def invocation(
    standard_output_bytes: int = 17,
    standard_error_bytes: int = 0,
    *,
    output_limit: int = 17,
) -> AgentProcessInvocation:
    return collector_invocation(
        (
            sys.executable,
            "-c",
            _WRITE_STREAMS,
            str(standard_output_bytes),
            str(standard_error_bytes),
        ),
        Path.cwd(),
        frame_bytes=output_limit,
    )


def collector_invocation(
    arguments: tuple[str, ...],
    working_directory: Path,
    environment: tuple[tuple[str, str], ...] = (),
    standard_input: bytes = b"",
    *,
    frame_bytes: int,
) -> AgentProcessInvocation:
    """One invocation for a test that houses the leased directory itself."""

    return AgentProcessInvocation(
        AgentProcessCommand(
            arguments,
            environment,
            standard_input,
            standard_output_frame_bytes=frame_bytes,
        ),
        AgentAttemptWorkspaceLease(
            AgentAttemptId.of(b"collector-attempt"), working_directory
        ),
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
            process_invocation.command.standard_output_frame_bytes,
            process_invocation.lease.working_directory,
        )
    )
    launch_envelope_path.write_bytes(envelope)
    os.chmod(launch_envelope_path, 0o600)
    return records, launch_envelope_path


def test_launch_envelope_roundtrips_exact_live_only_invocation_bytes() -> None:
    process_invocation = collector_invocation(
        ("/bin/provider", "argument"),
        Path("/workspace"),
        standard_input=b"input\x00\xff",
        frame_bytes=29,
    )

    encoded = encode_direct_systemd_launch_envelope(process_invocation)

    # A launcher gets the command and the leased ground, never the lease: the
    # envelope's bytes are the same, and what it decodes to says who needs what.
    assert decode_direct_systemd_launch_envelope(encoded) == DirectSystemdLaunch(
        process_invocation.command, process_invocation.lease.working_directory
    )
    with pytest.raises(ValueError, match="canonical"):
        decode_direct_systemd_launch_envelope(encoded.replace(b",", b", ", 1))


def test_launch_envelope_roundtrips_exact_ordered_secret_free_environment() -> None:
    process_invocation = collector_invocation(
        ("/bin/provider",),
        Path("/workspace"),
        (("CLAUDE_CONFIG_DIR", "/credentials/claude"), ("PATH", "/usr/bin")),
        frame_bytes=29,
    )

    assert decode_direct_systemd_launch_envelope(
        encode_direct_systemd_launch_envelope(process_invocation)
    ) == DirectSystemdLaunch(
        process_invocation.command, process_invocation.lease.working_directory
    )


@pytest.mark.parametrize(("mode", "extra"), [(0o400, 0), (0o600, 0), (0o400, 1)])
def test_launch_envelope_reader_enforces_credential_modes_and_exact_bound(
    tmp_path: Path, mode: int, extra: int
) -> None:
    envelope_path = tmp_path / "credential"
    payload = b"x" * (
        collector_module.MAXIMUM_DIRECT_SYSTEMD_LAUNCH_ENVELOPE_BYTES + extra
    )
    envelope_path.write_bytes(payload)
    envelope_path.chmod(mode)

    read = lambda: collector_module.read_direct_systemd_launch_envelope(
        envelope_path, allowed_modes=frozenset({0o400, 0o600})
    )
    if extra:
        with pytest.raises(ValueError, match="exact bound"):
            read()
    else:
        assert read() == payload


def test_launch_envelope_reader_completes_a_short_regular_file_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    envelope_path = tmp_path / "credential"
    payload = b"bounded-complete-envelope"
    envelope_path.write_bytes(payload)
    envelope_path.chmod(0o400)
    original_read = os.read
    raw_reads: list[int] = []

    class ShortFile(io.FileIO):
        def readinto(self, buffer: Any) -> int | None:
            return raw_reads.append(1) or super().readinto(memoryview(buffer)[:3])

    def short_read(descriptor: int, count: int) -> bytes:
        return original_read(descriptor, min(count, 3))

    monkeypatch.setattr(os, "read", short_read)
    monkeypatch.setattr(
        os,
        "fdopen",
        lambda descriptor, *_args, **_kwargs: io.BufferedReader(ShortFile(descriptor)),
    )

    assert (
        collector_module.read_direct_systemd_launch_envelope(
            envelope_path, allowed_modes=frozenset({0o400, 0o600})
        )
        == payload
    )
    assert len(raw_reads) > 1


def test_collector_cli_derives_only_credential_and_source_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    credential_directory = tmp_path / "credentials"
    collect = Mock()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credential_directory))
    monkeypatch.setenv("INVOCATION_ID", "0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(
        collector_module, "collect_direct_systemd_agent_process", collect
    )

    collector_module.main()

    call = collect.call_args
    assert call.args[0].intent_path.parent == tmp_path
    assert call.args[1] == DirectSystemdInvocationId("0123456789abcdef0123456789abcdef")
    assert call.kwargs == {
        "launch_credential_path": credential_directory / "atelier2-launch",
        "source_envelope_path": tmp_path / "launch-envelope",
    }


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
        launch_credential_path=launch_envelope_path,
        source_envelope_path=launch_envelope_path,
    )

    assert result.outcome is outcome
    assert (
        len(result.standard_output)
        <= process_invocation.command.standard_output_frame_bytes
    )
    assert len(result.standard_error) <= MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
    assert result.standard_output_overflow is (
        standard_output_bytes > process_invocation.command.standard_output_frame_bytes
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
        launch_credential_path=launch_envelope_path,
        source_envelope_path=launch_envelope_path,
    )
    completion = result.process_completion
    if completion is not None:
        decoded.append(completion)

    assert result.outcome is DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED
    assert completion is None
    assert decoded == []


def test_both_streams_at_limit_plus_one_are_bounded_and_bypass_decode(
    tmp_path: Path,
) -> None:
    process_invocation = collector_invocation(
        (sys.executable, "-c", _WRITE_BOTH_STREAMS_AFTER_STDOUT_CLOSE),
        Path.cwd(),
        frame_bytes=17,
    )
    records, launch_envelope_path = prepared_records(tmp_path, process_invocation)
    processes: list[subprocess.Popen[bytes]] = []

    def recording_popen(
        arguments: tuple[str, ...], **keywords: Any
    ) -> subprocess.Popen[bytes]:
        process = cast(subprocess.Popen[bytes], subprocess.Popen(arguments, **keywords))
        processes.append(process)
        return process

    result = collect_direct_systemd_agent_process(
        records,
        DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
        launch_credential_path=launch_envelope_path,
        source_envelope_path=launch_envelope_path,
        popen=recording_popen,
    )

    assert processes[0].wait(timeout=5) == 0
    assert result.outcome is DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED
    assert (
        len(result.standard_output)
        <= process_invocation.command.standard_output_frame_bytes
    )
    assert len(result.standard_error) <= MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
    assert result.standard_output_overflow
    assert result.standard_error_overflow
    assert result.process_completion is None


def test_provider_exit_is_not_delayed_by_a_pipe_inheriting_descendant(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release-descendant"
    descendant_ready = tmp_path / "descendant-ready"
    process_invocation = collector_invocation(
        (
            sys.executable,
            "-c",
            _EXIT_WITH_PIPE_INHERITING_DESCENDANT,
            str(release),
            str(descendant_ready),
        ),
        Path.cwd(),
        frame_bytes=17,
    )
    records, launch_envelope_path = prepared_records(tmp_path, process_invocation)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            collect_direct_systemd_agent_process,
            records,
            DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
            launch_credential_path=launch_envelope_path,
            source_envelope_path=launch_envelope_path,
        )
        try:
            result = future.result(timeout=1)
            assert descendant_ready.exists()
            assert not release.exists()
        finally:
            release.touch()
            if not future.done():
                future.result(timeout=2)

    assert result.outcome is DirectSystemdResultOutcome.COMPLETED
    assert result.standard_output == b"done"


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
            launch_credential_path=launch_envelope_path,
            source_envelope_path=launch_envelope_path,
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
            launch_credential_path=launch_envelope_path,
            source_envelope_path=launch_envelope_path,
            popen=forbidden_popen,
        )
    assert popen_calls == []


def test_popen_runs_only_after_started_is_durable_and_envelope_unlink_is_durable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INHERITED", "must-not-reach-child")
    process_invocation = collector_invocation(
        (sys.executable, "-c", _PRINT_ENV),
        Path.cwd(),
        (("DECLARED", "yes"),),
        frame_bytes=32,
    )
    records, source_path = prepared_records(tmp_path, process_invocation)
    credential_path = tmp_path / "credential"
    credential_path.write_bytes(source_path.read_bytes())
    credential_path.chmod(0o400)
    observed: list[tuple[bool, bool, bool, object]] = []

    def observing_popen(
        arguments: tuple[str, ...], **keywords: Any
    ) -> subprocess.Popen[bytes]:
        observed.append(
            (
                records.started_path.exists(),
                source_path.exists(),
                credential_path.exists(),
                keywords.get("env"),
            )
        )
        return cast(subprocess.Popen[bytes], subprocess.Popen(arguments, **keywords))

    result = collect_direct_systemd_agent_process(
        records,
        DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
        launch_credential_path=credential_path,
        source_envelope_path=source_path,
        popen=observing_popen,
    )

    assert observed == [(True, False, True, {"DECLARED": "yes"})]
    assert result.standard_output == b"yes None\n"


@pytest.mark.parametrize(
    "failure", ["credential", "missing-source", "changed-source", "source-mode"]
)
def test_changed_credential_or_source_never_runs_popen(
    tmp_path: Path, failure: str
) -> None:
    records, source_path = prepared_records(tmp_path, invocation())
    credential_path = tmp_path / "credential"
    credential_path.write_bytes(source_path.read_bytes())
    if failure == "credential":
        credential_path.write_bytes(b"changed")
    elif failure == "missing-source":
        source_path.unlink()
    elif failure == "changed-source":
        source_path.write_bytes(b"changed")
    else:
        source_path.chmod(0o400)
    credential_path.chmod(0o400)
    forbidden_popen = Mock(side_effect=AssertionError("provider started"))

    with pytest.raises((FileNotFoundError, ValueError)):
        collect_direct_systemd_agent_process(
            records,
            DirectSystemdInvocationId("0123456789abcdef0123456789abcdef"),
            launch_credential_path=credential_path,
            source_envelope_path=source_path,
            popen=forbidden_popen,
        )

    assert records.inspect().state is (
        DirectSystemdRecoveryState.SAFE_TO_RETRY
        if failure == "credential"
        else DirectSystemdRecoveryState.POSSIBLY_RAN
    )
    forbidden_popen.assert_not_called()


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
            launch_credential_path=launch_envelope_path,
            source_envelope_path=launch_envelope_path,
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
        launch_credential_path=launch_envelope_path,
        source_envelope_path=launch_envelope_path,
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
