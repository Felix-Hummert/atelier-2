from __future__ import annotations

import stat
import sys
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest

import atelier2.adapters.systemd_agent_processes as process_module
from atelier2.adapters.systemd_agent_collector import (
    DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME,
    encode_direct_systemd_launch_envelope,
)
from atelier2.adapters.systemd_agent_processes import (
    DirectSystemdAgentProcessConfiguration,
    DirectSystemdAgentProcessManager,
    DirectSystemdPossiblyRan,
    direct_systemd_unit_name,
)
from atelier2.adapters.systemd_generation_records import (
    DirectSystemdGenerationRecords,
    DirectSystemdIntent,
    DirectSystemdInvocationId,
    DirectSystemdRecoveryState,
    DirectSystemdResult,
    DirectSystemdResultOutcome,
    DirectSystemdStarted,
    encode_direct_systemd_intent,
)
from atelier2.adapters.systemd_timespans import (
    DirectSystemdHostFailure,
    DirectSystemdUnitConflict,
)
from atelier2.contracts.agent_attempts import AgentAttemptId, WatchdogGenerationId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentProcessCommand,
    AgentProcessInvocation,
)


def _configuration() -> DirectSystemdAgentProcessConfiguration:
    return DirectSystemdAgentProcessConfiguration(
        Path("/run"),
        Path("/ctl"),
        Path("/ana"),
        Path("/python"),
        Path("/runtime"),
        5,
        60,
        1.0,
        1.0,
        1.0,
        0.01,
    )


def _invocation(root: Path) -> AgentProcessInvocation:
    return AgentProcessInvocation(
        AgentProcessCommand(("provider",), standard_output_frame_bytes=17),
        AgentAttemptWorkspaceLease(AgentAttemptId.of(b"attempt"), root),
    )


def _seed_generation(
    generation: Path,
    invocation: AgentProcessInvocation,
    *,
    started: bool = True,
    result: bool = False,
) -> DirectSystemdGenerationRecords:
    generation.mkdir(mode=0o700)
    attempt_id = AgentAttemptId.of(b"attempt")
    generation_id = WatchdogGenerationId("generation")
    envelope = encode_direct_systemd_launch_envelope(invocation)
    records = DirectSystemdGenerationRecords(generation)
    intent = DirectSystemdIntent(
        attempt_id,
        generation_id,
        direct_systemd_unit_name(attempt_id, generation_id),
        Sha256Hash.of(envelope),
        invocation.command.standard_output_frame_bytes,
        invocation.lease.working_directory,
    )
    records.publish_intent(intent)
    if started:
        _publish_started(records)
    if result:
        _publish_result(records)
    return records


def _publish_started(
    records: DirectSystemdGenerationRecords,
    invocation_id: str = "0123456789abcdef0123456789abcdef",
) -> None:
    intent = records.read_intent()
    records.publish_started(
        DirectSystemdStarted(
            Sha256Hash.of(encode_direct_systemd_intent(intent)),
            DirectSystemdInvocationId(invocation_id),
        )
    )


def _publish_result(records: DirectSystemdGenerationRecords) -> DirectSystemdResult:
    started = records.inspect().started
    assert started is not None
    result = DirectSystemdResult(
        Sha256Hash.of(records.started_path.read_bytes()),
        started.invocation_id,
        DirectSystemdResultOutcome.COMPLETED,
        0,
        b"answer",
        b"",
        False,
        False,
    )
    records.publish_result(result)
    return result


