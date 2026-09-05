from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from atelier2.contracts.agent_attempts import RunnerCancellationObservation

_MAXIMUM_PATH_BYTES = 4096
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_ACCESS_EXECUTE = 1 << 0
_ACCESS_WRITE_FILE = 1 << 1
_ACCESS_READ_FILE = 1 << 2
_ACCESS_READ_DIR = 1 << 3
_ACCESS_REMOVE_DIR = 1 << 4
_ACCESS_REMOVE_FILE = 1 << 5
_ACCESS_MAKE_DIR = 1 << 7
_ACCESS_MAKE_REGULAR = 1 << 8
_ACCESS_MAKE_SOCKET = 1 << 9
_ACCESS_MAKE_FIFO = 1 << 10
_ACCESS_MAKE_SYMLINK = 1 << 12
REQUIRED_LANDLOCK_ABI = 1
_HANDLED_ACCESS = (1 << 13) - 1


class RunnerPathRight(StrEnum):
    """What a Runner's provider child may do beneath one attested path.

    Execution is its own right rather than a property of being readable,
    because the two differ exactly where it matters: the image root holds the
    interpreter and the provider CLI and must be executable, while a surface
    that carries a provider's own configuration -- plugins, hooks, shell
    snippets a real credential directory is full of -- must be readable and
    never runnable. A mount option cannot carry that distinction here, because
    the one host surface ADR 0009 sec. 2 admits is a bind mount and the
    launcher cannot mount it `noexec`, so the right does.
    """

    READ_AND_EXECUTE = "read-and-execute"
    READ_ONLY = "read-only"
    READ_WRITE = "read-write"


@dataclass(frozen=True)
class RunnerPathGrant:
    """One attested path of a provider child's filesystem surface, with its right."""

    path: PurePosixPath
    right: RunnerPathRight

    def __post_init__(self) -> None:
        if not isinstance(self.path, PurePosixPath) or not self.path.is_absolute():
            raise ValueError("a runner path grant must name an absolute POSIX path")
        if ".." in self.path.parts:
            raise ValueError("a runner path grant must name a normalized path")
        if len(self.path.as_posix().encode("utf-8")) > _MAXIMUM_PATH_BYTES:
            raise ValueError("a runner path grant must name a bounded path")


# `EXECUTE` is granted only where the image root's own code lives. Neither
# other right carries it, and both drop the device-node rights as well:
#
#   * a read-only grant is data the child reads and must never run. It is what
#     the one admitted host bind -- the provider's credential directory --
#     receives, and the right is the only fence available there, because the
#     launcher cannot mount a bind `noexec` the way it mounts a tmpfs.
#   * a writable grant is data the child produces. Code it could first write
#     and then run is exactly the widening the manifest attestation and the
#     `noexec` tmpfs are there to stop -- two independent fences, not one.
_GRANTED_ACCESS = {
    RunnerPathRight.READ_AND_EXECUTE: (
        _ACCESS_EXECUTE | _ACCESS_READ_FILE | _ACCESS_READ_DIR
    ),
    RunnerPathRight.READ_ONLY: _ACCESS_READ_FILE | _ACCESS_READ_DIR,
    RunnerPathRight.READ_WRITE: (
        _ACCESS_READ_FILE
        | _ACCESS_READ_DIR
        | _ACCESS_WRITE_FILE
        | _ACCESS_REMOVE_DIR
        | _ACCESS_REMOVE_FILE
        | _ACCESS_MAKE_DIR
        | _ACCESS_MAKE_REGULAR
        | _ACCESS_MAKE_SOCKET
        | _ACCESS_MAKE_FIFO
        | _ACCESS_MAKE_SYMLINK
    ),
}
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
    path_grants: tuple[RunnerPathGrant, ...] | None = None,
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
    if path_grants is not None:
        declared = tuple(
            (grant.path.as_posix(), grant.right.value) for grant in path_grants
        )
        launcher = (
            "import os, signal, sys\n"
            "from pathlib import PurePosixPath\n"
            "from atelier2.adapters.runner_child import (\n"
            "    RunnerPathGrant, RunnerPathRight, install_landlock_guard,\n"
            ")\n"
            "install_landlock_guard(tuple(\n"
            "    RunnerPathGrant(PurePosixPath(path), RunnerPathRight(right))\n"
            f"    for path, right in {declared!r}\n"
            "))\n"
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


def install_landlock_guard(path_grants: tuple[RunnerPathGrant, ...]) -> int:
    """Install a deny-by-default filesystem guard and return its kernel ABI.

    Every grant the caller names is the whole surface: a path outside them is
    denied, and a path named read-only stays read-only even where the mount
    beneath it would allow a write.
    """
    abi = _landlock_abi()
    if abi < REQUIRED_LANDLOCK_ABI:
        raise LandlockUnavailable("the kernel does not provide Landlock ABI 1")
    if not path_grants:
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
        for grant in path_grants:
            _allow_path(ruleset_descriptor, grant)
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


def _allow_path(ruleset_descriptor: int, grant: RunnerPathGrant) -> None:
    try:
        descriptor = os.open(grant.path, os.O_PATH | os.O_CLOEXEC)
    except OSError as error:
        raise LandlockUnavailable(
            f"the attested child surface {grant.path} is absent: {error}"
        ) from error
    try:
        attributes = _PathBeneathAttributes(_GRANTED_ACCESS[grant.right], descriptor)
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
