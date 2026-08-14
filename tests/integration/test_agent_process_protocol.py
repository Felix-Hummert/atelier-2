from __future__ import annotations

import base64
import json
import os
import select
import selectors
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest

from atelier2.adapters import agent_process_watchdog as watchdog_module
from atelier2.adapters import agent_processes as process_module
from atelier2.adapters.agent_process_watchdog import (
    MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES,
    MAXIMUM_AGENT_WAIT_RESPONSE_BYTES_V2,
    Watchdog,
    encode_control_frame,
)
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptProcessPhase,
    AgentAttemptReplacement,
    AgentAttemptState,
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


def test_running_watchdog_bounds_four_control_roles_independently(
    running_wire_watchdog: tuple[Watchdog, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    watchdog, endpoint = running_wire_watchdog
    held = {operation: threading.Event() for operation in ("LAUNCH", "WAIT", "CANCEL")}
    for handler, event in zip(
        ("_handle_launch", "_handle_wait", "_handle_cancel"),
        held.values(),
        strict=True,
    ):
        monkeypatch.setattr(watchdog, handler, lambda *_args, event=event: event.set())

    with ExitStack() as clients:
        for operation, admitted in held.items():
            clients.enter_context(
                _send_without_reading(
                    endpoint, encode_control_frame({"operation": operation})
                )
            )
            assert admitted.wait(timeout=2)
        unclassified = clients.enter_context(_connect_control(endpoint))
        _wait_until(lambda: "UNCLASSIFIED" in watchdog._slots)
        with _connect_control(endpoint) as competing:
            assert _receive_control_bytes(competing) == encode_control_frame(
                {"type": "BUSY"}
            )
        unclassified.close()
        _wait_until(lambda: "UNCLASSIFIED" not in watchdog._slots)

        for operation in ("LAUNCH", "WAIT", "FINALIZE"):
            assert _request_control_bytes(
                endpoint, encode_control_frame({"operation": operation})
            ) == encode_control_frame({"type": "BUSY"})


def test_running_watchdog_times_out_a_stalled_response_then_replays_it(
    running_wire_watchdog: tuple[Watchdog, Path],
) -> None:
    watchdog, endpoint = running_wire_watchdog
    with _send_without_reading(endpoint, encode_control_frame({"operation": "WAIT"})):
        _wait_until(
            lambda: any(
                connection.operation == "WAIT"
                for connection in watchdog._connections.values()
            )
        )
        connection = next(iter(watchdog._connections.values()))
        connection.socket.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1_024)
        watchdog._publish_wait(
            {
                "return_code": -(2**31),
                "standard_error": base64.b64encode(
                    b"e" * MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
                ).decode("ascii"),
                "standard_output": base64.b64encode(
                    b"o" * MAXIMUM_AGENT_OUTPUT_BYTES_V2
                ).decode("ascii"),
                "type": "COMPLETED",
            },
            time.monotonic(),
        )
        cached = watchdog._wait_response
        assert cached is not None
        assert len(cached) == MAXIMUM_AGENT_WAIT_RESPONSE_BYTES_V2
        _wait_until(lambda: 0 < connection.output_offset < len(cached))
        _wait_until(lambda: connection.socket.fileno() == -1)
        assert connection.response_deadline is not None
        assert time.monotonic() >= connection.response_deadline
        assert (
            _request_control_bytes(
                endpoint, encode_control_frame({"operation": "WAIT"})
            )
            == cached
        )


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
                "from pathlib import Path; import os,sys,time; Path(sys.argv[1]).open('ab').write(b'x'); time.sleep(.2); os.write(1,b'done')",
                str(counter),
            ),
            Path.cwd(),
        )
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        owned = supervisor._owned[execution.attempt_id]
        assert owned is not None
        launch_frame = encode_control_frame(process_module._launch_request(invocation))
        lost_launch = _send_without_reading(owned.endpoint, launch_frame)
        ready_launches, _writable, _failed = select.select((lost_launch,), (), (), 5)
        assert ready_launches == [lost_launch]
        started = _request_control_bytes(owned.endpoint, launch_frame)
        assert started == encode_control_frame({"type": "STARTED"})
        assert _request_control_bytes(owned.endpoint, launch_frame) == started
        partial_wait = _send_without_reading(
            owned.endpoint, encode_control_frame({"operation": "WAIT"})
        )
        assert partial_wait.recv(8) == b'{"return'
        partial_wait.close()

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
        ready_finalizers, _writable, _failed = select.select(
            (lost_finalize,), (), (), 5
        )
        assert ready_finalizers == [lost_finalize]
        finalized = _request_control_bytes(
            owned.endpoint, encode_control_frame({"operation": "FINALIZE"})
        )
        assert finalized == encode_control_frame({"type": "FINALIZE_ACCEPTED"})
        assert (
            _request_control_bytes(
                owned.endpoint, encode_control_frame({"operation": "FINALIZE"})
            )
            == finalized
        )
        supervisor.finalize(execution)
        lost_launch.close()
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
        with _send_without_reading(
            owned.endpoint, b"x" * (MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES + 1)
        ) as oversized:
            assert _receive_control(oversized) == {"type": "FRAME_TOO_LARGE"}
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
        invocation = AgentProcessInvocation(
            (
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).touch()",
                str(counter),
            ),
            Path.cwd(),
        )
        terminal_frame = encode_control_frame(
            process_module._launch_request(invocation)
        )
        terminal_before_start = _request_control_bytes(owned.endpoint, terminal_frame)
        assert terminal_before_start == encode_control_frame(
            {"outcome": "STOPPED", "type": "TERMINAL_BEFORE_START"}
        )
        assert (
            _request_control_bytes(owned.endpoint, terminal_frame)
            == terminal_before_start
        )
        with pytest.raises(AgentProcessOwnerNotLocal):
            supervisor.launch_and_wait(execution, invocation)
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
        ) -> dict[str, object]:
            nonlocal cancel_requests
            if request.get("operation") == "CANCEL":
                cancel_requests += 1
            return request_once(endpoint, request, timeout_seconds=timeout_seconds)

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


