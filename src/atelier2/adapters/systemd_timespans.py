import math
import re
import subprocess
from pathlib import Path

from atelier2.adapters.bounded_processes import bounded_process_answer

_INFINITY = 2**64 - 1
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
    if microseconds == _INFINITY or expected_microseconds not in (None, microseconds):
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
    try:
        return bounded_process_answer(process, timeout_seconds, maximum_output_bytes)
    except OSError as error:
        raise DirectSystemdHostFailure("systemd timespan command failed") from error
