from __future__ import annotations

import fcntl
import multiprocessing
import os
import shlex
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from queue import Empty

import pytest

from atelier2.adapters.systemd_agent_collector import (
    DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME,
    MAXIMUM_DIRECT_SYSTEMD_LAUNCH_ENVELOPE_BYTES,
    encode_direct_systemd_launch_envelope,
)
from atelier2.adapters.systemd_agent_processes import (
    DirectSystemdAgentProcessConfiguration,
    DirectSystemdAgentProcessManager,
    DirectSystemdPossiblyRan,
    direct_systemd_unit_name,
)
from atelier2.adapters.systemd_generation_records import (
    DirectSystemdResultOutcome,
)
from atelier2.adapters.systemd_timespans import DirectSystemdHostFailure
from atelier2.contracts.agent_attempts import AgentAttemptId, WatchdogGenerationId
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    AgentProcessCommand,
    AgentProcessInvocation,
)
from tests.scenarios.agents import leased_directory_identity

pytestmark = pytest.mark.xdist_group("direct-systemd-user-manager")
_SYSTEMCTL_TIMEOUT_SECONDS = 3


def _configuration(
    *, runtime_max_seconds: int = 5
) -> DirectSystemdAgentProcessConfiguration:
    paths = {
        name: shutil.which(name)
        for name in ("systemd-run", "systemctl", "systemd-analyze")
    }
    if any(value is None for value in paths.values()):
        pytest.fail("the direct systemd lifecycle requires the systemd executables")
    version = subprocess.run(
        (str(paths["systemctl"]), "--version"),
        capture_output=True,
        text=True,
        check=True,
        timeout=_SYSTEMCTL_TIMEOUT_SECONDS,
    ).stdout.splitlines()[0]
    if not version.startswith("systemd 255 "):
        message = f"the direct systemd proof is pinned to systemd 255, found {version}"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    runtime = Path(f"/run/user/{os.getuid()}")
    if (
        not runtime.is_dir()
        or _systemctl(Path(str(paths["systemctl"])), "is-system-running").returncode
    ):
        message = "a running user systemd manager is required"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    return DirectSystemdAgentProcessConfiguration(
        Path(str(paths["systemd-run"])),
        Path(str(paths["systemctl"])),
        Path(str(paths["systemd-analyze"])),
        Path(sys.executable),
        runtime,
        1,
        runtime_max_seconds,
        2.0,
        1.0,
        2.0,
        0.02,
    )


def _systemctl(executable: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        (str(executable), "--user", *arguments),
        capture_output=True,
        text=True,
        check=False,
        timeout=_SYSTEMCTL_TIMEOUT_SECONDS,
        env={
            "LC_ALL": "C",
            "SYSTEMD_COLORS": "0",
            "SYSTEMD_URLIFY": "0",
            "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}",
        },
    )


def _provider(root: Path) -> Path:
    executable = root / "provider"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import os,signal,subprocess,sys,time\n"
        "mode=sys.argv[1]\n"
        "if mode in {'write','both-overflow'}:\n"
        " streams=(int(sys.argv[2]),) if mode=='write' else (1,2)\n"
        " for descriptor in streams:\n"
        "  payload=bytes([int(sys.argv[3])])*int(sys.argv[4])\n"
        "  while payload:\n"
        "   try:payload=payload[os.write(descriptor,payload):]\n"
        "   except BrokenPipeError:break\n"
        " sys.exit()\n"
        "ready,release,count=sys.argv[2:5]\n"
        "with open(count,'ab',buffering=0) as stream:stream.write(f'{os.getpid()}\\n'.encode())\n"
        "if mode in {'descendants','overflow-survivor'}:\n"
        " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        " code='import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)'\n"
        " inherited=subprocess.Popen((sys.executable,'-c',code))\n"
        " detached=subprocess.Popen((sys.executable,'-c',code),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        " open(ready+'.pids','w').write(f'{os.getpid()} {inherited.pid} {detached.pid}')\n"
        "open(ready,'w').write('ready')\n"
        "if mode=='overflow-survivor':os.write(1,b'x'*18)\n"
        "while not os.path.exists(release):time.sleep(.01)\n"
        "os.write(1,b'answer')\n"
    )
    executable.chmod(0o700)
    return executable