class _FakeSystemd:
    def __init__(
        self,
        configuration: DirectSystemdAgentProcessConfiguration,
        generation: Path,
        invocation: AgentProcessInvocation,
    ) -> None:
        self.configuration = configuration
        self.generation = generation
        self.invocation = invocation
        self.live = False
        self.start_return_code = 0
        self.start_error: DirectSystemdHostFailure | None = None
        self.publish_started_on_acceptance = False
        self.vanish_after_acceptance = False
        self.credential_lines = 1
        self.credential_payload: bytes | None = None
        self.credential_return_code = 0
        self.cat_calls = 0
        self.publish_started_on_cat_number: int | None = None
        self.started_during_cat_invocation_id = "0123456789abcdef0123456789abcdef"
        self.state_return_code = 0
        self.state_payload: bytes | None = None
        self.property_overrides: dict[str, str] = {}
        self.stop_publishes_result = False
        self.stop_return_code = 0
        self.stop_keeps_live = False
        self.advance_stop_clock: Callable[[], None] = lambda: None
        self.collect_before_properties = False
        self.collect_after_cat = False
        self.result_during_cat = False
        self.advance_clock: Callable[[], None] = lambda: None
        self.advance_normalization_clock: Callable[[], None] = lambda: None
        self.accepted = Event()
        self.observed_live = Event()
        self.observed_without_started = Event()
        self.commands: list[tuple[str, ...]] = []
        self.calls: list[tuple[tuple[str, ...], float, int]] = []
        self.normalizations: list[tuple[str, int, float]] = []

    def answer(
        self,
        command: tuple[str, ...],
        timeout: float,
        maximum_output_bytes: int,
        environment: dict[str, str],
        *,
        before_spawn: Callable[[], None] = lambda: None,
        timeout_at_spawn: Callable[[], float] | None = None,
    ) -> tuple[int, bytes]:
        assert timeout > 0
        assert maximum_output_bytes >= 0
        assert environment == {
            "LC_ALL": "C",
            "SYSTEMD_COLORS": "0",
            "SYSTEMD_URLIFY": "0",
            "XDG_RUNTIME_DIR": "/runtime",
        }
        before_spawn()
        if timeout_at_spawn is not None:
            timeout = timeout_at_spawn()
        self.commands.append(command)
        self.calls.append((command, timeout, maximum_output_bytes))
        self.advance_clock()
        if command[0] == str(self.configuration.systemd_run):
            self._assert_durable_preparation()
            if self.start_error is not None:
                raise self.start_error
            if self.publish_started_on_acceptance:
                _publish_started(DirectSystemdGenerationRecords(self.generation))
            self.accepted.set()
            self.live = self.start_return_code == 0
            if self.vanish_after_acceptance:
                self.live = False
            return self.start_return_code, b""
        operation = command[2]
        if operation == "stop":
            if self.stop_publishes_result:
                _publish_result(DirectSystemdGenerationRecords(self.generation))
            self.advance_stop_clock()
            if not self.stop_keeps_live:
                self.live = False
            return self.stop_return_code, b""
        if operation == "cat":
            self.cat_calls += 1
            source = self.generation / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME
            output = self.credential_payload or (
                f"LoadCredential=atelier2-launch:{source}\n".encode()
                * self.credential_lines
            )
            assert len(output) <= maximum_output_bytes
            if self.result_during_cat:
                _publish_result(DirectSystemdGenerationRecords(self.generation))
            if self.publish_started_on_cat_number == self.cat_calls:
                _publish_started(
                    DirectSystemdGenerationRecords(self.generation),
                    self.started_during_cat_invocation_id,
                )
            if self.collect_after_cat:
                self.live = False
            return self.credential_return_code, output
        if "--value" in command:
            output = self.state_payload or (
                b"loaded\n" if self.live else b"not-found\n"
            )
            assert len(output) <= maximum_output_bytes
            return self.state_return_code, output
        if self.collect_before_properties:
            records = DirectSystemdGenerationRecords(self.generation)
            if records.inspect().result is None:
                _publish_result(records)
            self.live = False
            return 0, b"LoadState=not-found\n"
        output = self._properties(command)
        assert len(output) <= maximum_output_bytes
        self.observed_live.set()
        if not DirectSystemdGenerationRecords(self.generation).started_path.exists():
            self.observed_without_started.set()
        return 0, output

    def _assert_durable_preparation(self) -> None:
        records = DirectSystemdGenerationRecords(self.generation)
        intent = records.read_intent()
        source = self.generation / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME
        assert source.read_bytes() == encode_direct_systemd_launch_envelope(
            self.invocation
        )
        assert stat.S_IMODE(source.stat().st_mode) == 0o600
        assert intent.launch_envelope_hash == Sha256Hash.of(source.read_bytes())

    def _properties(self, command: tuple[str, ...]) -> bytes:
        intent = DirectSystemdGenerationRecords(self.generation).read_intent()
        values = dict.fromkeys(
            (
                argument.removeprefix("--property=")
                for argument in command
                if argument.startswith("--property=")
            ),
            "",
        )
        values.update(
            LoadState="loaded",
            Id=intent.unit_name,
            Type="exec",
            ExitType="main",
            KillMode="control-group",
            TimeoutStopUSec="5s",
            RuntimeMaxUSec="1min",
            SendSIGKILL="yes",
            CollectMode="inactive-or-failed",
            WorkingDirectory=str(self.invocation.lease.working_directory),
            InvocationID="0123456789abcdef0123456789abcdef",
            ExecStart="{ path=/python ; argv[]=/python -m atelier2.adapters.systemd_agent_collector ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=7 ; code=(null) ; status=0/0 }",
        )
        values.update(self.property_overrides)
        return "".join(f"{name}={value}\n" for name, value in values.items()).encode()


