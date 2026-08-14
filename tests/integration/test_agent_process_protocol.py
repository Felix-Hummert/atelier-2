from __future__ import annotations

import json
import os
import select
import selectors
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from atelier2.adapters import agent_process_watchdog as watchdog_module
from atelier2.adapters import agent_processes as process_module
from atelier2.adapters.agent_process_watchdog import (
    MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES,
    MAXIMUM_AGENT_WAIT_RESPONSE_BYTES_V2,
    Watchdog,
    encode_control_frame,
)
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptReplacement,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentExecutionResult,
)
from atelier2.ports.agent_attempts import AgentAttemptSucceeded
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    AgentProcessInvocation,
    AgentProcessOwnerNotLocal,
)
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.scenarios.agents import agent_attempt_execution


@pytest.mark.parametrize("termination_owner", (None, "CANCEL"))
def test_terminal_publication_is_one_shot_and_reuses_cached_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    termination_owner: str | None,
) -> None:
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    owner_pipe, owner_writer = os.pipe()
    process = subprocess.Popen((sys.executable, "-c", "pass"))
    process.wait(timeout=5)
    watchdog = Watchdog(tmp_path / "control.sock", cgroup, owner_pipe, 0.1)
    watchdog._process = process
    watchdog._standard_output.extend(b"output")
    watchdog._standard_error.extend(b"error")
    watchdog._termination_owner = termination_owner
    watchdog._termination_disposition = "EXITED_BEFORE_SIGNAL"
    try:
        watchdog._advance_process(1.0)
        cached_wait = watchdog._wait_response
        cached_cancel = watchdog._cancel_response
        assert cached_wait is not None
        if termination_owner == "CANCEL":
            assert cached_cancel is not None

        def reject_reconstruction(_now: float) -> None:
            raise AssertionError("terminal response was reconstructed")

        monkeypatch.setattr(
            watchdog, "_publish_process_completion", reject_reconstruction
        )
        monkeypatch.setattr(watchdog, "_publish_cancel", reject_reconstruction)

        watchdog._advance_process(2.0)

        assert watchdog._wait_response is cached_wait
        assert watchdog._cancel_response is cached_cancel
    finally:
        watchdog._selector.close()
        os.close(owner_pipe)
        os.close(owner_writer)


def test_recovery_handoff_publication_and_retries_reuse_cached_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(tmp_path / "control.sock", tmp_path / "cgroup", owner_pipe, 0.1)
    peers: list[socket.socket] = []
    try:
        watchdog._publish_recovery_handoff(1.0)
        cached_wait = watchdog._wait_response
        cached_cancel = watchdog._cancel_response
        assert cached_wait is not None
        assert cached_cancel is cached_wait

        def reject_reconstruction(_payload: dict[str, object]) -> bytes:
            raise AssertionError("recovery handoff was reconstructed")

        monkeypatch.setattr(
            watchdog_module, "encode_control_frame", reject_reconstruction
        )
        watchdog._publish_recovery_handoff(2.0)

        for operation, handler, cached in (
            ("WAIT", watchdog._handle_wait, cached_wait),
            ("CANCEL", watchdog._handle_cancel, cached_cancel),
        ):
            server, peer = socket.socketpair()
            peers.append(peer)
            server.setblocking(False)
            connection = watchdog_module._Connection(
                server, 2.0, slot=operation, operation=operation
            )
            watchdog._connections[server.fileno()] = connection
            watchdog._slots[operation] = server.fileno()
            watchdog._selector.register(server, selectors.EVENT_READ, connection)

            handler(connection, 2.0)

            assert connection.output_bytes is cached
            watchdog._close_connection(connection)
    finally:
        for peer in peers:
            peer.close()
        watchdog._selector.close()
        os.close(owner_pipe)
        os.close(owner_writer)


def test_watchdog_fails_loud_if_wait_response_exceeds_protocol_bound(
    tmp_path: Path,
) -> None:
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(tmp_path / "control.sock", tmp_path / "cgroup", owner_pipe, 0.1)
    try:
        with pytest.raises(RuntimeError, match="wait response exceeds"):
            watchdog._publish_wait(
                {"detail": "x" * MAXIMUM_AGENT_WAIT_RESPONSE_BYTES_V2}, 1.0
            )

        assert watchdog._wait_response is None
    finally:
        watchdog._selector.close()
        os.close(owner_pipe)
        os.close(owner_writer)