def _identity(seed: str) -> tuple[AgentAttemptId, WatchdogGenerationId, str]:
    attempt_id = AgentAttemptId.of(seed.encode())
    generation_id = WatchdogGenerationId(f"{seed}-generation")
    unit = direct_systemd_unit_name(attempt_id, generation_id)
    manifest = os.environ.get("ATELIER2_SYSTEMD_UNIT_MANIFEST")
    if manifest:
        descriptor = os.open(
            manifest, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            payload = f"{unit}\n".encode("ascii")
            if os.write(descriptor, payload) != len(payload):
                raise OSError("direct-systemd unit manifest write was incomplete")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return attempt_id, generation_id, unit


def _wait_until(predicate: object, message: str, timeout: float = 3) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(predicate) and predicate():
            return
        time.sleep(0.02)
    pytest.fail(message)


def _unit_state(
    configuration: DirectSystemdAgentProcessConfiguration, unit: str
) -> str:
    answer = _systemctl(
        configuration.systemctl,
        "show",
        unit,
        "--property=LoadState",
        "--value",
        "--no-pager",
    )
    assert answer.returncode == 0
    return answer.stdout.strip()


def _show(
    configuration: DirectSystemdAgentProcessConfiguration,
    unit: str,
    *properties: str,
) -> dict[str, str]:
    answer = _systemctl(
        configuration.systemctl,
        "show",
        unit,
        *(f"--property={name}" for name in properties),
        "--no-pager",
    )
    assert answer.returncode == 0, answer.stderr
    return dict(line.split("=", 1) for line in answer.stdout.splitlines())


def _cleanup_unit(
    configuration: DirectSystemdAgentProcessConfiguration, unit: str
) -> None:
    _systemctl(configuration.systemctl, "stop", unit)
    _systemctl(configuration.systemctl, "reset-failed", unit)
    _wait_until(
        lambda: _unit_state(configuration, unit) == "not-found",
        f"unit {unit} remained after exact cleanup",
    )


def _assert_no_processes(process_file: Path) -> None:
    if not process_file.exists():
        return
    for process_id in map(int, process_file.read_text().split()):
        _wait_until(
            lambda process_id=process_id: not Path(f"/proc/{process_id}").exists(),
            f"process {process_id} remained",
        )


def _credential_directory(process_id: str) -> Path:
    environment = Path(f"/proc/{process_id}/environ").read_bytes().split(b"\0")
    return Path(
        next(
            value.split(b"=", 1)[1].decode()
            for value in environment
            if value.startswith(b"CREDENTIALS_DIRECTORY=")
        )
    )


def _assert_no_residue(
    configuration: DirectSystemdAgentProcessConfiguration,
    unit: str,
    generation: Path,
    *,
    control_group: str | None = None,
    credential_directory: Path | None = None,
    process_file: Path | None = None,
) -> None:
    _wait_until(
        lambda: _unit_state(configuration, unit) == "not-found",
        f"unit {unit} was not collected",
    )
    assert not (generation / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME).exists()
    if control_group:
        cgroup_path = Path("/sys/fs/cgroup") / control_group.removeprefix("/")
        _wait_until(
            lambda: not cgroup_path.exists(), f"cgroup {control_group} remained"
        )
    if credential_directory is not None:
        _wait_until(
            lambda: not credential_directory.exists(),
            f"credential directory {credential_directory} remained",
        )
    if process_file is not None:
        _assert_no_processes(process_file)


def _invocation(
    provider: Path,
    root: Path,
    *arguments: str,
    output_limit: int = 17,
) -> AgentProcessInvocation:
    return AgentProcessInvocation(
        AgentProcessCommand(
            (str(provider), *arguments), standard_output_frame_bytes=output_limit
        ),
        leased_directory_identity(AgentAttemptId.of(b"lifecycle-attempt"), root),
    )


def _controller_start(
    configuration: DirectSystemdAgentProcessConfiguration,
    attempt_id: AgentAttemptId,
    generation_id: WatchdogGenerationId,
    generation: Path,
    invocation: AgentProcessInvocation,
    answers: multiprocessing.Queue[tuple[str, str]],
) -> None:
    try:
        inspection = DirectSystemdAgentProcessManager(configuration).start(
            attempt_id, generation_id, generation, invocation
        )
    except (OSError, RuntimeError, ValueError) as error:  # pragma: no cover
        answers.put(("error", repr(error)))
    else:
        answers.put(("ok", inspection.state.value))


def _controller_start_and_hold(
    configuration: DirectSystemdAgentProcessConfiguration,
    attempt_id: AgentAttemptId,
    generation_id: WatchdogGenerationId,
    generation: Path,
    invocation: AgentProcessInvocation,
) -> None:
    DirectSystemdAgentProcessManager(configuration).start(
        attempt_id, generation_id, generation, invocation
    )
    time.sleep(30)


def _trace_events(prefix: Path) -> list[str]:
    timestamped: list[tuple[float, str]] = []
    for path in prefix.parent.glob(f"{prefix.name}*"):
        for line in path.read_text(errors="strict").splitlines():
            timestamp, separator, event = line.partition(" ")
            if separator:
                timestamped.append((float(timestamp), event))
    return [event for _timestamp, event in sorted(timestamped)]


def _assert_ordered_events(
    events: list[str], predicates: tuple[tuple[str, ...], ...]
) -> None:
    cursor = 0
    for needles in predicates:
        for index in range(cursor, len(events)):
            if all(needle in events[index] for needle in needles):
                cursor = index + 1
                break
        else:
            pytest.fail(f"trace never reached ordered event {needles!r}")


def test_live_unit_has_exact_identity_and_credential_then_collects_cleanly(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    manager = DirectSystemdAgentProcessManager(configuration)
    provider = _provider(tmp_path)
    generation = tmp_path / "generation"
    ready, release, count = (tmp_path / name for name in ("ready", "release", "count"))
    marker = "credential-only-marker-7da4"
    invocation = _invocation(
        provider, tmp_path, "block", str(ready), str(release), str(count), marker
    )
    attempt_id, generation_id, unit = _identity("live-identity")
    try:
        manager.probe()
        inspection = manager.start(attempt_id, generation_id, generation, invocation)
        assert inspection.started is not None and inspection.result is None
        _wait_until(ready.exists, "provider did not reach its live barrier")
        properties = _show(
            configuration,
            unit,
            "LoadState",
            "Id",
            "Type",
            "ExitType",
            "KillMode",
            "TimeoutStopUSec",
            "RuntimeMaxUSec",
            "SendSIGKILL",
            "CollectMode",
            "WorkingDirectory",
            "InvocationID",
            "ExecStart",
            "ControlGroup",
            "MainPID",
        )
        expected = {
            "LoadState": "loaded",
            "Id": unit,
            "Type": "exec",
            "ExitType": "main",
            "KillMode": "control-group",
            "TimeoutStopUSec": "1s",
            "RuntimeMaxUSec": "5s",
            "SendSIGKILL": "yes",
            "CollectMode": "inactive-or-failed",
            "WorkingDirectory": str(generation),
            "InvocationID": inspection.started.invocation_id.value,
        }
        assert {name: properties[name] for name in expected} == expected
        assert properties["ExecStart"].startswith(
            f"{{ path={sys.executable} ; argv[]={sys.executable} -m atelier2.adapters.systemd_agent_collector ; ignore_errors=no ; "
        )
        unit_file = _systemctl(configuration.systemctl, "cat", unit, "--no-pager")
        assert unit_file.returncode == 0
        credential = (
            f"LoadCredential=atelier2-launch:"
            f"{generation / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME}"
        )
        assert [
            line.strip()
            for line in unit_file.stdout.splitlines()
            if line.strip().startswith("LoadCredential=")
        ] == [credential]
        show_all = _systemctl(configuration.systemctl, "show", "--all", unit)
        assert show_all.returncode == 0 and marker not in show_all.stdout
        credential_directory = _credential_directory(properties["MainPID"])
        credential_copy = credential_directory / "atelier2-launch"
        assert credential_copy.read_bytes() == encode_direct_systemd_launch_envelope(
            invocation
        )
        assert stat.S_IMODE(credential_copy.stat().st_mode) in {0o400, 0o600}
        release.touch()
        result = manager.wait(generation)
        assert result.outcome is DirectSystemdResultOutcome.COMPLETED
        assert result.standard_output == b"answer"
        _assert_no_residue(
            configuration,
            unit,
            generation,
            control_group=properties["ControlGroup"],
            credential_directory=credential_directory,
            process_file=count,
        )
    finally:
        release.touch(exist_ok=True)
        _cleanup_unit(configuration, unit)


@pytest.mark.parametrize(
    ("descriptor", "count", "outcome", "overflow"),
    [
        (1, 17, DirectSystemdResultOutcome.COMPLETED, False),
        (1, 18, DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED, True),
        (
            2,
            MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
            DirectSystemdResultOutcome.COMPLETED,
            False,
        ),
        (
            2,
            MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES + 1,
            DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED,
            True,
        ),
    ],
)
def test_real_stream_boundaries_are_owned_by_the_invocation(
    tmp_path: Path,
    descriptor: int,
    count: int,
    outcome: DirectSystemdResultOutcome,
    overflow: bool,
) -> None:
    configuration = _configuration()
    manager = DirectSystemdAgentProcessManager(configuration)
    provider = _provider(tmp_path)
    generation = tmp_path / "generation"
    attempt_id, generation_id, unit = _identity(f"stream-{descriptor}-{count}")
    invocation = _invocation(
        provider, tmp_path, "write", str(descriptor), str(120 + descriptor), str(count)
    )
    try:
        manager.start(attempt_id, generation_id, generation, invocation)
        result = manager.wait(generation)
        assert result.outcome is outcome
        assert result.standard_output_overflow is (overflow and descriptor == 1)
        assert result.standard_error_overflow is (overflow and descriptor == 2)
        expected = bytes([120 + descriptor]) * (
            17 if descriptor == 1 else MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
        )
        assert (
            result.standard_output if descriptor == 1 else result.standard_error
        ) == expected
        _assert_no_residue(configuration, unit, generation)
    finally:
        _cleanup_unit(configuration, unit)


def test_both_streams_overflow_in_one_real_process(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    manager = DirectSystemdAgentProcessManager(configuration)
    provider = _provider(tmp_path)
    generation = tmp_path / "generation"
    count = MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES + 1
    attempt_id, generation_id, unit = _identity("both-streams-overflow")
    invocation = _invocation(
        provider,
        tmp_path,
        "both-overflow",
        "1",
        "122",
        str(count),
        output_limit=MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    )
    try:
        manager.start(attempt_id, generation_id, generation, invocation)
        result = manager.wait(generation)
        assert result.outcome is DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED
        assert result.standard_output_overflow and result.standard_error_overflow
        assert result.standard_output == b"z" * (count - 1)
        assert result.standard_error == b"z" * (count - 1)
        _assert_no_residue(configuration, unit, generation)
    finally:
        _cleanup_unit(configuration, unit)


def test_systemd_255_loads_the_exact_maximum_launch_credential(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    manager = DirectSystemdAgentProcessManager(configuration)
    provider = _provider(tmp_path)
    generation = tmp_path / "generation"
    attempt_id, generation_id, unit = _identity("maximum-credential")
    arguments = (str(provider), "write", "1", "120", "6")
    empty_environment = (("PAD_A", ""), ("PAD_B", ""), ("PAD_C", ""))
    prototype = AgentProcessInvocation(
        AgentProcessCommand(
            arguments, empty_environment, standard_output_frame_bytes=17
        ),
        leased_directory_identity(AgentAttemptId.of(b"envelope-attempt"), tmp_path),
    )
    filler_bytes = MAXIMUM_DIRECT_SYSTEMD_LAUNCH_ENVELOPE_BYTES - len(
        encode_direct_systemd_launch_envelope(prototype)
    )
    environment: list[tuple[str, str]] = []
    for name in ("PAD_A", "PAD_B", "PAD_C"):
        count = min(filler_bytes, 100_000)
        environment.append((name, "x" * count))
        filler_bytes -= count
    assert filler_bytes == 0
    invocation = AgentProcessInvocation(
        AgentProcessCommand(
            arguments, tuple(environment), standard_output_frame_bytes=17
        ),
        leased_directory_identity(AgentAttemptId.of(b"envelope-attempt"), tmp_path),
    )
    assert (
        len(encode_direct_systemd_launch_envelope(invocation))
        == MAXIMUM_DIRECT_SYSTEMD_LAUNCH_ENVELOPE_BYTES
    )
    try:
        manager.start(attempt_id, generation_id, generation, invocation)
        result = manager.wait(generation)
        assert result.outcome is DirectSystemdResultOutcome.COMPLETED
        assert result.standard_output == b"x" * 6
        _assert_no_residue(configuration, unit, generation)
    finally:
        _cleanup_unit(configuration, unit)


@pytest.mark.parametrize(
    ("mode", "via_runtime_max"),
    [("descendants", False), ("descendants", True), ("overflow-survivor", True)],
)
def test_stop_and_runtime_max_remove_the_whole_control_group(
    tmp_path: Path, mode: str, via_runtime_max: bool
) -> None:
    configuration = _configuration(runtime_max_seconds=1 if via_runtime_max else 5)
    manager = DirectSystemdAgentProcessManager(configuration)
    provider = _provider(tmp_path)
    generation = tmp_path / "generation"
    ready, release, count = (tmp_path / name for name in ("ready", "release", "count"))
    invocation = _invocation(
        provider, tmp_path, mode, str(ready), str(release), str(count)
    )
    attempt_id, generation_id, unit = _identity(
        f"runtime-max-{mode}" if via_runtime_max else "explicit-stop"
    )
    try:
        inspection = manager.start(attempt_id, generation_id, generation, invocation)
        assert inspection.started is not None
        _wait_until(ready.exists, "provider descendants did not start")
        properties = _show(configuration, unit, "ControlGroup")
        if via_runtime_max:
            with pytest.raises(DirectSystemdPossiblyRan):
                manager.wait(generation)
        else:
            stopped = manager.stop(generation)
            assert stopped.started is not None and stopped.result is None
        _assert_no_residue(
            configuration,
            unit,
            generation,
            control_group=properties["ControlGroup"],
            process_file=ready.with_suffix(".pids"),
        )
    finally:
        release.touch(exist_ok=True)
        _cleanup_unit(configuration, unit)


@pytest.mark.proves("no-global-runtime-serialization-is-introduced")
def test_same_generation_is_single_flight_while_generations_overlap(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    provider = _provider(tmp_path)
    context = multiprocessing.get_context("spawn")
    answers: multiprocessing.Queue[tuple[str, str]] = context.Queue()
    units: list[str] = []
    releases: list[Path] = []
    try:
        same_generation = tmp_path / "same-generation"
        ready = tmp_path / "same-ready"
        release = tmp_path / "same-release"
        releases.append(release)
        count = tmp_path / "provider-count"
        invocation = _invocation(
            provider, tmp_path, "block", str(ready), str(release), str(count)
        )
        attempt_id, generation_id, unit = _identity("same-generation")
        units.append(unit)
        controllers = [
            context.Process(
                target=_controller_start,
                args=(
                    configuration,
                    attempt_id,
                    generation_id,
                    same_generation,
                    invocation,
                    answers,
                ),
            )
            for _index in range(2)
        ]
        for controller in controllers:
            controller.start()
        assert [answers.get(timeout=5)[0] for _index in controllers] == ["ok", "ok"]
        for controller in controllers:
            controller.join(timeout=2)
            assert controller.exitcode == 0
        _wait_until(
            lambda: count.exists() and len(count.read_text().splitlines()) == 1,
            "same-generation provider did not reach its barrier exactly once",
        )
        assert len(count.read_text().splitlines()) == 1
        release.touch()
        DirectSystemdAgentProcessManager(configuration).wait(same_generation)

        overlaps: list[tuple[Path, Path, AgentAttemptId, WatchdogGenerationId]] = []
        controllers = []
        for index in range(2):
            generation = tmp_path / f"parallel-generation-{index}"
            ready = tmp_path / f"parallel-ready-{index}"
            release = tmp_path / f"parallel-release-{index}"
            releases.append(release)
            attempt_id, generation_id, unit = _identity(f"parallel-{index}")
            units.append(unit)
            invocation = _invocation(
                provider, tmp_path, "block", str(ready), str(release), str(count)
            )
            overlaps.append((generation, ready, attempt_id, generation_id))
            controllers.append(
                context.Process(
                    target=_controller_start,
                    args=(
                        configuration,
                        attempt_id,
                        generation_id,
                        generation,
                        invocation,
                        answers,
                    ),
                )
            )
        for controller in controllers:
            controller.start()
        for _index in controllers:
            assert answers.get(timeout=5)[0] == "ok"
        _wait_until(
            lambda: all(
                ready.exists() for _generation, ready, _attempt, _id in overlaps
            ),
            "different-generation providers did not overlap at their barriers",
        )
        assert all(ready.exists() for _generation, ready, _attempt, _id in overlaps)
        _wait_until(
            lambda: count.exists() and len(count.read_text().splitlines()) == 3,
            "different generations did not launch exactly two providers",
        )
        assert len(count.read_text().splitlines()) == 3
        for release in releases[1:]:
            release.touch()
        for generation, _ready, _attempt, _id in overlaps:
            DirectSystemdAgentProcessManager(configuration).wait(generation)
        for controller in controllers:
            controller.join(timeout=2)
            assert controller.exitcode == 0
    except Empty:
        pytest.fail("a controller did not return its direct-systemd inspection")
    finally:
        for release in releases:
            release.touch(exist_ok=True)
        for unit in units:
            _cleanup_unit(configuration, unit)


def test_controller_death_is_adopted_without_a_second_provider(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    provider = _provider(tmp_path)
    generation = tmp_path / "generation"
    ready, release, count = (tmp_path / name for name in ("ready", "release", "count"))
    invocation = _invocation(
        provider, tmp_path, "block", str(ready), str(release), str(count)
    )
    attempt_id, generation_id, unit = _identity("controller-death")
    context = multiprocessing.get_context("spawn")
    controller = context.Process(
        target=_controller_start_and_hold,
        args=(configuration, attempt_id, generation_id, generation, invocation),
    )
    try:
        controller.start()
        _wait_until(ready.exists, "provider did not survive controller acceptance")
        controller.terminate()
        controller.join(timeout=2)
        assert controller.exitcode is not None
        replacement = DirectSystemdAgentProcessManager(configuration)
        inspection = replacement.start(
            attempt_id, generation_id, generation, invocation
        )
        assert inspection.started is not None and inspection.result is None
        assert len(count.read_text().splitlines()) == 1
        release.touch()
        assert replacement.wait(generation).standard_output == b"answer"
        _assert_no_residue(configuration, unit, generation, process_file=count)
    finally:
        if controller.is_alive():
            controller.terminate()
            controller.join(timeout=2)
        release.touch(exist_ok=True)
        _cleanup_unit(configuration, unit)


def test_one_attempt_has_durable_preparation_and_handoff_syscall_order(
    tmp_path: Path,
) -> None:
    strace = shutil.which("strace")
    if strace is None:
        if os.environ.get("CI"):
            pytest.fail("the direct systemd durability proof requires strace")
        pytest.skip("strace is unavailable")
    configuration = _configuration()
    provider = _provider(tmp_path)
    generation = tmp_path / "strace-generation"
    ready, release, count = (
        tmp_path / name for name in ("strace-ready", "strace-release", "strace-count")
    )
    _attempt_id, _generation_id, unit = _identity("strace-attempt")
    controller_trace = tmp_path / "controller.trace"
    collector_trace = tmp_path / "collector.trace"
    collector_python = tmp_path / "collector-python"
    collector_python.write_text(
        "#!/bin/sh\n"
        f"exec {shlex.quote(strace)} -ff -ttt -yy "
        "-e trace=openat,fsync,unlink,execve "
        f"-o {shlex.quote(str(collector_trace))} "
        f'{shlex.quote(sys.executable)} "$@"\n'
    )
    collector_python.chmod(0o700)
    traced_configuration = replace(configuration, python=collector_python)
    invocation = _invocation(
        provider, tmp_path, "block", str(ready), str(release), str(count)
    )
    helper = tmp_path / "controller.py"
    helper.write_text(
        "from pathlib import Path\n"
        "from atelier2.adapters.systemd_agent_processes import "
        "DirectSystemdAgentProcessConfiguration,DirectSystemdAgentProcessManager\n"
        "from atelier2.contracts.agent_attempts import "
        "AgentAttemptId,WatchdogGenerationId\n"
        "from atelier2.ports.agent_executions import "
        "AgentAttemptWorkspaceLease,AgentProcessCommand,AgentProcessInvocation\n"
        f"configuration=DirectSystemdAgentProcessConfiguration("
        f"Path({str(configuration.systemd_run)!r}),"
        f"Path({str(configuration.systemctl)!r}),"
        f"Path({str(configuration.systemd_analyze)!r}),"
        f"Path({str(collector_python)!r}),"
        f"Path({str(configuration.user_runtime_directory)!r}),1,5,2.,1.,2.,.02)\n"
        f"invocation=AgentProcessInvocation(AgentProcessCommand("
        f"{invocation.command.arguments!r},standard_output_frame_bytes="
        f"{invocation.command.standard_output_frame_bytes}),"
        f"AgentAttemptWorkspaceLease(AgentAttemptId.of(b'strace-attempt'),"
        f"Path({str(invocation.lease.working_directory)!r}),"
        f"{invocation.lease.device},{invocation.lease.inode}))\n"
        f"DirectSystemdAgentProcessManager(configuration).start("
        f"AgentAttemptId.of(b'strace-attempt'),"
        f"WatchdogGenerationId('strace-attempt-generation'),"
        f"Path({str(generation)!r}),invocation)\n"
    )
    try:
        answer = subprocess.run(
            (
                strace,
                "-ff",
                "-ttt",
                "-yy",
                "-e",
                "trace=openat,fsync,unlink,execve",
                "-o",
                str(controller_trace),
                sys.executable,
                str(helper),
            ),
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert answer.returncode == 0, answer.stderr
        _wait_until(ready.exists, "traced provider did not reach its barrier")
        release.touch()
        result = DirectSystemdAgentProcessManager(traced_configuration).wait(generation)
        assert result.outcome is DirectSystemdResultOutcome.COMPLETED

        controller_events = _trace_events(controller_trace)
        _assert_ordered_events(
            controller_events,
            (
                ("openat(", "launch-envelope", "O_EXCL"),
                ("fsync(", "launch-envelope"),
                ("fsync(", str(generation)),
                ("openat(", "INTENT", "O_EXCL"),
                ("fsync(", "INTENT"),
                ("fsync(", str(generation)),
                ("execve(", str(configuration.systemd_run)),
            ),
        )
        collector_events = _trace_events(collector_trace)
        _assert_ordered_events(
            collector_events,
            (
                ("openat(", "STARTED", "O_EXCL"),
                ("fsync(", "STARTED"),
                ("fsync(", str(generation)),
                ("unlink(", "launch-envelope"),
                ("fsync(", str(generation)),
                ("execve(", str(provider)),
            ),
        )
        _assert_no_residue(configuration, unit, generation, process_file=count)
    finally:
        release.touch(exist_ok=True)
        _cleanup_unit(configuration, unit)


@pytest.mark.skipif(
    os.environ.get("ATELIER2_ALLOW_USER_MANAGER_RESTART") != "1",
    reason="requires an explicitly disposable user manager",
)
def test_user_manager_restart_preserves_each_durable_recovery_state(
    tmp_path: Path,
) -> None:
    configuration = _configuration()
    manager = DirectSystemdAgentProcessManager(configuration)
    provider = _provider(tmp_path)
    units: list[str] = []
    releases: list[Path] = []

    retry_generation = tmp_path / "retry-generation"
    retry_ready = tmp_path / "retry-ready"
    retry_release = tmp_path / "retry-release"
    retry_count = tmp_path / "retry-count"
    releases.append(retry_release)
    retry_invocation = _invocation(
        provider,
        tmp_path,
        "block",
        str(retry_ready),
        str(retry_release),
        str(retry_count),
    )
    retry_attempt, retry_generation_id, retry_unit = _identity("restart-retry")
    units.append(retry_unit)

    possibly_generation = tmp_path / "possibly-generation"
    possibly_ready = tmp_path / "possibly-ready"
    possibly_release = tmp_path / "possibly-release"
    possibly_count = tmp_path / "possibly-count"
    releases.append(possibly_release)
    possibly_invocation = _invocation(
        provider,
        tmp_path,
        "block",
        str(possibly_ready),
        str(possibly_release),
        str(possibly_count),
    )
    possibly_attempt, possibly_generation_id, possibly_unit = _identity(
        "restart-possibly"
    )
    units.append(possibly_unit)

    result_generation = tmp_path / "result-generation"
    result_ready = tmp_path / "result-ready"
    result_release = tmp_path / "result-release"
    result_count = tmp_path / "result-count"
    releases.append(result_release)
    result_invocation = _invocation(
        provider,
        tmp_path,
        "block",
        str(result_ready),
        str(result_release),
        str(result_count),
    )
    result_attempt, result_generation_id, result_unit = _identity("restart-result")
    units.append(result_unit)

    try:
        missing_run = replace(configuration, systemd_run=tmp_path / "not-installed")
        with pytest.raises(DirectSystemdHostFailure, match="did not start"):
            DirectSystemdAgentProcessManager(missing_run).start(
                retry_attempt,
                retry_generation_id,
                retry_generation,
                retry_invocation,
            )

        manager.start(
            possibly_attempt,
            possibly_generation_id,
            possibly_generation,
            possibly_invocation,
        )
        _wait_until(possibly_ready.exists, "possibly-ran provider did not start")
        possibly_properties = _show(
            configuration, possibly_unit, "ControlGroup", "MainPID"
        )
        possibly_credential_directory = _credential_directory(
            possibly_properties["MainPID"]
        )

        manager.start(
            result_attempt,
            result_generation_id,
            result_generation,
            result_invocation,
        )
        _wait_until(result_ready.exists, "result provider did not start")
        result_release.touch()
        expected_result = manager.wait(result_generation)

        sudo = shutil.which("sudo")
        if sudo is None:
            pytest.fail("user-manager restart proof requires sudo")
        restarted = subprocess.run(
            (
                sudo,
                "systemctl",
                "restart",
                f"user@{os.getuid()}.service",
            ),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        assert restarted.returncode == 0, restarted.stderr
        _wait_until(
            lambda: (
                _systemctl(configuration.systemctl, "is-system-running").returncode == 0
            ),
            "restarted user manager did not become available",
            timeout=10,
        )

        replacement = DirectSystemdAgentProcessManager(configuration)
        replacement.start(
            retry_attempt,
            retry_generation_id,
            retry_generation,
            retry_invocation,
        )
        _wait_until(retry_ready.exists, "safe retry did not launch after restart")
        retry_release.touch()
        assert replacement.wait(retry_generation).standard_output == b"answer"
        with pytest.raises(DirectSystemdPossiblyRan):
            replacement.start(
                possibly_attempt,
                possibly_generation_id,
                possibly_generation,
                possibly_invocation,
            )
        assert (
            replacement.start(
                result_attempt,
                result_generation_id,
                result_generation,
                result_invocation,
            ).result
            == expected_result
        )

        assert len(retry_count.read_text().splitlines()) == 1
        assert len(possibly_count.read_text().splitlines()) == 1
        assert len(result_count.read_text().splitlines()) == 1
        _assert_no_residue(
            configuration,
            retry_unit,
            retry_generation,
            process_file=retry_count,
        )
        _assert_no_residue(
            configuration,
            possibly_unit,
            possibly_generation,
            control_group=possibly_properties["ControlGroup"],
            credential_directory=possibly_credential_directory,
            process_file=possibly_count,
        )
        _assert_no_residue(
            configuration,
            result_unit,
            result_generation,
            process_file=result_count,
        )
    finally:
        for release in releases:
            release.touch(exist_ok=True)
        for unit in units:
            _cleanup_unit(configuration, unit)