def _patch_systemd(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeSystemd
) -> DirectSystemdAgentProcessManager:
    monkeypatch.setattr(process_module, "_run_direct_systemd_command", fake.answer)

    def normalize(_path: Path, value: str, **keywords: float) -> int:
        expected = int(keywords["expected_microseconds"])
        fake.normalizations.append(
            (value, expected, float(keywords["command_timeout_seconds"]))
        )
        fake.advance_normalization_clock()
        if value == "changed":
            raise DirectSystemdUnitConflict("systemd duration changed")
        return expected

    monkeypatch.setattr(process_module, "normalize_direct_systemd_timespan", normalize)
    return DirectSystemdAgentProcessManager(fake.configuration)


def test_start_waits_for_durable_started_after_launch_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    manager = _patch_systemd(monkeypatch, fake)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            manager.start,
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )
        assert fake.accepted.wait(1)
        assert fake.observed_without_started.wait(1)
        assert not future.done()
        _publish_started(DirectSystemdGenerationRecords(generation))
        inspection = future.result(timeout=1)

    assert DirectSystemdGenerationRecords(generation).inspect() == inspection
    assert sum(command[0] == "/run" for command in fake.commands) == 1
    intent = DirectSystemdGenerationRecords(generation).read_intent()
    calls = fake.calls
    assert {limit for command, _timeout, limit in calls if command[0] == "/run"} == {
        process_module._MAXIMUM_QUIET_OUTPUT_BYTES
    }
    assert {
        limit
        for command, _timeout, limit in calls
        if command[2:3] == ("show",) and "--value" in command
    } == {process_module._MAXIMUM_UNIT_STATE_OUTPUT_BYTES}
    assert {
        limit
        for command, _timeout, limit in calls
        if command[2:3] == ("show",) and "--value" not in command
    } == {
        process_module._maximum_properties_output_bytes(
            fake.configuration, intent, generation
        )
    }
    assert {
        limit for command, _timeout, limit in calls if command[2:3] == ("cat",)
    } == {
        process_module._maximum_unit_file_output_bytes(
            fake.configuration, intent, generation
        )
    }
    assert [(value, expected) for value, expected, _timeout in fake.normalizations] == [
        ("5s", 5_000_000),
        ("60s", 60_000_000),
        ("5s", 5_000_000),
        ("1min", 60_000_000),
    ]


def test_start_waits_for_result_unit_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation, result=True)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    manager = _patch_systemd(monkeypatch, fake)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            manager.start,
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )
        assert fake.observed_live.wait(1)
        assert not future.done()
        fake.live = False
        inspection = future.result(timeout=1)

    assert inspection == records.inspect()
    assert inspection.result is not None


def test_adoption_ignores_source_after_durable_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation)
    source = generation / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME
    source.write_bytes(encode_direct_systemd_launch_envelope(invocation))
    source.chmod(0o600)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    manager = _patch_systemd(monkeypatch, fake)

    monkeypatch.setattr(
        process_module,
        "_read_source",
        lambda _path: pytest.fail("durable STARTED owns adoption, not Source"),
    )

    assert (
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )
        == records.inspect()
    )


