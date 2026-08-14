from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from atelier2.adapters.agent_process_watchdog import (
    MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES,
)
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentExecutionResult,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.ports.agent_attempts import AgentAttemptFailed, AgentAttemptSucceeded
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    AgentProcessExited,
    AgentProcessInvocation,
    AgentProcessOutputLimitExceeded,
    AgentProcessRunner,
)
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.scenarios.agents import agent_attempt_execution


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
        invocation = AgentProcessInvocation(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; import sys,time; Path(sys.argv[1]).touch(); time.sleep(60)",
                str(ready_file),
            ),
            Path.cwd(),
        )
        supervisor.prepare(execution, invocation)
        store.claim(execution)
        result: list[object] = []
        waiter = threading.Thread(
            target=lambda: result.append(supervisor.launch_and_wait(execution))
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
        invocation = AgentProcessInvocation(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; import signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); Path(sys.argv[1]).touch(); time.sleep(60)",
                str(ready_file),
            ),
            Path.cwd(),
        )
        supervisor.prepare(execution, invocation)
        store.claim(execution)
        result: list[object] = []
        waiter = threading.Thread(
            target=lambda: result.append(supervisor.launch_and_wait(execution))
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
        provider = (
            "from pathlib import Path; import subprocess,sys,time; "
            "subprocess.Popen((sys.executable,'-c',"
            "'from pathlib import Path; import os,signal,sys,time; '"
            "+'signal.signal(signal.SIGTERM,signal.SIG_IGN); '"
            "+'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)',"
            "sys.argv[1]), start_new_session=True); "
            "Path(sys.argv[2]).touch(); time.sleep(60)"
        )
        invocation = AgentProcessInvocation(
            (
                sys.executable,
                "-c",
                provider,
                str(descendant_pid_file),
                str(ready_file),
            ),
            Path.cwd(),
        )
        supervisor.prepare(execution, invocation)
        store.claim(execution)
        result: list[object] = []
        waiter = threading.Thread(
            target=lambda: result.append(supervisor.launch_and_wait(execution))
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


def test_supervisor_drains_two_bounded_outputs_when_the_child_closes_input(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "process/io"))
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        provider = """
import os
import threading

os.close(0)
barrier = threading.Barrier(3)

def write_all(descriptor, value):
    barrier.wait()
    remaining = memoryview(value)
    while remaining:
        remaining = remaining[os.write(descriptor, remaining):]

threads = (
    threading.Thread(target=write_all, args=(1, b'o' * 49152)),
    threading.Thread(target=write_all, args=(2, b'e' * 49152)),
)
for thread in threads:
    thread.start()
barrier.wait()
for thread in threads:
    thread.join()
"""
        invocation = AgentProcessInvocation(
            (sys.executable, "-c", provider),
            Path.cwd(),
            standard_input=b"i" * MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
        )
        store.prepare(execution)
        supervisor.prepare(execution, invocation)
        store.claim(execution)

        process_outcome = supervisor.launch_and_wait(execution)

        assert isinstance(process_outcome, AgentProcessExited)
        assert process_outcome.return_code == 0
        assert process_outcome.standard_output == (b"o" * MAXIMUM_AGENT_OUTPUT_BYTES_V2)
        assert process_outcome.standard_error == (b"e" * MAXIMUM_AGENT_OUTPUT_BYTES_V2)
        terminal = store.complete_success(
            execution, AgentExecutionResult(process_outcome.standard_output)
        )
        assert isinstance(terminal, AgentAttemptSucceeded)
        supervisor.finalize(execution)
        _assert_no_attempt_witnesses(runtime, execution)
    finally:
        runtime.close()


def test_lost_launch_ack_retries_the_exact_frame_without_a_second_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/lost-launch-ack")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        counter = tmp_path / "launch-counter"
        invocation = AgentProcessInvocation(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; import os,sys; Path(sys.argv[1]).open('ab').write(b'x'); os.write(1,b'done')",
                str(counter),
            ),
            Path.cwd(),
        )
        store.prepare(execution)
        supervisor.prepare(execution, invocation)
        store.claim(execution)
        request_once = supervisor._request_once
        launch_ack_lost = False

        def lose_first_launch_ack(
            endpoint: Path,
            frame: bytes,
            *,
            timeout_seconds: float | None,
            maximum_response_bytes: int,
        ) -> dict[str, object]:
            nonlocal launch_ack_lost
            response = request_once(
                endpoint,
                frame,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=maximum_response_bytes,
            )
            if b'"operation":"LAUNCH"' in frame and not launch_ack_lost:
                launch_ack_lost = True
                raise ConnectionResetError("the first STARTED reply was lost")
            return response

        monkeypatch.setattr(supervisor, "_request_once", lose_first_launch_ack)

        process_outcome = supervisor.launch_and_wait(execution)

        assert launch_ack_lost
        assert isinstance(process_outcome, AgentProcessExited)
        assert process_outcome.standard_output == b"done"
        assert counter.read_bytes() == b"x"
        terminal = store.complete_success(
            execution, AgentExecutionResult(process_outcome.standard_output)
        )
        assert isinstance(terminal, AgentAttemptSucceeded)
        supervisor.finalize(execution)
        _assert_no_attempt_witnesses(runtime, execution)
    finally:
        runtime.close()


