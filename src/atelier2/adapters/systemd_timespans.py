import concurrent.futures
import math
import os
import re
import signal
import subprocess
import time
from pathlib import Path

_PROCESS_GROUP_STOP_SECONDS = 0.1
# LC_ALL=C owns ASCII "us:"; C.UTF-8 changes it to Unicode "μs:".
_ANALYZE_ENV = {"LC_ALL": "C", "SYSTEMD_COLORS": "0", "SYSTEMD_URLIFY": "0"}


class DirectSystemdHostFailure(RuntimeError): ...


class DirectSystemdUnitConflict(RuntimeError): ...


def normalize_direct_systemd_timespan(
    systemd_analyze: Path,
    value: str,
    *,
    command_timeout_seconds: float,
    maximum_output_bytes: int,
    expected_microseconds: int | None = None,
) -> int:
    if not systemd_analyze.is_absolute():
        raise ValueError("systemd-analyze executable must be absolute")
    if (
        type(command_timeout_seconds) not in (int, float)
        or not math.isfinite(command_timeout_seconds)
        or command_timeout_seconds <= 0
    ):
        raise ValueError("systemd-analyze command timeout must be finite and positive")
    if type(maximum_output_bytes) is not int or maximum_output_bytes <= 0:
        raise ValueError("systemd-analyze output bound must be a positive integer")
    bounds = (command_timeout_seconds, maximum_output_bytes)
    return_code, standard_output = _run_bounded(systemd_analyze, value, bounds)
    if return_code != 0:
        raise DirectSystemdHostFailure("systemd timespan command was refused")
    try:
        output = standard_output.decode("utf-8", "strict")
    except UnicodeDecodeError as error:
        raise DirectSystemdHostFailure("systemd timespan output is invalid") from error
    pattern = rf"Original: {re.escape(value)}\n +us: ([0-9]+)\n +Human: ([^\n]+)\n?"
    match = re.fullmatch(pattern, output)
    if match is None:
        raise DirectSystemdHostFailure("systemd timespan output is malformed")
    microseconds = int(match.group(1))
    if match.group(2) == "infinity" or (
        expected_microseconds is not None and microseconds != expected_microseconds
    ):
        raise DirectSystemdUnitConflict("systemd duration changed")
    return microseconds


def _run_bounded(
    executable: Path, value: str, bounds: tuple[float, int]
) -> tuple[int, bytes]:
    timeout_seconds, maximum_output_bytes = bounds
    try:
        process = subprocess.Popen(
            (str(executable), "--no-pager", "timespan", "--", value),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=_ANALYZE_ENV,
        )
    except (OSError, ValueError) as error:
        raise DirectSystemdHostFailure("systemd timespan command failed") from error
    assert process.stdout is not None and process.stderr is not None
    streams = (process.stdout, process.stderr)
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    reads = tuple(
        executor.submit(stream.read, maximum_output_bytes + 1) for stream in streams
    )
    deadline = time.monotonic() + timeout_seconds
    try:
        for read in concurrent.futures.as_completed(
            reads, timeout=max(0, deadline - time.monotonic())
        ):
            if len(read.result()) > maximum_output_bytes:
                raise DirectSystemdHostFailure(
                    "systemd timespan output exceeded its bound"
                )
        outputs = tuple(read.result() for read in reads)
        return_code = process.wait(timeout=max(0, deadline - time.monotonic()))
    except DirectSystemdHostFailure:
        _terminate_process_group(process)
        raise
    except (OSError, OverflowError, subprocess.SubprocessError) as error:
        _terminate_process_group(process)
        raise DirectSystemdHostFailure("systemd timespan command failed") from error
    finally:
        for stream in streams:
            stream.close()
        executor.shutdown(wait=True)
    return return_code, outputs[0]


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    for group_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, group_signal)
        except ProcessLookupError:
            break
        except OSError as error:
            raise DirectSystemdHostFailure("systemd group signal failed") from error
        try:
            process.wait(timeout=_PROCESS_GROUP_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
    if process.poll() is None:
        raise DirectSystemdHostFailure("systemd timespan process group did not stop")
