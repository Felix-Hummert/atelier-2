from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path, PurePosixPath

import pytest

from atelier2.adapters.runner_child import (
    LandlockUnavailable,
    RunnerPathGrant,
    RunnerPathRight,
    install_landlock_guard,
    reap_cancelled_runner_child,
    start_runner_child,
)
from atelier2.contracts.agent_attempts import RunnerCancellationObservation

_GUARD_IMPORTS = (
    "from pathlib import PurePosixPath\n"
    "from atelier2.adapters.runner_child import (\n"
    "    RunnerPathGrant, RunnerPathRight, install_landlock_guard,\n"
    ")\n"
)


def _read_only_grant(path: Path) -> RunnerPathGrant:
    return RunnerPathGrant(PurePosixPath(path), RunnerPathRight.READ_ONLY)


def _executable_grant(path: Path) -> RunnerPathGrant:
    return RunnerPathGrant(PurePosixPath(path), RunnerPathRight.READ_AND_EXECUTE)


def _read_only(path: Path) -> str:
    return f"RunnerPathGrant(PurePosixPath({str(path)!r}), RunnerPathRight.READ_ONLY)"


def _executable(path: Path) -> str:
    return (
        "RunnerPathGrant("
        f"PurePosixPath({str(path)!r}), RunnerPathRight.READ_AND_EXECUTE)"
    )


def _read_write(path: Path) -> str:
    return f"RunnerPathGrant(PurePosixPath({str(path)!r}), RunnerPathRight.READ_WRITE)"


def _interpreter_reachable_grants() -> tuple[RunnerPathGrant, ...]:
    return tuple(
        _executable_grant(path)
        for path in (
            Path("/usr"),
            Path("/lib"),
            Path("/lib64"),
            Path("/proc"),
            Path("/dev"),
            Path(sys.prefix),
            Path(sys.base_prefix),
        )
        if path.exists()
    )


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 2
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not path.is_file():
        raise AssertionError(f"file did not appear: {path}")


