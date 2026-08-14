from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptReplacement,
    CancelAgentAttemptRequest,
)
from atelier2.ports.agent_executions import AgentProcessInvocation, AgentProcessRunner
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.scenarios.agents import (
    SCENARIO_PROVIDER_FRAME_BYTES,
    agent_attempt_execution,
)


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


def test_supervisor_reaps_a_process_that_exits_on_term(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        ready_file = tmp_path / "term-ready"
        execution = agent_attempt_execution(attempt_request(runtime, "process/term"))
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        result: list[object] = []
        waiter = threading.Thread(
            target=lambda: result.append(
                supervisor.launch_and_wait(
                    execution,
                    AgentProcessInvocation(
                        (
                            sys.executable,
                            "-c",
                            "from pathlib import Path; import sys,time; Path(sys.argv[1]).touch(); time.sleep(60)",
                            str(ready_file),
                        ),
                        Path.cwd(),
                        standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
                    ),
                )
            )
        )
        waiter.start()
        _wait_for_observed_process(store, execution.attempt_id)
        _wait_for_file(ready_file)

        disposition = _cancel_and_release(store, supervisor, execution.attempt_id)
        waiter.join(timeout=5)

        assert disposition is AgentAttemptCancellationDisposition.REAPED_AFTER_TERM
        assert not waiter.is_alive()
        assert len(result) == 1
    finally:
        runtime.close()


def test_supervisor_kills_and_reaps_a_process_that_ignores_term(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        ready_file = tmp_path / "kill-ready"
        execution = agent_attempt_execution(attempt_request(runtime, "process/kill"))
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        result: list[object] = []
        waiter = threading.Thread(
            target=lambda: result.append(
                supervisor.launch_and_wait(
                    execution,
                    AgentProcessInvocation(
                        (
                            sys.executable,
                            "-c",
                            "from pathlib import Path; import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(sys.argv[1]).touch(); time.sleep(60)",
                            str(ready_file),
                        ),
                        Path.cwd(),
                        standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
                    ),
                )
            )
        )
        waiter.start()
        _wait_for_observed_process(store, execution.attempt_id)
        _wait_for_file(ready_file)

        disposition = _cancel_and_release(store, supervisor, execution.attempt_id)
        waiter.join(timeout=5)

        assert disposition is AgentAttemptCancellationDisposition.REAPED_AFTER_KILL
        assert not waiter.is_alive()
        assert len(result) == 1
    finally:
        runtime.close()


def test_supervisor_kills_session_escaped_descendants_in_the_attempt_cgroup(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    descendant_pid_file = tmp_path / "descendant-pid"
    try:
        ready_file = tmp_path / "descendant-ready"
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/descendant")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        result: list[object] = []
        provider = (
            "from pathlib import Path; import subprocess,sys,time; "
            "subprocess.Popen((sys.executable,'-c',"
            "'from pathlib import Path; import os,signal,sys,time; '"
            "+'signal.signal(signal.SIGTERM,signal.SIG_IGN); '"
            "+'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)',"
            "sys.argv[1]), start_new_session=True); "
            "Path(sys.argv[2]).touch(); time.sleep(60)"
        )
        waiter = threading.Thread(
            target=lambda: result.append(
                supervisor.launch_and_wait(
                    execution,
                    AgentProcessInvocation(
                        (
                            sys.executable,
                            "-c",
                            provider,
                            str(descendant_pid_file),
                            str(ready_file),
                        ),
                        Path.cwd(),
                        standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
                    ),
                )
            )
        )
        waiter.start()
        _wait_for_observed_process(store, execution.attempt_id)
        _wait_for_file(ready_file)
        _wait_for_file(descendant_pid_file)
        descendant_pid = int(descendant_pid_file.read_text(encoding="ascii"))

        disposition = _cancel_and_release(store, supervisor, execution.attempt_id)
        waiter.join(timeout=5)

        assert disposition is AgentAttemptCancellationDisposition.REAPED_AFTER_KILL
        assert not Path(f"/proc/{descendant_pid}").exists()
        assert not waiter.is_alive()
        assert len(result) == 1
    finally:
        if descendant_pid_file.exists():
            descendant_pid = int(descendant_pid_file.read_text(encoding="ascii"))
            try:
                os.kill(descendant_pid, 9)
            except ProcessLookupError:
                pass
        runtime.close()


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError("controlled process did not become ready")


def _cancel_and_release(
    store: DbosAgentAttemptStore,
    supervisor: AgentProcessRunner,
    attempt_id: AgentAttemptId,
) -> AgentAttemptCancellationDisposition:
    attempt = store.load(attempt_id)
    command = CancelAgentAttemptRequest(
        attempt.run_id,
        attempt.attempt_id,
        "cancel-process",
        attempt.state_version,
        AgentAttemptReplacement.NONE,
    )
    store.request_cancellation(command)
    disposition, owner, generation = supervisor.cancel(store.load(attempt_id))
    terminal = store.attest_cancellation_cleanup(
        command, disposition, owner, generation
    )
    supervisor.release(terminal.attempt)
    return disposition