def test_source_absence_after_started_never_recreates_launch_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation, started=False)
    source = generation / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME
    source.write_bytes(encode_direct_systemd_launch_envelope(invocation))
    source.chmod(0o600)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    manager = _patch_systemd(monkeypatch, fake)
    real_lexists = process_module.os.path.lexists
    raced = False

    def publish_started_before_source_absence(path: Path) -> bool:
        nonlocal raced
        if (
            path.name == DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME
            and str(path).startswith("/proc/self/fd/")
            and not raced
        ):
            raced = True
            _publish_started(records)
            source.unlink()
            return False
        return real_lexists(path)

    monkeypatch.setattr(
        process_module.os.path, "lexists", publish_started_before_source_absence
    )

    with pytest.raises(DirectSystemdPossiblyRan):
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )

    assert raced
    assert records.inspect().started is not None
    assert not source.exists()
    assert not any(command[0] == "/run" for command in fake.commands)


@pytest.mark.parametrize("started_payload", [b"", b"not-json"])
def test_visible_invalid_started_never_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    started_payload: bytes,
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation, started=False)
    source = generation / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME
    source.write_bytes(encode_direct_systemd_launch_envelope(invocation))
    source.chmod(0o600)
    records.started_path.write_bytes(started_payload)
    records.started_path.chmod(0o600)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    manager = _patch_systemd(monkeypatch, fake)

    with pytest.raises(DirectSystemdPossiblyRan):
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )
    with pytest.raises(DirectSystemdPossiblyRan):
        manager.wait(generation)

    assert not any(command[0] == "/run" for command in fake.commands)


def test_a_command_that_never_started_removes_only_the_unstarted_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.start_error = process_module._DirectSystemdCommandNotStarted("not spawned")
    manager = _patch_systemd(monkeypatch, fake)

    with pytest.raises(DirectSystemdHostFailure, match="not spawned"):
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )

    records = DirectSystemdGenerationRecords(generation)
    assert records.intent_path.exists()
    assert not records.started_path.exists()
    assert not (generation / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME).exists()

    fake.start_error = None
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            manager.start,
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )
        assert fake.accepted.wait(1)
        assert fake.observed_without_started.wait(1)
        _publish_started(records)
        assert future.result(timeout=1) == records.inspect()

    with pytest.raises(DirectSystemdUnitConflict, match="INTENT"):
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            AgentProcessInvocation(
                AgentProcessCommand(("changed",), standard_output_frame_bytes=17),
                AgentAttemptWorkspaceLease(AgentAttemptId.of(b"attempt"), tmp_path),
            ),
        )


def test_preparation_filesystem_failure_is_typed_before_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    manager = _patch_systemd(monkeypatch, fake)

    def refuse_publication(_path: Path, _payload: bytes) -> None:
        raise OSError("disk refused publication")

    monkeypatch.setattr(process_module, "_publish_source", refuse_publication)

    with pytest.raises(DirectSystemdHostFailure, match="could not be prepared"):
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )

    assert not fake.commands


def test_an_uncertain_refusal_retains_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.start_return_code = 1
    manager = _patch_systemd(monkeypatch, fake)

    with pytest.raises(DirectSystemdHostFailure, match="refused"):
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )

    assert (generation / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME).exists()


def test_accepted_unit_that_vanishes_before_started_is_typed_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.vanish_after_acceptance = True
    manager = _patch_systemd(monkeypatch, fake)

    with pytest.raises(DirectSystemdHostFailure, match="vanished before STARTED"):
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )

    assert sum(command[0] == "/run" for command in fake.commands) == 1


def test_stop_returns_a_result_published_during_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    fake.stop_publishes_result = True
    manager = _patch_systemd(monkeypatch, fake)

    inspection = manager.stop(generation)

    assert inspection == records.inspect()
    assert inspection.result is not None
    assert sum(command[2:3] == ("stop",) for command in fake.commands) == 1
    assert {
        limit for command, _timeout, limit in fake.calls if command[2:3] == ("stop",)
    } == {process_module._MAXIMUM_QUIET_OUTPUT_BYTES}


