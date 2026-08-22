from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from atelier2.contracts.agent_attempts import RunnerCancellationObservation

_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_ACCESS_EXECUTE = 1 << 0
_ACCESS_READ_FILE = 1 << 2
_ACCESS_READ_DIR = 1 << 3
REQUIRED_LANDLOCK_ABI = 1
_HANDLED_ACCESS = (1 << 13) - 1
_ALLOWED_READ_ONLY_ACCESS = _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_READ_DIR
_LIBC = ctypes.CDLL(None, use_errno=True)


class _RulesetAttributes(ctypes.Structure):
    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
    )


class _PathBeneathAttributes(ctypes.Structure):
    _pack_ = 1
    _fields_ = (("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32))


class LandlockUnavailable(RuntimeError):
    """The kernel cannot enforce the required child identity fence."""


class RunnerChildReapFailed(RuntimeError):
    """TERM then KILL did not reap the candidate child."""


def start_runner_child(
    command: tuple[str, ...],
    allowed_read_only_paths: tuple[Path, ...] | None = None,
    *,
    environment: tuple[tuple[str, str], ...] = (),
    standard_input: bytes = b"",
) -> subprocess.Popen[bytes]:
    """Start the one free child in its own session with identity descriptors closed.

    `environment` is the child's complete environment when given, never an
    overlay on this process's own -- the same contract `AgentProcessCommand`
    declares. An empty tuple, the default, inherits this process's own
    environment exactly as before this parameter existed, so every existing
    caller that never named one is unaffected. `standard_input` is written and
    the pipe closed before this call returns; every job document this
    candidate consumes today is small enough that the write completes without
    the child needing to already be draining it.
    """
    launched = command
    if allowed_read_only_paths is not None:
        allowed = (
            "(" + ", ".join(repr(str(path)) for path in allowed_read_only_paths) + ")"
        )
        launcher = (
            "import os, signal, sys\n"
            "from pathlib import Path\n"
            "from atelier2.adapters.runner_child import install_landlock_guard\n"
            f"install_landlock_guard(tuple(Path(path) for path in {allowed}))\n"
            "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
            "os.execvp(sys.argv[1], sys.argv[1:])\n"
        )
        launched = (sys.executable, "-c", launcher, *command)
    process = subprocess.Popen(
        launched,
        env=dict(environment) if environment else None,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
        start_new_session=True,
    )
    assert process.stdin is not None
    try:
        if standard_input:
            process.stdin.write(standard_input)
        process.stdin.close()
    except OSError:
        process.kill()
        process.wait()
        raise
    return process


def reap_cancelled_runner_child(
    child: subprocess.Popen[bytes],
    term_grace: float,
    reap_wait: float,
) -> RunnerCancellationObservation:
    """Send TERM, then KILL, and return the one physical observation."""
    if child.poll() is not None:
        return RunnerCancellationObservation.EXITED_BEFORE_SIGNAL
    pid = child.pid
    if pid is None:
        raise RunnerChildReapFailed("runner child has no pid")
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        child.wait()
        return RunnerCancellationObservation.EXITED_BEFORE_SIGNAL
    try:
        child.wait(term_grace)
        return RunnerCancellationObservation.REAPED_AFTER_TERM
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        child.wait()
        return RunnerCancellationObservation.REAPED_AFTER_TERM
    try:
        child.wait(reap_wait)
    except subprocess.TimeoutExpired as error:
        raise RunnerChildReapFailed("runner child survived SIGKILL") from error
    return RunnerCancellationObservation.REAPED_AFTER_KILL


def install_landlock_guard(allowed_read_only_paths: tuple[Path, ...]) -> int:
    """Install a deny-by-default filesystem guard and return its kernel ABI."""
    abi = _landlock_abi()
    if abi < REQUIRED_LANDLOCK_ABI:
        raise LandlockUnavailable("the kernel does not provide Landlock ABI 1")
    if not allowed_read_only_paths:
        raise LandlockUnavailable("the child has no Landlock allowlist")
    _call(_LIBC.prctl, _PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    ruleset = _RulesetAttributes(_HANDLED_ACCESS, 0)
    ruleset_descriptor = _call(
        _LIBC.syscall,
        _LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset),
        ctypes.sizeof(ruleset),
        0,
    )
    try:
        for path in allowed_read_only_paths:
            _allow_path(ruleset_descriptor, path)
        _call(_LIBC.syscall, _LANDLOCK_RESTRICT_SELF, ruleset_descriptor, 0)
    finally:
        os.close(ruleset_descriptor)
    return abi


def landlock_kernel_abi() -> int:
    return _landlock_abi()


def _landlock_abi() -> int:
    value = _LIBC.syscall(
        _LANDLOCK_CREATE_RULESET, None, 0, _LANDLOCK_CREATE_RULESET_VERSION
    )
    return max(0, value)


def _allow_path(ruleset_descriptor: int, path: Path) -> None:
    descriptor = os.open(path, os.O_PATH | os.O_CLOEXEC)
    try:
        attributes = _PathBeneathAttributes(_ALLOWED_READ_ONLY_ACCESS, descriptor)
        _call(
            _LIBC.syscall,
            _LANDLOCK_ADD_RULE,
            ruleset_descriptor,
            _LANDLOCK_RULE_PATH_BENEATH,
            ctypes.byref(attributes),
            0,
        )
    finally:
        os.close(descriptor)


def _call(function: Callable[..., object], *arguments: object) -> int:
    result = function(*arguments)
    if type(result) is not int:
        raise LandlockUnavailable("Landlock syscall returned no integer")
    if result == -1:
        raise LandlockUnavailable(os.strerror(ctypes.get_errno()))
    return result