def test_watchdog_source_has_no_unbounded_collector_or_connection_threads() -> None:
    source = (
        Path(__file__).parents[2] / "src/atelier2/adapters/agent_process_watchdog.py"
    ).read_text(encoding="utf-8")

    assert ".communicate(" not in source
    assert "import threading" not in source
    assert "Thread(" not in source


def test_oversized_launch_is_rejected_before_any_process_witness_exists(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/oversized-launch")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        prepared = store.prepare(execution)
        invocation = AgentProcessInvocation(
            ("provider", "x" * MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES), Path.cwd()
        )

        with pytest.raises(ValueError, match="encoded agent launch exceeds"):
            runtime.agent_process_supervisor.prepare(execution, invocation)

        assert store.load(execution.attempt_id) == prepared
        assert prepared.state is AgentAttemptState.PREPARED
        assert prepared.process_phase is AgentAttemptProcessPhase.NONE
        control_root = runtime.settings.process_control_root()
        assert not (control_root / f"{execution.attempt_id.value}.lock").exists()
        assert not (control_root / f"{execution.attempt_id.value}.sock").exists()
        assert not (
            runtime.settings.process_cgroup_root()
            / f"atelier2-{execution.attempt_id.value}"
        ).exists()
    finally:
        runtime.close()


@pytest.mark.parametrize("overflow", ("stdout", "stderr", "both"))
def test_supervisor_discards_every_prefix_after_the_first_output_overflow(
    tmp_path: Path, overflow: str
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, f"process/overflow/{overflow}")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        descriptors = {"stdout": "(1,)", "stderr": "(2,)", "both": "(1, 2)"}[overflow]
        descriptor_count = 2 if overflow == "both" else 1
        provider = f"""
import os
import threading

os.close(0)
barrier = threading.Barrier({descriptor_count + 1})

def overflow_output(descriptor):
    barrier.wait()
    remaining = memoryview(b'x' * 49153)
    while remaining:
        remaining = remaining[os.write(descriptor, remaining):]

threads = tuple(
    threading.Thread(target=overflow_output, args=(descriptor,))
    for descriptor in {descriptors}
)
for thread in threads:
    thread.start()
barrier.wait()
for thread in threads:
    thread.join()
"""
        invocation = AgentProcessInvocation(
            (sys.executable, "-c", provider), Path.cwd()
        )
        store.prepare(execution)
        supervisor.prepare(execution, invocation)
        store.claim(execution)

        process_outcome = supervisor.launch_and_wait(execution)

        assert isinstance(process_outcome, AgentProcessOutputLimitExceeded)
        assert process_outcome.__dict__ == {}
        terminal = store.complete_known_failure(
            execution, AgentAttemptFailureCode.PROCESS_OUTPUT_LIMIT_EXCEEDED
        )
        assert isinstance(terminal, AgentAttemptFailed)
        supervisor.finalize(execution)
        _assert_no_attempt_witnesses(runtime, execution)
    finally:
        runtime.close()


def _wait_for_file(path: Path) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError("controlled process did not become ready")


def _assert_no_attempt_witnesses(
    runtime: DbosRuntime, execution: AgentAttemptExecution
) -> None:
    attempt_id = execution.attempt_id
    control_root = runtime.settings.process_control_root()
    assert not (control_root / f"{attempt_id.value}.lock").exists()
    assert not (control_root / f"{attempt_id.value}.sock").exists()
    assert not (
        runtime.settings.process_cgroup_root() / f"atelier2-{attempt_id.value}"
    ).exists()
    assert attempt_id not in runtime.agent_process_supervisor._owned


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