def test_supervisor_drains_exactly_bounded_outputs_after_closed_input(
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
        supervisor.prepare(execution)
        store.claim(execution)

        completion = supervisor.launch_and_wait(execution, invocation)

        assert completion.standard_output == b"o" * MAXIMUM_AGENT_OUTPUT_BYTES_V2
        assert (
            completion.standard_error
            == b"e" * MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
        )
        terminal = store.complete_success(
            execution, AgentExecutionResult(completion.standard_output)
        )
        assert isinstance(terminal, AgentAttemptSucceeded)
        supervisor.finalize(execution)
    finally:
        runtime.close()


def test_lost_control_replies_replay_without_launching_twice(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/lost-launch-reply")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        counter = tmp_path / "provider-count"
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
        supervisor.prepare(execution)
        store.claim(execution)
        owned = supervisor._owned[execution.attempt_id]
        assert owned is not None
        lost_launch = _send_without_reading(
            owned.endpoint,
            encode_control_frame(process_module._launch_request(invocation)),
        )
        lost_wait = _send_without_reading(
            owned.endpoint, encode_control_frame({"operation": "WAIT"})
        )

        completion = supervisor.launch_and_wait(execution, invocation)

        assert completion.standard_output == b"done"
        assert counter.read_bytes() == b"x"
        terminal = store.complete_success(
            execution, AgentExecutionResult(completion.standard_output)
        )
        assert isinstance(terminal, AgentAttemptSucceeded)
        lost_finalize = _send_without_reading(
            owned.endpoint, encode_control_frame({"operation": "FINALIZE"})
        )
        supervisor.finalize(execution)
        lost_launch.close()
        lost_wait.close()
        lost_finalize.close()
    finally:
        runtime.close()


def test_control_slots_bound_bad_peers_while_cancel_progresses_beside_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "process/control-slots")
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

        with _connect_control(owned.endpoint) as incomplete:
            incomplete.sendall(b'{"operation"')
            assert _receive_control(incomplete) == {"type": "CONTROL_FRAME_TIMEOUT"}
        with _send_without_reading(
            owned.endpoint, b'{"operation": "WAIT"}'
        ) as noncanonical:
            assert _receive_control(noncanonical) == {"type": "MALFORMED"}
        waits = tuple(
            _send_without_reading(
                owned.endpoint, encode_control_frame({"operation": "WAIT"})
            )
            for _index in range(2)
        )
        ready_waits, _writable, _failed = select.select(waits, (), (), 2)
        assert len(ready_waits) == 1
        competing_wait = ready_waits[0]
        waiting = waits[1] if competing_wait is waits[0] else waits[0]
        with competing_wait:
            assert _receive_control(competing_wait) == {"type": "BUSY"}

        counter = tmp_path / "provider-count"
        lost_cancel = _send_without_reading(
            owned.endpoint, encode_control_frame({"operation": "CANCEL"})
        )
        with waiting:
            assert _receive_control(waiting) == {"type": "STOPPED"}
        with pytest.raises(AgentProcessOwnerNotLocal):
            supervisor.launch_and_wait(
                execution,
                AgentProcessInvocation(
                    (
                        sys.executable,
                        "-c",
                        "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                        str(counter),
                    ),
                    Path.cwd(),
                ),
            )
        assert not counter.exists()

        attempt = store.load(execution.attempt_id)
        command = CancelAgentAttemptRequest(
            attempt.run_id,
            attempt.attempt_id,
            "cancel-beside-wait",
            attempt.state_version,
            AgentAttemptReplacement.NONE,
        )
        store.request_cancellation(command)
        blocked = _connect_control(owned.endpoint)
        cancel_frame = encode_control_frame({"operation": "CANCEL"})
        blocked.sendall(cancel_frame[:-1])
        request_once = supervisor._request
        cancel_requests = 0

        def count_cancel_requests(
            endpoint: Path,
            request: dict[str, object],
            *,
            timeout_seconds: float | None = 30,
            maximum_response_bytes: int,
        ) -> dict[str, object]:
            nonlocal cancel_requests
            if request.get("operation") == "CANCEL":
                cancel_requests += 1
            return request_once(
                endpoint,
                request,
                timeout_seconds=timeout_seconds,
                maximum_response_bytes=maximum_response_bytes,
            )

        monkeypatch.setattr(supervisor, "_request", count_cancel_requests)
        disposition, owner, generation = supervisor.cancel(
            store.load(execution.attempt_id)
        )

        lost_cancel.close()
        with blocked:
            assert _receive_control(blocked) == {"type": "CONTROL_FRAME_TIMEOUT"}
        assert cancel_requests == 2
        assert disposition is AgentAttemptCancellationDisposition.NEVER_LAUNCHED
        terminal = store.attest_cancellation_cleanup(
            command, disposition, owner, generation
        )
        supervisor.release(terminal.attempt)
    finally:
        runtime.close()


def _send_without_reading(endpoint: Path, frame: bytes) -> socket.socket:
    connection = _connect_control(endpoint)
    connection.sendall(frame)
    connection.shutdown(socket.SHUT_WR)
    return connection


def _connect_control(endpoint: Path) -> socket.socket:
    control_directory = os.open(endpoint.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        short_endpoint = Path("/proc/self/fd") / str(control_directory) / endpoint.name
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(2)
        connection.connect(str(short_endpoint))
        return connection
    finally:
        os.close(control_directory)


def _receive_control(connection: socket.socket) -> dict[str, object]:
    response_bytes = bytearray()
    while chunk := connection.recv(
        MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES + 1 - len(response_bytes)
    ):
        response_bytes.extend(chunk)
        if len(response_bytes) > MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES:
            raise AssertionError("control response exceeded its test bound")
    response = json.loads(bytes(response_bytes).decode("ascii"))
    assert isinstance(response, dict)
    assert encode_control_frame(response) == bytes(response_bytes)
    return response