@pytest.mark.parametrize(
    ("peer_phase", "peer_response"),
    (
        ("connect", None),
        ("send", b""),
        ("partial-read", b'{"type":'),
        ("decode-noncanonical", b'{"type": "STARTED"}'),
        ("decode-oversized", b"x" * 4_097),
    ),
)
def test_real_transport_failures_retain_exact_durable_launch_authority(
    tmp_path: Path,
    peer_phase: str,
    peer_response: bytes | None,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, f"process/transport/{peer_phase}")
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
        real_endpoint = owned.endpoint
        peer_endpoint = real_endpoint.with_name(f"peer-{peer_phase}.sock")
        requests: list[bytes] = []
        peer_errors: list[BaseException] = []
        peer_thread = (
            _serve_control_responses(
                peer_endpoint, peer_response, requests, peer_errors
            )
            if peer_response is not None
            else None
        )
        invocation = AgentProcessInvocation((sys.executable, "-c", "pass"), Path.cwd())
        owned.endpoint = peer_endpoint
        try:
            with pytest.raises(AgentProcessOwnerNotLocal):
                supervisor.launch_and_wait(execution, invocation)
        finally:
            owned.endpoint = real_endpoint
            if peer_thread is not None:
                peer_thread.join(timeout=5)
                peer_endpoint.unlink(missing_ok=True)

        if peer_thread is not None:
            assert not peer_thread.is_alive()
            assert peer_errors == []
            assert (
                requests
                == [encode_control_frame(process_module._launch_request(invocation))]
                * 2
            )
        assert supervisor._owned[execution.attempt_id] is owned
        assert owned.process.poll() is None
        assert real_endpoint.is_socket()
        assert owned.launched is False
        durable = store.load(execution.attempt_id)
        assert durable.state is AgentAttemptState.LAUNCH_ARMED
        assert durable.process_phase is AgentAttemptProcessPhase.LAUNCH_AUTHORIZED
        assert durable.process_owner_id == owned.owner
        assert durable.watchdog_generation_id == owned.generation

        command = CancelAgentAttemptRequest(
            durable.run_id,
            durable.attempt_id,
            "cleanup-transport-uncertainty",
            durable.state_version,
            AgentAttemptReplacement.NONE,
        )
        store.request_cancellation(command)
        disposition, owner, generation = supervisor.cancel(
            store.load(execution.attempt_id)
        )
        terminal = store.attest_cancellation_cleanup(
            command, disposition, owner, generation
        )
        supervisor.release(terminal.attempt)
    finally:
        runtime.close()


def _serve_control_responses(
    endpoint: Path,
    response: bytes,
    requests: list[bytes],
    errors: list[BaseException],
) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        control_directory = os.open(endpoint.parent, os.O_RDONLY | os.O_DIRECTORY)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            short_endpoint = (
                Path("/proc/self/fd") / str(control_directory) / endpoint.name
            )
            server.bind(str(short_endpoint))
            server.listen()
            server.settimeout(5)
            ready.set()
            for _retry in range(2):
                with server.accept()[0] as connection:
                    connection.settimeout(2)
                    request = bytearray()
                    while chunk := connection.recv(65_536):
                        request.extend(chunk)
                    requests.append(bytes(request))
                    if response:
                        connection.sendall(response)
        except OSError as error:
            errors.append(error)
            ready.set()
        finally:
            server.close()
            os.close(control_directory)

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(timeout=2)
    assert errors == []
    return thread


@pytest.fixture
def running_wire_watchdog(tmp_path: Path) -> Iterator[tuple[Watchdog, Path]]:
    endpoint = tmp_path / "control.sock"
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(endpoint, tmp_path / "cgroup", owner_pipe, 0.1)
    errors: list[Exception] = []

    def serve() -> None:
        try:
            watchdog.serve()
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            errors.append(error)

    thread = threading.Thread(target=serve)
    thread.start()
    _wait_until(endpoint.is_socket)
    try:
        yield watchdog, endpoint
    finally:
        os.close(owner_writer)
        thread.join(timeout=5)
        endpoint.unlink(missing_ok=True)
    assert not thread.is_alive()
    assert errors == []


def _wait_until(predicate: Callable[[], bool]) -> None:
    deadline = time.monotonic() + 2
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("watchdog did not reach the expected wire state")
        time.sleep(0.005)


def _send_without_reading(endpoint: Path, frame: bytes) -> socket.socket:
    connection = _connect_control(endpoint)
    connection.sendall(frame)
    connection.shutdown(socket.SHUT_WR)
    return connection


def _request_control_bytes(endpoint: Path, frame: bytes) -> bytes:
    with _send_without_reading(endpoint, frame) as connection:
        return _receive_control_bytes(connection)


def _receive_control_bytes(connection: socket.socket) -> bytes:
    response = bytearray()
    while chunk := connection.recv(65_536):
        response.extend(chunk)
    return bytes(response)


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
    while chunk := connection.recv(4_097 - len(response_bytes)):
        response_bytes.extend(chunk)
        if len(response_bytes) > 4_096:
            raise AssertionError("control response exceeded its test bound")
    response = json.loads(bytes(response_bytes).decode("ascii"))
    assert isinstance(response, dict)
    assert encode_control_frame(response) == bytes(response_bytes)
    return response
