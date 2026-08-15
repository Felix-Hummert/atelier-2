from __future__ import annotations

import base64
import json
import os
import select
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from atelier2.adapters import agent_processes as process_module
from atelier2.adapters.agent_process_protocol import (
    CONTROL_FRAME_TIMEOUT_SECONDS,
    MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES,
    MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES,
    encode_control_frame,
    launch_request,
    maximum_agent_wait_response_bytes,
)
from atelier2.adapters.agent_process_watchdog import Watchdog
from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptProcessPhase,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentExecutionRequestV2,
    AgentExecutionResult,
)
from atelier2.ports.agent_attempts import AgentAttemptSucceeded
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    AgentProcessCompletion,
    AgentProcessInvocation,
    AgentProcessOwnerNotLocal,
)
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.scenarios.agents import (
    SCENARIO_PROVIDER_FRAME_BYTES,
    agent_attempt_execution,
)

_PROVIDER_WRITES_EXACT_BYTES = """
import os, sys
frame = b"o" * int(sys.argv[1])
while frame:
    frame = frame[os.write(1, frame):]
"""

_PROVIDER_EMITS_PADDED_ENVELOPE = """
import json, os, sys
frame = json.dumps({"padding": "x" * int(sys.argv[1]), "result": sys.argv[2]}).encode()
while frame:
    frame = frame[os.write(1, frame):]
"""


@dataclass
class _PaddedEnvelopeExecutor:
    """A provider carrying a small result inside a frame it declares itself."""

    declared_frame_bytes: int
    padding_bytes: int
    result: str = "ok"
    decoded: list[bytes] = field(default_factory=list)
    observed_frame_bytes: int = 0

    def prepare_process(
        self, request: AgentExecutionRequestV2
    ) -> AgentProcessInvocation:
        del request
        return AgentProcessInvocation(
            (
                sys.executable,
                "-c",
                _PROVIDER_EMITS_PADDED_ENVELOPE,
                str(self.padding_bytes),
                self.result,
            ),
            Path.cwd(),
            standard_output_frame_bytes=self.declared_frame_bytes,
        )

    def decode_process_completion(
        self, completion: AgentProcessCompletion
    ) -> AgentExecutionResult:
        self.observed_frame_bytes = len(completion.standard_output)
        envelope = json.loads(completion.standard_output.decode("utf-8"))
        decoded = str(envelope["result"]).encode("utf-8")
        self.decoded.append(decoded)
        return AgentExecutionResult(decoded)

    def close(self) -> None:
        return


def test_a_wire_launch_returns_the_whole_declared_frame_over_a_real_socket(
    tmp_path: Path,
) -> None:
    declared_frame_bytes = 8_192
    endpoint = tmp_path / "control.sock"
    cgroup = tmp_path / "cgroup"
    cgroup.mkdir()
    (cgroup / "cgroup.events").write_text("populated 0\n", encoding="ascii")
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(endpoint, cgroup, owner_pipe, 0.1)
    launch_frame = encode_control_frame(
        launch_request(
            AgentProcessInvocation(
                (
                    sys.executable,
                    "-c",
                    _PROVIDER_WRITES_EXACT_BYTES,
                    str(declared_frame_bytes),
                ),
                Path.cwd(),
                standard_output_frame_bytes=declared_frame_bytes,
            )
        )
    )
    errors: list[Exception] = []
    thread = _start_wire_watchdog(watchdog, endpoint, errors)
    owner_open = True
    try:
        started = _request_control_bytes(endpoint, launch_frame)
        completed = _request_control_bytes(
            endpoint, encode_control_frame({"operation": "WAIT"})
        )
        finalized = _request_control_bytes(
            endpoint, encode_control_frame({"operation": "FINALIZE"})
        )
        os.close(owner_writer)
        owner_open = False
        thread.join(timeout=5)
    finally:
        if owner_open:
            os.close(owner_writer)
        thread.join(timeout=5)
        endpoint.unlink(missing_ok=True)

    assert started == encode_control_frame({"type": "STARTED"})
    assert completed == encode_control_frame(
        {
            "return_code": 0,
            "standard_error": "",
            "standard_output": base64.b64encode(b"o" * declared_frame_bytes).decode(
                "ascii"
            ),
            "type": "COMPLETED",
        }
    )
    assert finalized == encode_control_frame({"type": "FINALIZE_ACCEPTED"})
    assert MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES < len(completed)
    assert len(completed) <= maximum_agent_wait_response_bytes(declared_frame_bytes)
    assert not thread.is_alive()
    assert errors == []


