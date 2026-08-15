from __future__ import annotations

import ctypes
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from atelier2.adapters import agent_process_exec_guard as guard_module
from atelier2.adapters.agent_process_exec_guard import main
from atelier2.adapters.agent_process_protocol import (
    PROVIDER_ENVIRONMENT_CHANNEL,
    encode_provider_environment,
)

_PROVIDER_COMMAND = ("/bin/echo", "provider")


class _FakeLibc:
    """The one libc call the guard makes, without arming this test process."""

    def __init__(self, result: int = 0, errno: int = 0) -> None:
        self.result = result
        self.errno = errno
        self.armings: list[tuple[int, int]] = []

    def prctl(self, option: int, argument: int) -> int:
        self.armings.append((option, argument))
        if self.result != 0:
            ctypes.set_errno(self.errno)
        return self.result


@pytest.fixture
def libc(monkeypatch: pytest.MonkeyPatch) -> _FakeLibc:
    fake = _FakeLibc()
    monkeypatch.setattr(
        guard_module.ctypes, "CDLL", lambda *_arguments, **_keywords: fake
    )
    return fake


@pytest.fixture
def cgroup(tmp_path: Path) -> Path:
    return tmp_path


@dataclass(frozen=True)
class _Execution:
    file: str
    arguments: tuple[str, ...]
    environment: Mapping[str, str]


@pytest.fixture
def executions(monkeypatch: pytest.MonkeyPatch) -> list[_Execution]:
    """Replace the one irreversible step so everything before it stays testable."""

    recorded: list[_Execution] = []

    def record_exec(
        file: str, arguments: Sequence[str], environment: Mapping[str, str]
    ) -> None:
        recorded.append(_Execution(file, tuple(arguments), dict(environment)))

    monkeypatch.setattr(guard_module.os, "execvpe", record_exec)
    return recorded


def _arguments(cgroup: Path, watchdog_pid: int) -> list[str]:
    return [
        "--cgroup",
        str(cgroup),
        "--watchdog-pid",
        str(watchdog_pid),
        "--",
        *_PROVIDER_COMMAND,
    ]


def test_a_guard_without_a_provider_command_refuses_to_run(cgroup: Path) -> None:
    with pytest.raises(SystemExit):
        main(["--cgroup", str(cgroup), "--watchdog-pid", str(os.getppid())])


def test_a_guard_without_its_environment_channel_refuses_to_run(
    cgroup: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(PROVIDER_ENVIRONMENT_CHANNEL, raising=False)

    with pytest.raises(SystemExit):
        main(_arguments(cgroup, os.getppid()))


def test_a_guard_whose_watchdog_vanished_before_arming_refuses_to_exec(
    cgroup: Path, monkeypatch: pytest.MonkeyPatch, executions: list[_Execution]
) -> None:
    monkeypatch.setenv(PROVIDER_ENVIRONMENT_CHANNEL, encode_provider_environment({}))

    with pytest.raises(RuntimeError, match="before exec guard armed"):
        main(_arguments(cgroup, os.getpid()))

    assert executions == []
    assert not (cgroup / "cgroup.procs").exists()


def test_a_guard_whose_watchdog_vanished_while_arming_refuses_to_exec(
    cgroup: Path,
    monkeypatch: pytest.MonkeyPatch,
    libc: _FakeLibc,
    executions: list[_Execution],
) -> None:
    watchdog_pid = os.getppid()
    observations = iter((watchdog_pid, watchdog_pid + 1))
    monkeypatch.setattr(guard_module.os, "getppid", lambda: next(observations))
    monkeypatch.setenv(PROVIDER_ENVIRONMENT_CHANNEL, encode_provider_environment({}))

    with pytest.raises(RuntimeError, match="while exec guard armed"):
        main(_arguments(cgroup, watchdog_pid))

    assert libc.armings == [
        (guard_module.PARENT_DEATH_OPTION, guard_module.PARENT_DEATH_SIGNAL)
    ]
    assert executions == []
    assert not (cgroup / "cgroup.procs").exists()


def test_a_guard_that_cannot_arm_parent_death_refuses_to_exec(
    cgroup: Path,
    monkeypatch: pytest.MonkeyPatch,
    libc: _FakeLibc,
    executions: list[_Execution],
) -> None:
    libc.result = -1
    libc.errno = 1
    monkeypatch.setenv(PROVIDER_ENVIRONMENT_CHANNEL, encode_provider_environment({}))

    with pytest.raises(OSError):
        main(_arguments(cgroup, os.getppid()))

    assert executions == []
    assert not (cgroup / "cgroup.procs").exists()


def test_an_armed_guard_joins_its_containment_before_it_execs(
    cgroup: Path,
    monkeypatch: pytest.MonkeyPatch,
    libc: _FakeLibc,
    executions: list[_Execution],
) -> None:
    monkeypatch.setenv(
        PROVIDER_ENVIRONMENT_CHANNEL, encode_provider_environment({"NAME": "value"})
    )

    main(_arguments(cgroup, os.getppid()))

    assert libc.armings == [
        (guard_module.PARENT_DEATH_OPTION, guard_module.PARENT_DEATH_SIGNAL)
    ]
    assert (cgroup / "cgroup.procs").read_text(encoding="ascii") == str(os.getpid())
    assert executions == [
        _Execution(_PROVIDER_COMMAND[0], _PROVIDER_COMMAND, {"NAME": "value"})
    ]


def test_an_armed_guard_execs_with_exactly_the_environment_it_was_given(
    cgroup: Path,
    monkeypatch: pytest.MonkeyPatch,
    libc: _FakeLibc,
    executions: list[_Execution],
) -> None:
    del libc
    declared = {"NAME": "value", "ÜBER": "gerüst"}
    monkeypatch.setenv(
        PROVIDER_ENVIRONMENT_CHANNEL, encode_provider_environment(declared)
    )

    main(_arguments(cgroup, os.getppid()))

    assert [execution.environment for execution in executions] == [declared]
    assert PROVIDER_ENVIRONMENT_CHANNEL not in os.environ
