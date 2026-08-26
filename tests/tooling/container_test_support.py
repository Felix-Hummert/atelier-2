from __future__ import annotations

import subprocess
import time
from pathlib import Path

_SUBPROCESS_STALL_SECONDS = 5.0
_SUBPROCESS_DEADLOCK_SECONDS = 60.0


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