@pytest.mark.parametrize(
    ("contender_frame", "closes_input"),
    (
        pytest.param(
            encode_control_frame({"operation": "WAIT"}), True, id="complete-eof"
        ),
        pytest.param(b"{", False, id="read-timeout"),
        pytest.param(
            b"x" * (MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES + 1),
            False,
            id="request-overflow",
        ),
    ),
)
def test_unclassified_busy_reply_survives_every_bounded_contender_exit(
    running_wire_watchdog: tuple[Watchdog, Path],
    contender_frame: bytes,
    closes_input: bool,
) -> None:
    _watchdog, endpoint = running_wire_watchdog
    with _connect_control(endpoint), _connect_control(endpoint) as contender:
        contender.settimeout(CONTROL_FRAME_TIMEOUT_SECONDS * 2)
        contender.sendall(contender_frame)
        if closes_input:
            contender.shutdown(socket.SHUT_WR)

        assert _receive_control_bytes(contender) == encode_control_frame(
            {"type": "BUSY"}
        )


@pytest.mark.parametrize("_startup", range(5))
def test_repeated_wire_watchdog_start_returns_only_after_listener_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _startup: int,
) -> None:
    endpoint = tmp_path / "control.sock"
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(endpoint, tmp_path / "cgroup", owner_pipe, 0.1)
    original_listen = socket.socket.listen
    listen_entered = threading.Event()
    allow_listener = threading.Event()
    errors: list[Exception] = []
    watchdog_threads: list[threading.Thread] = []

    def pause_listener(server: socket.socket, *arguments: int) -> None:
        listen_entered.set()
        assert allow_listener.wait(timeout=2)
        original_listen(server, *arguments)

    monkeypatch.setattr(socket.socket, "listen", pause_listener)

    def start() -> None:
        watchdog_threads.append(_start_wire_watchdog(watchdog, endpoint, errors))

    starter = threading.Thread(target=start)
    starter.start()
    try:
        assert listen_entered.wait(timeout=2)
        assert endpoint.is_socket()
        assert starter.is_alive()
        with pytest.raises(ConnectionRefusedError), _connect_control(endpoint):
            pass

        allow_listener.set()
        starter.join(timeout=2)
        assert not starter.is_alive()
        assert len(watchdog_threads) == 1
        with _connect_control(endpoint):
            pass
    finally:
        allow_listener.set()
        os.close(owner_writer)
        starter.join(timeout=2)
        for thread in watchdog_threads:
            thread.join(timeout=5)
        endpoint.unlink(missing_ok=True)
    assert errors == []
    assert all(not thread.is_alive() for thread in watchdog_threads)


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
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)

        completion = supervisor.launch_and_wait(execution, invocation)

        assert completion.standard_output == b"o" * SCENARIO_PROVIDER_FRAME_BYTES
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


@pytest.mark.parametrize(
    "declared_frame_bytes", (0, -1, 1.0), ids=("zero", "negative", "not-an-integer")
)
def test_an_invocation_without_a_positive_declared_frame_is_refused(
    declared_frame_bytes: Any,
) -> None:
    with pytest.raises(ValueError, match="standard output frame"):
        AgentProcessInvocation(
            (sys.executable, "-c", "pass"),
            Path.cwd(),
            standard_output_frame_bytes=declared_frame_bytes,
        )


@pytest.mark.parametrize("excess_bytes", (0, 1), ids=("at-the-bound", "one-byte-over"))
def test_supervision_admits_the_declared_frame_and_refuses_one_byte_more(
    tmp_path: Path, excess_bytes: int
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    declared_frame_bytes = 8_192
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, f"frame/edge/{excess_bytes}")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        supervisor = runtime.agent_process_supervisor
        invocation = AgentProcessInvocation(
            (
                sys.executable,
                "-c",
                _PROVIDER_WRITES_EXACT_BYTES,
                str(declared_frame_bytes + excess_bytes),
            ),
            Path.cwd(),
            standard_output_frame_bytes=declared_frame_bytes,
        )
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)

        if excess_bytes:
            with pytest.raises(RuntimeError, match="did not return a process"):
                supervisor.launch_and_wait(execution, invocation)
        else:
            completion = supervisor.launch_and_wait(execution, invocation)
            assert completion.standard_output == b"o" * declared_frame_bytes
    finally:
        runtime.close()