@pytest.mark.proves("runner-child-landlock")
def test_landlock_guard_denies_a_child_direct_read_of_runner_identity(
    tmp_path: Path,
) -> None:
    allowed = tmp_path / "workspace"
    identity = tmp_path / "identity"
    allowed.mkdir()
    identity.mkdir()
    (allowed / "job.txt").write_text("permitted", encoding="utf-8")
    key = identity / "client.key"
    key.write_text("not-for-child", encoding="utf-8")
    code = (
        "from pathlib import Path\n"
        f"{_GUARD_IMPORTS}"
        f"install_landlock_guard(({_read_only(allowed)},))\n"
        f"assert Path({str(allowed / 'job.txt')!r}).read_text() == 'permitted'\n"
        "try:\n"
        f"    Path({str(key)!r}).read_bytes()\n"
        "except PermissionError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(23)\n"
    )

    result = subprocess.run(
        (sys.executable, "-c", code), capture_output=True, check=False, text=True
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.proves("runner-child-landlock")
def test_landlock_guard_denies_a_write_beneath_a_read_only_grant(
    tmp_path: Path,
) -> None:
    """The right a grant carries, not merely its path, reaches the kernel."""
    readable = tmp_path / "image"
    writable = tmp_path / "scratch"
    readable.mkdir()
    writable.mkdir()
    code = (
        "from pathlib import Path\n"
        f"{_GUARD_IMPORTS}"
        "install_landlock_guard((\n"
        f"    {_read_only(readable)},\n"
        f"    {_read_write(writable)},\n"
        "))\n"
        f"Path({str(writable / 'kept.txt')!r}).write_text('written')\n"
        "try:\n"
        f"    Path({str(readable / 'denied.txt')!r}).write_text('nope')\n"
        "except PermissionError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(23)\n"
    )

    result = subprocess.run(
        (sys.executable, "-c", code), capture_output=True, check=False, text=True
    )

    assert result.returncode == 0, result.stderr
    assert (writable / "kept.txt").read_text(encoding="utf-8") == "written"


def test_landlock_refusal_is_loud_when_the_kernel_cannot_install_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("atelier2.adapters.runner_child._landlock_abi", lambda: 0)

    with pytest.raises(LandlockUnavailable):
        install_landlock_guard((_read_only_grant(Path("/tmp")),))


def test_landlock_refusal_names_an_attested_surface_this_image_lacks(
    tmp_path: Path,
) -> None:
    with pytest.raises(LandlockUnavailable, match="never-created"):
        install_landlock_guard((_read_only_grant(tmp_path / "never-created"),))


@pytest.mark.proves("runner-cancel-none")
def test_cancel_reap_observes_exit_before_signal() -> None:
    child = start_runner_child((sys.executable, "-c", "pass"))
    child.wait(timeout=2)

    assert (
        reap_cancelled_runner_child(child, 1, 5)
        is RunnerCancellationObservation.EXITED_BEFORE_SIGNAL
    )


@pytest.mark.proves("runner-cancel-none")
def test_cancel_reap_observes_term() -> None:
    child = start_runner_child((sys.executable, "-c", "import time; time.sleep(60)"))
    try:
        assert (
            reap_cancelled_runner_child(child, 1, 5)
            is RunnerCancellationObservation.REAPED_AFTER_TERM
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


@pytest.mark.proves("runner-cancel-none")
def test_cancel_reap_kills_a_child_that_ignores_term(tmp_path: Path) -> None:
    ready = tmp_path / "ready"
    child = start_runner_child(
        (
            sys.executable,
            "-c",
            "import signal,sys,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(sys.argv[1]).touch(); time.sleep(60)",
            str(ready),
        )
    )
    _wait_for_file(ready)
    try:
        assert (
            reap_cancelled_runner_child(child, 1, 5)
            is RunnerCancellationObservation.REAPED_AFTER_KILL
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


@pytest.mark.proves("runner-child-landlock")
def test_started_child_landlock_denies_identity(tmp_path: Path) -> None:
    identity = tmp_path / "identity"
    identity.mkdir()
    key = identity / "client.key"
    key.write_text("not-for-child", encoding="utf-8")
    allowed = _interpreter_reachable_grants()
    child = start_runner_child(
        (
            sys.executable,
            "-c",
            "import sys\nfrom pathlib import Path\ntry:\n    Path(sys.argv[1]).read_bytes()\nexcept PermissionError:\n    raise SystemExit(0)\nraise SystemExit(23)",
            str(key),
        ),
        allowed,
    )
    assert child.wait(timeout=5) == 0, child.stderr.read() if child.stderr else b""


def test_landlocked_child_reaps_after_term() -> None:
    allowed = _interpreter_reachable_grants()
    child = start_runner_child(
        (sys.executable, "-c", "import time; time.sleep(60)"), allowed
    )
    try:
        assert (
            reap_cancelled_runner_child(child, 1, 5)
            is RunnerCancellationObservation.REAPED_AFTER_TERM
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)


@pytest.mark.proves("runner-child-landlock")
def test_landlock_guard_denies_running_a_program_beneath_a_read_only_grant(
    tmp_path: Path,
) -> None:
    """A readable surface is not a runnable one.

    The one host surface ADR 0009 sec. 2 admits into a Runner is a bind mount
    of the provider's own credential directory, which the launcher cannot mount
    `noexec` the way it mounts a tmpfs. A real one carries plugins, hooks and
    shell snippets, so the grant is what has to stop the child running them --
    and that property lives in the manifest identity, not in a convention.
    """
    data = tmp_path / "credential-directory"
    data.mkdir()
    program = data / "hook.sh"
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    interpreter = Path(sys.executable).resolve()
    code = (
        "import subprocess, sys\n"
        f"{_GUARD_IMPORTS}"
        "install_landlock_guard((\n"
        f"    {_read_only(data)},\n"
        + "".join(
            f"    {_executable(path)},\n"
            for path in (interpreter.parent.parent, Path("/usr"), Path("/lib"))
            if path.exists()
        )
        + "))\n"
        f"assert open({str(program)!r}).read().startswith('#!')\n"
        "try:\n"
        f"    subprocess.run(({str(program)!r},), check=False)\n"
        "except PermissionError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(23)\n"
    )

    result = subprocess.run(
        (sys.executable, "-c", code), capture_output=True, check=False, text=True
    )

    assert result.returncode == 0, result.stderr
