from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from atelier2.adapters import agent_processes as process_module
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.ports.agent_attempts import AgentAttemptSucceeded
from atelier2.ports.agent_executions import (
    AgentProcessCompletion,
    AgentProcessOwnerNotLocal,
)
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.scenarios.agents import (
    NOTHING_IS_PERMITTED,
    SCENARIO_PROVIDER_FRAME_BYTES,
    agent_attempt_execution,
    process_invocation,
)


def test_concurrent_local_waiters_share_one_process_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/shared-completion")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        counter = tmp_path / "provider-count"
        invocation = process_invocation(
            execution.attempt_id,
            (
                sys.executable,
                "-c",
                "from pathlib import Path; import os,sys,time; Path(sys.argv[1]).open('ab').write(b'x'); time.sleep(.2); os.write(1,b'\"done\"')",
                str(counter),
            ),
            Path.cwd(),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        start = threading.Barrier(3)
        outcomes: list[AgentProcessCompletion] = []
        failures: list[BaseException] = []
        decode_completion = process_module._completion_from_response
        decode_count = 0

        def count_decode(
            response: dict[str, object],
            standard_output_frame_bytes: int,
            conversation: process_module._ConversationEvidence = (
                process_module._NOTHING_WAS_SAID
            ),
        ) -> AgentProcessCompletion:
            nonlocal decode_count
            decode_count += 1
            return decode_completion(
                response, standard_output_frame_bytes, conversation
            )

        monkeypatch.setattr(process_module, "_completion_from_response", count_decode)

        def invoke() -> None:
            start.wait()
            try:
                outcomes.append(
                    supervisor.launch_and_wait(
                        execution, invocation, NOTHING_IS_PERMITTED
                    )
                )
            except (AgentProcessOwnerNotLocal, OSError, RuntimeError) as error:
                failures.append(error)

        waiters = tuple(threading.Thread(target=invoke) for _ in range(2))
        for waiter in waiters:
            waiter.start()
        start.wait()
        for waiter in waiters:
            waiter.join(timeout=5)

        assert failures == []
        assert len(outcomes) == 2
        assert decode_count == 1
        assert outcomes[0] is outcomes[1]
        assert outcomes[0].standard_output == b'"done"'
        assert counter.read_bytes() == b"x"
        terminal = store.complete_success(
            execution, AgentExecutionResult(outcomes[0].standard_output)
        )
        assert isinstance(terminal, AgentAttemptSucceeded)
        supervisor.finalize(execution)
    finally:
        runtime.close()


def test_changed_launch_refuses_without_stopping_the_valid_process(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/changed-launch")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        counter = tmp_path / "provider-count"
        ready = tmp_path / "provider-ready"
        finish = tmp_path / "provider-finish"
        invocation = process_invocation(
            execution.attempt_id,
            (
                sys.executable,
                "-c",
                "from pathlib import Path; import os,sys,time; counter=Path(sys.argv[1]); ready=Path(sys.argv[2]); finish=Path(sys.argv[3]); counter.open('ab').write(b'x'); ready.touch();\nwhile not finish.exists(): time.sleep(.01)\nos.write(1,b'\"done\"')",
                str(counter),
                str(ready),
                str(finish),
            ),
            Path.cwd(),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        outcomes: list[AgentProcessCompletion] = []
        failures: list[Exception] = []

        def invoke() -> None:
            try:
                outcomes.append(
                    supervisor.launch_and_wait(
                        execution, invocation, NOTHING_IS_PERMITTED
                    )
                )
            except (AgentProcessOwnerNotLocal, OSError, RuntimeError) as error:
                failures.append(error)

        waiter = threading.Thread(target=invoke)
        waiter.start()
        _wait_for_observed_process(store, execution.attempt_id)
        _wait_for_file(ready)

        with pytest.raises(RuntimeError, match="invocation changed"):
            supervisor.launch_and_wait(
                execution,
                process_invocation(
                    execution.attempt_id,
                    (sys.executable, "-c", "pass"),
                    Path.cwd(),
                    standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
                ),
                NOTHING_IS_PERMITTED,
            )

        assert waiter.is_alive()
        assert counter.read_bytes() == b"x"
        finish.touch()
        waiter.join(timeout=5)
        assert failures == []
        assert len(outcomes) == 1
        terminal = store.complete_success(
            execution, AgentExecutionResult(outcomes[0].standard_output)
        )
        assert isinstance(terminal, AgentAttemptSucceeded)
        supervisor.finalize(execution)
    finally:
        runtime.close()


def test_close_prevents_a_paused_prepare_from_publishing_a_watchdog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/prepare-close")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        store.prepare(execution)
        ready = supervisor._wait_ready
        ready_proven = threading.Event()
        continue_prepare = threading.Event()
        unpublished_watchdogs: list[Any] = []

        def pause_after_ready(owned: Any) -> None:
            ready(owned)
            unpublished_watchdogs.append(owned)
            ready_proven.set()
            assert continue_prepare.wait(timeout=5)

        monkeypatch.setattr(supervisor, "_wait_ready", pause_after_ready)
        failures: list[BaseException] = []

        def prepare() -> None:
            try:
                supervisor.prepare(execution)
            except (AgentProcessOwnerNotLocal, OSError, RuntimeError) as error:
                failures.append(error)

        preparing = threading.Thread(target=prepare)
        preparing.start()
        assert ready_proven.wait(timeout=5)

        supervisor.close()
        continue_prepare.set()
        preparing.join(timeout=5)

        assert not preparing.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], RuntimeError)
        assert "closed" in str(failures[0])
        assert supervisor._owned == {}
        assert len(unpublished_watchdogs) == 1
        unpublished = unpublished_watchdogs[0]
        assert unpublished.process.poll() is not None
        assert not unpublished.endpoint.exists()
        assert not unpublished.cgroup.exists()
    finally:
        runtime.close()


def test_owner_eof_before_launch_preserves_witness_until_recovery(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/eof-before-launch")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        owned = supervisor._owned[execution.attempt_id]
        assert owned is not None

        supervisor.close()

        assert owned.process.poll() is not None
        assert supervisor._owned == {}
        assert owned.endpoint.is_socket()
        assert owned.cgroup.is_dir()
        durable = store.load(execution.attempt_id)
        assert durable.state is AgentAttemptState.LAUNCH_ARMED
        assert durable.process_phase is AgentAttemptProcessPhase.LAUNCH_AUTHORIZED

        command = CancelAgentAttemptRequest(
            durable.run_id,
            durable.attempt_id,
            "recover-eof-before-launch",
            durable.state_version,
            AgentAttemptReplacement.NONE,
        )
        store.request_cancellation(command)
        disposition, owner, generation = supervisor.recover(
            store.load(execution.attempt_id)
        )
        assert (
            disposition
            is AgentAttemptCancellationDisposition.OWNER_LOST_AFTER_PARENT_DEATH
        )
        terminal = store.attest_cancellation_cleanup(
            command, disposition, owner, generation
        )
        supervisor.release(terminal.attempt)
        assert not owned.endpoint.exists()
        assert not owned.cgroup.exists()
    finally:
        runtime.close()


def test_failed_watchdog_spawn_releases_prepared_process_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cgroup_token = hashlib.sha256(os.fsencode(tmp_path.resolve())).hexdigest()[:16]
    test_cgroup_root = (
        process_module.delegated_cgroup_root() / f"atelier2-test-{cgroup_token}"
    )
    test_cgroup_root.mkdir()
    runtime = attempt_runtime(tmp_path, agent_process_cgroup_root=test_cgroup_root)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/spawn-failure")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        store.prepare(execution)
        opened_descriptors: list[int] = []
        open_pipe = process_module.os.pipe

        def record_pipe() -> tuple[int, int]:
            descriptors = open_pipe()
            opened_descriptors.extend(descriptors)
            return descriptors

        def fail_spawn(*_arguments: object, **_keywords: object) -> None:
            raise OSError("watchdog spawn failed")

        monkeypatch.setattr(process_module.os, "pipe", record_pipe)
        monkeypatch.setattr(process_module.subprocess, "Popen", fail_spawn)

        with pytest.raises(OSError, match="watchdog spawn failed"):
            supervisor.prepare(execution)

        assert len(opened_descriptors) == 2
        for descriptor in opened_descriptors:
            with pytest.raises(OSError):
                os.fstat(descriptor)
        cgroup_prefix = "atelier2-" + execution.attempt_id.value[:16] + "-"
        assert not any(
            path.name.startswith(cgroup_prefix)
            for path in runtime.settings.process_cgroup_root().iterdir()
        )
        assert tuple(runtime.settings.process_control_root().glob("*.sock")) == ()
    finally:
        try:
            runtime.close()
        finally:
            for cgroup in tuple(test_cgroup_root.iterdir()):
                if cgroup.is_dir():
                    cgroup.rmdir()
            test_cgroup_root.rmdir()


def _wait_for_observed_process(
    store: DbosAgentAttemptStore, attempt_id: AgentAttemptId
) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        attempt = store.load(attempt_id)
        if attempt.process_phase is AgentAttemptProcessPhase.PROCESS_OBSERVED:
            return
        time.sleep(0.01)
    raise AssertionError("controlled process was never durably observed")


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError("controlled process did not become ready")