@pytest.mark.parametrize(
    ("return_code", "message"),
    [(1, "refused direct agent stop"), (0, "outlived its stop bound")],
)
def test_stop_failure_is_typed_after_one_attested_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    return_code: int,
    message: str,
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    _seed_generation(generation, invocation)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    fake.stop_return_code = return_code
    fake.stop_keeps_live = True
    now = [0.0]
    fake.advance_stop_clock = lambda: now.__setitem__(0, now[0] + 10)
    monkeypatch.setattr(process_module.time, "monotonic", lambda: now[0])
    manager = _patch_systemd(monkeypatch, fake)

    with pytest.raises(DirectSystemdHostFailure, match=message):
        manager.stop(generation)

    assert sum(command[2:3] == ("stop",) for command in fake.commands) == 1
    assert not any(command[0] == "/run" for command in fake.commands)


def test_result_published_during_identity_wins_before_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    fake.result_during_cat = True
    fake.collect_after_cat = True
    manager = _patch_systemd(monkeypatch, fake)

    inspection = manager.stop(generation)

    assert inspection == records.inspect()
    assert inspection.result is not None
    assert not any(command[2:3] == ("stop",) for command in fake.commands)


def test_result_published_at_stop_admission_prevents_the_stop_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    manager = _patch_systemd(monkeypatch, fake)

    def publish_before_spawn(
        command: tuple[str, ...],
        timeout: float,
        maximum_output_bytes: int,
        environment: dict[str, str],
        *,
        before_spawn: Callable[[], None] = lambda: None,
        timeout_at_spawn: Callable[[], float] | None = None,
    ) -> tuple[int, bytes]:
        if command[2:3] == ("stop",):
            _publish_result(records)
            fake.live = False
        return fake.answer(
            command,
            timeout,
            maximum_output_bytes,
            environment,
            before_spawn=before_spawn,
            timeout_at_spawn=timeout_at_spawn,
        )

    monkeypatch.setattr(
        process_module, "_run_direct_systemd_command", publish_before_spawn
    )

    inspection = manager.stop(generation)

    assert inspection == records.inspect()
    assert inspection.result is not None
    assert not any(command[2:3] == ("stop",) for command in fake.commands)


def test_duplicate_credential_identity_prevents_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    _seed_generation(generation, invocation)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    fake.credential_lines = 2
    manager = _patch_systemd(monkeypatch, fake)

    with pytest.raises(DirectSystemdUnitConflict, match="credential"):
        manager.stop(generation)

    assert not any(command[2:3] == ("stop",) for command in fake.commands)


@pytest.mark.parametrize(
    ("property_name", "changed_value"),
    [
        ("Id", "changed.service"),
        ("Type", "simple"),
        ("ExitType", "cgroup"),
        ("KillMode", "process"),
        ("SendSIGKILL", "no"),
        ("CollectMode", "inactive"),
        ("WorkingDirectory", "/changed"),
        ("TimeoutStopUSec", "changed"),
        ("RuntimeMaxUSec", "changed"),
        ("InvocationID", "fedcba9876543210fedcba9876543210"),
        ("ExecStart", "{ path=/changed ; argv[]=/changed }"),
    ],
)
def test_each_c4_identity_mutation_prevents_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    property_name: str,
    changed_value: str,
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    _seed_generation(generation, invocation)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    fake.property_overrides[property_name] = changed_value
    manager = _patch_systemd(monkeypatch, fake)

    with pytest.raises(DirectSystemdUnitConflict):
        manager.stop(generation)

    assert not any(command[2:3] == ("stop",) for command in fake.commands)


def test_invalid_credential_output_is_a_typed_conflict_without_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    _seed_generation(generation, invocation)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    fake.credential_payload = b"\xff"
    manager = _patch_systemd(monkeypatch, fake)

    with pytest.raises(DirectSystemdUnitConflict, match="malformed"):
        manager.stop(generation)

    assert not any(command[2:3] == ("stop",) for command in fake.commands)


