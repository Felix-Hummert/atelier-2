import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from atelier2.adapters import systemd_timespans
from atelier2.adapters.systemd_timespans import (
    DirectSystemdHostFailure,
    DirectSystemdUnitConflict,
    normalize_direct_systemd_timespan,
)


def _normalize(executable: Path, value: str, expected: int | None = None) -> int:
    return normalize_direct_systemd_timespan(
        executable,
        value,
        command_timeout_seconds=0.25,
        maximum_output_bytes=128,
        expected_microseconds=expected,
    )


def _fake_analyzer(tmp_path: Path) -> Path:
    executable = tmp_path / "analyzer"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os,sys,time\n"
        "case=sys.argv[-1]\n"
        "assert sys.argv[1:-1]==['--no-pager','timespan','--']\n"
        "assert os.environ=={'LC_ALL':'C','SYSTEMD_COLORS':'0','SYSTEMD_URLIFY':'0'}\n"
        "line=lambda value,micros,human:f'Original: {value}\\n      us: {micros}\\n   Human: {human}\\n'\n"
        "if case=='refused':sys.exit(1)\n"
        "if case=='malformed':print('bad');sys.exit()\n"
        "if case=='duplicate':print(line(case,1,'1us')*2,end='');sys.exit()\n"
        "if case=='stdout':os.write(1,b'x'*129);sys.exit()\n"
        "if case=='stderr':os.write(2,b'x'*129);sys.exit()\n"
        "if case=='decode':os.write(1,b'\\xff');sys.exit()\n"
        "if case=='timeout':time.sleep(2)\n"
        "if case=='integer-infinity':print(line(case,2**64-1,'not infinity'),end='');sys.exit()\n"
        "human='infinity' if case=='infinity' else '1min'\n"
        "micros=2**64-1 if case=='infinity' else 61000000 if case=='unequal' else 60000000\n"
        "print(line(case,micros,human),end='')\n"
    )
    executable.chmod(0o700)
    return executable


def test_exact_command_and_c_locale_normalize_one_value(tmp_path: Path) -> None:
    assert _normalize(_fake_analyzer(tmp_path), "60s") == 60_000_000


@pytest.mark.parametrize(
    "case",
    [
        "refused",
        "malformed",
        "duplicate",
        "stdout",
        "stderr",
        "decode",
        "timeout",
        "infinity",
        "integer-infinity",
        "unequal",
    ],
)
def test_refuses_command_output_or_identity_failures(tmp_path: Path, case: str) -> None:
    error = (
        DirectSystemdUnitConflict
        if case in {"infinity", "integer-infinity", "unequal"}
        else DirectSystemdHostFailure
    )
    expected = 60_000_000 if case == "unequal" else None
    with pytest.raises(error):
        _normalize(_fake_analyzer(tmp_path), case, expected)


