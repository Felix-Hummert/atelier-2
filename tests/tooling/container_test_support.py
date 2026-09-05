from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

_SUBPROCESS_STALL_SECONDS = 5.0
_SUBPROCESS_DEADLOCK_SECONDS = 60.0
_PROCESS_GROUP_TERMINATION_SECONDS = 1.0


@contextmanager
def started_process(
    arguments: Sequence[str], *, cwd: Path, env: Mapping[str, str]
) -> Iterator[subprocess.Popen[bytes]]:
    process = subprocess.Popen(
        arguments,
        cwd=cwd,
        env=env,
        start_new_session=True,
    )
    try:
        yield process
    finally:
        _terminate_process_group(process)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    process_group_id = process.pid
    _signal_process_group(process_group_id, signal.SIGTERM)
    if not _wait_for_process_exit(process) or not _wait_for_process_group_exit(
        process_group_id
    ):
        _signal_process_group(process_group_id, signal.SIGKILL)
        if not _wait_for_process_exit(process) or not _wait_for_process_group_exit(
            process_group_id
        ):
            raise RuntimeError(f"process group {process_group_id} did not terminate")


def _signal_process_group(process_group_id: int, interruption: signal.Signals) -> None:
    try:
        os.killpg(process_group_id, interruption)
    except ProcessLookupError:
        return


def _wait_for_process_group_exit(process_group_id: int) -> bool:
    deadline = time.monotonic() + _PROCESS_GROUP_TERMINATION_SECONDS
    while True:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def _wait_for_process_exit(process: subprocess.Popen[bytes]) -> bool:
    try:
        process.wait(timeout=_PROCESS_GROUP_TERMINATION_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return True


def _process_tree_progress(pid: int) -> tuple[int, int]:
    cpu = 0
    io_chars = 0
    pending = [pid]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        proc = Path("/proc") / str(current)
        try:
            fields = (
                (proc / "stat").read_text(encoding="ascii").rpartition(")")[2].split()
            )
        except OSError:
            continue
        cpu += int(fields[11]) + int(fields[12])
        try:
            io_text = (proc / "io").read_text(encoding="ascii")
        except OSError:
            pass
        else:
            for line in io_text.splitlines():
                name, separator, value = line.partition(": ")
                if separator and name in ("rchar", "wchar"):
                    io_chars += int(value)
        try:
            children = (proc / "task" / str(current) / "children").read_text(
                encoding="ascii"
            )
        except OSError:
            continue
        pending.extend(int(token) for token in children.split())
    return cpu, io_chars


def _workspace_progress(workspace: Path) -> tuple[int, int]:
    files = 0
    size = 0
    for path in workspace.rglob("*"):
        try:
            if path.is_file():
                size += path.stat().st_size
                files += 1
        except OSError:
            continue
    return files, size


def _observed_progress(
    process: subprocess.Popen[bytes], workspace: Path
) -> tuple[int, int, int, int]:
    pid = process.pid
    cpu, io_chars = (0, 0) if pid is None else _process_tree_progress(pid)
    files, size = _workspace_progress(workspace)
    return cpu, io_chars, files, size


def wait_until_exists(
    path: Path, process: subprocess.Popen[bytes], message: str
) -> None:
    """Wait for the stub's phase marker, extending while the process works.

    A single 5s wall clock races pytest-xdist scheduling: git, bash, and the
    Docker stub are real processes whose work can slow down without stalling.
    The wait renews itself for as long as CPU, I/O, or workspace files
    advance, and only gives up once they freeze (or a generous absolute
    ceiling is reached, as a backstop against a genuine deadlock).
    """
    ceiling = time.monotonic() + _SUBPROCESS_DEADLOCK_SECONDS
    stall_deadline = time.monotonic() + _SUBPROCESS_STALL_SECONDS
    observed: tuple[int, int, int, int] | None = None
    while not path.exists():
        now = time.monotonic()
        if now >= ceiling or now >= stall_deadline:
            raise AssertionError(message)
        if process.poll() is not None:
            raise AssertionError(f"{message} (process exited {process.returncode})")
        current = _observed_progress(process, path.parent)
        if current != observed:
            observed = current
            stall_deadline = time.monotonic() + _SUBPROCESS_STALL_SECONDS
        time.sleep(0.01)


def wait_for_exit(
    process: subprocess.Popen[bytes], workspace: Path, message: str
) -> int:
    """Wait for the subprocess to exit, extending while cleanup works."""
    ceiling = time.monotonic() + _SUBPROCESS_DEADLOCK_SECONDS
    stall_deadline = time.monotonic() + _SUBPROCESS_STALL_SECONDS
    observed: tuple[int, int, int, int] | None = None
    while True:
        now = time.monotonic()
        if now >= ceiling or now >= stall_deadline:
            raise AssertionError(message)
        timeout = min(0.05, stall_deadline - now, ceiling - now)
        try:
            return process.wait(timeout=max(timeout, 0.0))
        except subprocess.TimeoutExpired:
            current = _observed_progress(process, workspace)
            if current != observed:
                observed = current
                stall_deadline = time.monotonic() + _SUBPROCESS_STALL_SECONDS