@pytest.mark.parametrize("record_name", ["STARTED", "RESULT"])
def test_partial_evidence_at_stop_admission_never_starts_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_name: str,
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation, started=record_name == "RESULT")
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    manager = _patch_systemd(monkeypatch, fake)
    now = [0.0]
    fake.advance_clock = lambda: now.__setitem__(0, now[0] + 1)
    monkeypatch.setattr(process_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        process_module.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    evidence_path = (
        records.started_path if record_name == "STARTED" else records.result_path
    )

    def publish_partial_before_stop(
        command: tuple[str, ...],
        timeout: float,
        maximum_output_bytes: int,
        environment: dict[str, str],
        *,
        before_spawn: Callable[[], None] = lambda: None,
        timeout_at_spawn: Callable[[], float] | None = None,
    ) -> tuple[int, bytes]:
        if command[2:3] == ("stop",) and not evidence_path.exists():
            evidence_path.write_bytes(b"")
            evidence_path.chmod(0o600)
        return fake.answer(
            command,
            timeout,
            maximum_output_bytes,
            environment,
            before_spawn=before_spawn,
            timeout_at_spawn=timeout_at_spawn,
        )

    monkeypatch.setattr(
        process_module, "_run_direct_systemd_command", publish_partial_before_stop
    )

    with pytest.raises(DirectSystemdPossiblyRan, match="incomplete"):
        manager.stop(generation)

    assert evidence_path.read_bytes() == b""
    assert not any(command[2:3] == ("stop",) for command in fake.commands)


def test_result_without_started_is_a_loud_conflict_without_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation, started=False)
    records.result_path.write_bytes(b"")
    records.result_path.chmod(0o600)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    manager = _patch_systemd(monkeypatch, fake)

    with pytest.raises(DirectSystemdUnitConflict, match="evidence conflicts"):
        manager.stop(generation)

    assert not fake.commands


def test_started_published_after_identity_is_reattested_before_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    _seed_generation(generation, invocation, started=False)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    fake.publish_started_on_cat_number = 1
    fake.started_during_cat_invocation_id = "fedcba9876543210fedcba9876543210"
    manager = _patch_systemd(monkeypatch, fake)

    with pytest.raises(DirectSystemdUnitConflict, match="InvocationID"):
        manager.stop(generation)

    assert not any(command[2:3] == ("stop",) for command in fake.commands)


@pytest.mark.parametrize("matching", [False, True])
def test_started_at_final_stop_admission_is_rebound_before_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    matching: bool,
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation, started=False)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    fake.publish_started_on_cat_number = 2
    if not matching:
        fake.started_during_cat_invocation_id = "fedcba9876543210fedcba9876543210"
    manager = _patch_systemd(monkeypatch, fake)

    if matching:
        inspection = manager.stop(generation)
        assert inspection == records.inspect()
        assert fake.cat_calls == 3
        assert sum(command[2:3] == ("stop",) for command in fake.commands) == 1
    else:
        with pytest.raises(DirectSystemdUnitConflict, match="InvocationID"):
            manager.stop(generation)
        assert fake.cat_calls == 2
        assert not any(command[2:3] == ("stop",) for command in fake.commands)


def test_generation_substitution_writes_nothing_to_the_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    moved = tmp_path / "moved"
    invocation = _invocation(tmp_path)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    manager = _patch_systemd(monkeypatch, fake)
    publish_source = process_module._publish_source

    def substitute_before_publish(path: Path, payload: bytes) -> None:
        generation.rename(moved)
        generation.mkdir(mode=0o700)
        publish_source(path, payload)

    monkeypatch.setattr(process_module, "_publish_source", substitute_before_publish)

    with pytest.raises(DirectSystemdUnitConflict, match="inode"):
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )

    assert list(generation.iterdir()) == []
    assert not any(command[0] == "/run" for command in fake.commands)
    assert (moved / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME).exists()


def test_same_generation_lock_contention_fails_without_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    invocation = _invocation(tmp_path)
    configuration = replace(_configuration(), preparation_timeout_seconds=0.02)
    fake = _FakeSystemd(configuration, generation, invocation)
    manager = _patch_systemd(monkeypatch, fake)

    with (
        process_module._generation_lock(
            generation, process_module.time.monotonic() + 1, 0.01
        ),
        pytest.raises(DirectSystemdHostFailure, match="lock"),
    ):
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )

    assert not fake.commands