@pytest.mark.parametrize("descriptor", [1, 2])
@pytest.mark.parametrize("deadline", [1.5, 1e20])
def test_overflow_preempts_deadline_and_removes_the_process_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    descriptor: int,
    deadline: float,
) -> None:
    executable = tmp_path / "analyzer"
    pid_file = tmp_path / "pids"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os,signal,subprocess,sys,time\n"
        "child=subprocess.Popen((sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(4)'))\n"
        "open(sys.argv[-1],'w').write(f'{os.getpid()} {child.pid}')\n"
        f"if {deadline > 1e10}:\n"
        " while not os.path.exists(sys.argv[-1]+'.release'):time.sleep(.01)\n"
        f"os.write({descriptor},b'x'*4096);time.sleep(4)\n"
    )
    executable.chmod(0o700)
    if deadline > 1e10:
        real_popen = subprocess.Popen

        def start(arguments: tuple[str, ...], **options: Any) -> Any:
            process = real_popen(arguments, **options)
            while not pid_file.exists():
                time.sleep(0.01)
            return process

        monkeypatch.setattr(systemd_timespans.subprocess, "Popen", start)

    started = time.monotonic()
    with pytest.raises(DirectSystemdHostFailure):
        normalize_direct_systemd_timespan(
            executable,
            str(pid_file),
            command_timeout_seconds=deadline,
            maximum_output_bytes=64,
        )

    assert time.monotonic() - started < 1
    for process_id in map(int, pid_file.read_text().split()):
        cleanup_deadline = time.monotonic() + 1
        while time.monotonic() < cleanup_deadline:
            try:
                os.kill(process_id, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            pytest.fail(f"process {process_id} remained after group cleanup")


@pytest.mark.parametrize("deadline", [float("nan"), float("inf")])
def test_rejects_a_nonfinite_deadline(deadline: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        normalize_direct_systemd_timespan(
            Path("/usr/bin/systemd-analyze"),
            "1s",
            command_timeout_seconds=deadline,
            maximum_output_bytes=128,
        )


@pytest.mark.parametrize("value", ["1s\0x", "1s"])
def test_maps_execution_failures_to_host_failure(tmp_path: Path, value: str) -> None:
    executable = (
        Path("/usr/bin/systemd-analyze") if "\0" in value else tmp_path / "absent"
    )
    with pytest.raises(DirectSystemdHostFailure):
        _normalize(executable, value)


def test_real_systemd_analyzer_owns_c_locale_grammar() -> None:
    found = shutil.which("systemd-analyze")
    if found is None:
        pytest.skip("systemd-analyze is absent")
    executable = Path(found).resolve()

    assert _normalize(executable, "60s") == _normalize(executable, "1min")
    assert _normalize(executable, "1min 30s") == 90_000_000
    with pytest.raises(DirectSystemdUnitConflict):
        _normalize(executable, "infinity")


@pytest.mark.parametrize("reap_times_out", [False, True])
def test_refused_group_signal_cannot_abandon_the_caller_deadline(
    tmp_path: Path,
    reap_times_out: bool,
) -> None:
    executable = tmp_path / "analyzer"
    pid_file = tmp_path / "pids"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os,subprocess,sys,time\n"
        "child=subprocess.Popen((sys.executable,'-c','import time;time.sleep(4)'),stdout=sys.stdout,stderr=sys.stderr)\n"
        "open(sys.argv[-1],'w').write(f'{os.getpid()} {child.pid}')\n"
        "os.write(1,b'x'*4096);time.sleep(4)\n"
    )
    executable.chmod(0o700)
    driver = f"""
import os,subprocess,time
from pathlib import Path
from atelier2.adapters import bounded_processes
from atelier2.adapters import systemd_timespans as owner
bounded_processes.os.killpg = lambda *_: (_ for _ in ()).throw(PermissionError())
if {reap_times_out!r}:
    real_wait = bounded_processes.subprocess.Popen.wait
    def timeout_after_reap(process, timeout=None):
        real_wait(process, timeout=timeout)
        raise subprocess.TimeoutExpired(process.args, timeout)
    bounded_processes.subprocess.Popen.wait = timeout_after_reap
started = time.monotonic()
try:
    owner.normalize_direct_systemd_timespan(Path({str(executable)!r}), {str(pid_file)!r}, command_timeout_seconds=.25, maximum_output_bytes=64)
except owner.DirectSystemdHostFailure:
    assert time.monotonic() - started < .25
else:
    raise AssertionError('group signal refusal was accepted')
analyzer, descendant = map(int, Path({str(pid_file)!r}).read_text().split())
try:
    os.kill(analyzer, 0)
except ProcessLookupError:
    pass
else:
    os.kill(analyzer, 9)
    raise AssertionError('owned analyzer was not reaped')
# The typed group-signal failure means descendants are not guaranteed; the proof owns cleanup.
try:
    os.kill(descendant, 9)
except ProcessLookupError:
    pass
"""

    subprocess.run((sys.executable, "-c", driver), check=True, timeout=1)