def test_supervision_holds_each_provider_to_its_own_declared_frame(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    padding_bytes = MAXIMUM_AGENT_OUTPUT_BYTES_V2 + 16_384
    try:
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        generous = _PaddedEnvelopeExecutor(2 * padding_bytes, padding_bytes)
        frugal = _PaddedEnvelopeExecutor(4_096, padding_bytes)
        generous_execution = agent_attempt_execution(
            attempt_request(runtime, "frame/generous")
        )
        frugal_execution = agent_attempt_execution(
            attempt_request(runtime, "frame/frugal")
        )

        accepted = execute_agent_attempt(
            generous_execution, generous, store, runtime.agent_process_supervisor
        )
        with pytest.raises(RuntimeError, match="did not return a process"):
            execute_agent_attempt(
                frugal_execution, frugal, store, runtime.agent_process_supervisor
            )

        assert isinstance(accepted, AgentAttemptSucceeded)
        assert generous.decoded == [b"ok"]
        assert generous.observed_frame_bytes > MAXIMUM_AGENT_OUTPUT_BYTES_V2
        assert frugal.decoded == []
        assert (
            store.load(frugal_execution.attempt_id).state
            is not AgentAttemptState.SUCCEEDED
        )
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
                "from pathlib import Path; import os,sys; Path(sys.argv[1]).open('ab').write(b'x'); Path(sys.argv[2]).open('rb', buffering=0).read(1); os.write(1,b'done')",
                str(counter),
                str(tmp_path / "provider-release"),
            ),
            Path.cwd(),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )
        os.mkfifo(tmp_path / "provider-release")
        store.prepare(execution)
        supervisor.prepare(execution)
        store.claim(execution)
        owned = supervisor._owned[execution.attempt_id]
        assert owned is not None
        launch_frame = encode_control_frame(launch_request(invocation))
        lost_launch = _send_without_reading(owned.endpoint, launch_frame)
        ready_launches, _writable, _failed = select.select((lost_launch,), (), (), 5)
        assert ready_launches == [lost_launch]
        started = _request_control_bytes(owned.endpoint, launch_frame)
        assert started == encode_control_frame({"type": "STARTED"})
        assert _request_control_bytes(owned.endpoint, launch_frame) == started
        partial_wait = _send_without_reading(
            owned.endpoint, encode_control_frame({"operation": "WAIT"})
        )
        with (tmp_path / "provider-release").open("wb", buffering=0) as release:
            release.write(b"x")
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
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )
        terminal_frame = encode_control_frame(launch_request(invocation))
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
        assert cancel_requests == process_module.MAXIMUM_AGENT_CONTROL_REQUEST_ATTEMPTS
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
        (
            "decode-oversized",
            b"x" * (MAXIMUM_AGENT_CONTROL_RESPONSE_BYTES + 1),
        ),
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
        invocation = AgentProcessInvocation(
            (sys.executable, "-c", "pass"),
            Path.cwd(),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )
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
            assert requests == [encode_control_frame(launch_request(invocation))] * 2
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
            for _retry in range(process_module.MAXIMUM_AGENT_CONTROL_REQUEST_ATTEMPTS):
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


def _start_wire_watchdog(
    watchdog: Watchdog, endpoint: Path, errors: list[Exception]
) -> threading.Thread:
    ready = threading.Event()

    def serve() -> None:
        try:
            watchdog.serve(ready.set)
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            errors.append(error)
            ready.set()

    thread = threading.Thread(target=serve)
    thread.start()
    assert ready.wait(timeout=2)
    assert errors == []
    assert endpoint.is_socket()
    return thread


@pytest.fixture
def running_wire_watchdog(tmp_path: Path) -> Iterator[tuple[Watchdog, Path]]:
    endpoint = tmp_path / "control.sock"
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(endpoint, tmp_path / "cgroup", owner_pipe, 0.1)
    errors: list[Exception] = []
    thread = _start_wire_watchdog(watchdog, endpoint, errors)
    try:
        yield watchdog, endpoint
    finally:
        os.close(owner_writer)
        thread.join(timeout=5)
        endpoint.unlink(missing_ok=True)
    assert not thread.is_alive()
    assert errors == []


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