def test_collection_between_state_and_identity_returns_the_durable_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    fake.collect_before_properties = True
    manager = _patch_systemd(monkeypatch, fake)

    inspection = manager.start(
        AgentAttemptId.of(b"attempt"),
        WatchdogGenerationId("generation"),
        generation,
        invocation,
    )

    assert inspection == records.inspect()
    assert inspection.result is not None
    assert not any(command[0] == "/run" for command in fake.commands)


def test_wait_returns_the_exact_raw_result_only_after_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation, result=True)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    manager = _patch_systemd(monkeypatch, fake)

    assert manager.wait(generation) == records.inspect().result


def test_wait_reobserves_a_cat_failure_until_durable_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation, result=True)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    fake.credential_return_code = 1
    fake.collect_after_cat = True
    manager = _patch_systemd(monkeypatch, fake)

    assert manager.wait(generation) == records.inspect().result


def test_c4_attested_live_unit_stops_before_started_without_provider_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation, started=False)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    manager = _patch_systemd(monkeypatch, fake)

    inspection = manager.stop(generation)

    assert inspection.state is DirectSystemdRecoveryState.SAFE_TO_RETRY
    assert inspection == records.inspect()
    assert sum(command[2:3] == ("stop",) for command in fake.commands) == 1
    assert not any(command[0] == "/run" for command in fake.commands)


def test_host_failure_before_identity_never_stops_the_unit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    _seed_generation(generation, invocation)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.live = True
    fake.state_return_code = 1
    now = [0.0]
    fake.advance_clock = lambda: now.__setitem__(0, now[0] + 4)
    monkeypatch.setattr(process_module.time, "monotonic", lambda: now[0])
    manager = _patch_systemd(monkeypatch, fake)

    with pytest.raises(DirectSystemdHostFailure, match="state observation"):
        manager.stop(generation)

    assert not any(command[2:3] == ("stop",) for command in fake.commands)


def test_every_command_receives_only_the_shared_deadline_remainder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    _seed_generation(generation, invocation)
    configuration = replace(
        _configuration(), command_timeout_seconds=30, timeout_stop_seconds=1
    )
    fake = _FakeSystemd(configuration, generation, invocation)
    fake.live = True
    now = [0.0]
    fake.advance_clock = lambda: now.__setitem__(0, now[0] + 1)
    fake.advance_normalization_clock = lambda: now.__setitem__(0, now[0] + 4)
    monkeypatch.setattr(process_module.time, "monotonic", lambda: now[0])
    manager = _patch_systemd(monkeypatch, fake)

    manager.stop(generation)

    assert [entry[2] for entry in fake.normalizations] == pytest.approx(
        [30, 28, 22, 18]
    )
    assert [call[1] for call in fake.calls] == pytest.approx(
        [24, 23, 14, 13, 12, 11, 10, 9]
    )


def test_start_budget_composes_normalization_preparation_and_reobservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    fake.publish_started_on_acceptance = True
    now = [0.0]
    fake.advance_clock = lambda: now.__setitem__(0, now[0] + 0.2)
    fake.advance_normalization_clock = lambda: now.__setitem__(0, now[0] + 0.4)
    monkeypatch.setattr(process_module.time, "monotonic", lambda: now[0])
    real_lock = process_module._generation_lock

    @contextmanager
    def delayed_lock(
        path: Path, deadline: float, poll: float
    ) -> Iterator[tuple[Path, int]]:
        now[0] += 0.3
        with real_lock(path, deadline, poll) as held:
            yield held

    monkeypatch.setattr(process_module, "_generation_lock", delayed_lock)
    manager = _patch_systemd(monkeypatch, fake)

    inspection = manager.start(
        AgentAttemptId.of(b"attempt"),
        WatchdogGenerationId("generation"),
        generation,
        invocation,
    )

    assert inspection.started is not None
    assert now[0] < 3


def test_start_lock_contention_cannot_outlive_the_shared_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    generation.mkdir(mode=0o700)
    invocation = _invocation(tmp_path)
    configuration = replace(
        _configuration(),
        command_timeout_seconds=0.1,
        preparation_timeout_seconds=0.1,
        reobservation_timeout_seconds=0.1,
        observation_poll_seconds=0.01,
    )
    fake = _FakeSystemd(configuration, generation, invocation)
    now = [0.0]
    fake.advance_normalization_clock = lambda: now.__setitem__(0, now[0] + 0.11)
    monkeypatch.setattr(process_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        process_module.time,
        "sleep",
        lambda seconds: now.__setitem__(0, now[0] + seconds),
    )
    manager = _patch_systemd(monkeypatch, fake)

    with (
        process_module._generation_lock(generation, 1, 0.01),
        pytest.raises(DirectSystemdHostFailure, match="lock"),
    ):
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )

    assert now[0] == pytest.approx(0.3)
    assert not fake.commands


def test_named_command_output_ceilings_accept_exactly_their_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    invocation = _invocation(tmp_path)
    records = _seed_generation(generation, invocation)
    intent = records.read_intent()
    configuration = _configuration()
    limits = (
        process_module._MAXIMUM_QUIET_OUTPUT_BYTES,
        process_module._MAXIMUM_UNIT_STATE_OUTPUT_BYTES,
        process_module._MAXIMUM_VERSION_OUTPUT_BYTES,
        process_module._maximum_properties_output_bytes(
            configuration, intent, generation
        ),
        process_module._maximum_unit_file_output_bytes(
            configuration, intent, generation
        ),
    )
    assert limits[:3] == (0, len(b"not-found\n"), 4096)
    assert len(set(limits)) == len(limits)
    monkeypatch.setenv("TOP_SECRET", "must-not-leak")
    environment = {
        "LC_ALL": "C",
        "SYSTEMD_COLORS": "0",
        "SYSTEMD_URLIFY": "0",
        "XDG_RUNTIME_DIR": "/runtime",
    }
    program = (
        "import os,sys;"
        f"assert os.environ=={environment!r};"
        "os.write(1,b'x'*int(sys.argv[1]))"
    )

    for limit in limits:
        command = (sys.executable, "-c", program, str(limit))
        assert process_module._run_direct_systemd_command(
            command, 1, limit, environment
        ) == (0, b"x" * limit)
        overflowing = (sys.executable, "-c", program, str(limit + 1))
        with pytest.raises(DirectSystemdHostFailure):
            process_module._run_direct_systemd_command(
                overflowing, 1, limit, environment
            )


def test_command_runner_recomputes_timeout_after_admission_attestation() -> None:
    environment = {
        "LC_ALL": "C",
        "SYSTEMD_COLORS": "0",
        "SYSTEMD_URLIFY": "0",
        "XDG_RUNTIME_DIR": "/runtime",
    }
    command = (sys.executable, "-c", "import time;time.sleep(.2)")
    started_at = process_module.time.monotonic()

    with pytest.raises(DirectSystemdHostFailure):
        process_module._run_direct_systemd_command(
            command,
            1,
            0,
            environment,
            timeout_at_spawn=lambda: 0.03,
        )

    assert process_module.time.monotonic() - started_at < 0.5


def test_substitution_at_the_spawn_boundary_never_reaches_systemd_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    generation = tmp_path / "generation"
    moved = tmp_path / "moved"
    invocation = _invocation(tmp_path)
    fake = _FakeSystemd(_configuration(), generation, invocation)
    manager = _patch_systemd(monkeypatch, fake)

    def substitute(
        command: tuple[str, ...],
        timeout: float,
        maximum_output_bytes: int,
        environment: dict[str, str],
        *,
        before_spawn: Callable[[], None] = lambda: None,
        timeout_at_spawn: Callable[[], float] | None = None,
    ) -> tuple[int, bytes]:
        if command[0] == "/run":
            generation.rename(moved)
            generation.mkdir(mode=0o700)
        return fake.answer(
            command,
            timeout,
            maximum_output_bytes,
            environment,
            before_spawn=before_spawn,
            timeout_at_spawn=timeout_at_spawn,
        )

    monkeypatch.setattr(process_module, "_run_direct_systemd_command", substitute)

    with pytest.raises(DirectSystemdUnitConflict, match="inode"):
        manager.start(
            AgentAttemptId.of(b"attempt"),
            WatchdogGenerationId("generation"),
            generation,
            invocation,
        )

    assert list(generation.iterdir()) == []
    assert not any(command[0] == "/run" for command in fake.commands)
