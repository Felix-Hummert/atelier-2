from __future__ import annotations

import argparse
import base64
import json
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from atelier2.contracts.agents import MAXIMUM_AGENT_OUTPUT_BYTES_V2
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_INPUT_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
)

MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES = 262_144
CONTROL_FRAME_TIMEOUT_SECONDS = 1.0


def encode_control_frame(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True
    ).encode("ascii")


def _maximum_completed_frame() -> bytes:
    encoded = base64.b64encode(b"x" * MAXIMUM_AGENT_OUTPUT_BYTES_V2).decode("ascii")
    return encode_control_frame(
        {
            "return_code": -(2**31),
            "standard_error": encoded,
            "standard_output": encoded,
            "type": "COMPLETED",
        }
    )


MAXIMUM_AGENT_WAIT_RESPONSE_BYTES_V2 = max(
    len(_maximum_completed_frame()),
    *(
        len(encode_control_frame({"type": arm}))
        for arm in (
            "OUTPUT_LIMIT_EXCEEDED",
            "SUPERVISION_FAILED",
            "STOPPED",
            "RECOVERY_HANDOFF",
        )
    ),
)


class _CoordinatorState(StrEnum):
    READY = "READY"
    LAUNCHING = "LAUNCHING"
    RUNNING = "RUNNING"
    CANCEL_TERMINATING = "CANCEL_TERMINATING"
    OVERFLOW_TERMINATING = "OVERFLOW_TERMINATING"
    SUPERVISION_TERMINATING = "SUPERVISION_TERMINATING"
    OWNER_DEATH_TERMINATING = "OWNER_DEATH_TERMINATING"
    RECOVERY_HANDOFF = "RECOVERY_HANDOFF"
    TERMINATED = "TERMINATED"
    FINALIZING = "FINALIZING"


@dataclass
class _Connection:
    socket: socket.socket
    accepted_at: float
    input_bytes: bytearray = field(default_factory=bytearray)
    output_bytes: bytes | None = None
    output_offset: int = 0
    response_deadline: float | None = None
    slot: str = "UNCLASSIFIED"
    operation: str | None = None


class Watchdog:
    """One selector-driven authority for a single provider generation."""

    def __init__(
        self,
        endpoint: Path,
        cgroup: Path,
        owner_pipe: int,
        attempt_lock: int,
        grace: float,
    ) -> None:
        self._endpoint = endpoint
        self._cgroup = cgroup
        self._owner_pipe = owner_pipe
        self._attempt_lock = attempt_lock
        self._grace = grace
        self._selector = selectors.DefaultSelector()
        self._server: socket.socket | None = None
        self._connections: dict[int, _Connection] = {}
        self._slots: dict[str, int] = {}
        self._state = _CoordinatorState.READY
        self._process: subprocess.Popen[bytes] | None = None
        self._provider_streams: dict[int, str] = {}
        self._standard_input = b""
        self._standard_input_offset = 0
        self._standard_output = bytearray()
        self._standard_error = bytearray()
        self._launch_frame: bytes | None = None
        self._launch_response: bytes | None = None
        self._wait_response: bytes | None = None
        self._cancel_response: bytes | None = None
        self._termination_deadline: float | None = None
        self._termination_escalated = False
        self._termination_disposition: str | None = None
        self._termination_owner: str | None = None
        self._owner_dead = False
        self._finalize_after_write = False

    def serve(self) -> None:
        self._endpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._endpoint.exists():
            raise RuntimeError("watchdog endpoint already exists")
        os.set_blocking(self._owner_pipe, False)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        try:
            server.bind(str(self._endpoint))
            os.chmod(self._endpoint, 0o600)
            server.listen()
            server.setblocking(False)
            self._selector.register(server, selectors.EVENT_READ, "server")
            self._selector.register(self._owner_pipe, selectors.EVENT_READ, "owner")
            print("READY", flush=True)
            while self._state is not _CoordinatorState.FINALIZING:
                try:
                    self._tick()
                except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
                    if self._termination_owner is None:
                        self._begin_termination("SUPERVISION", time.monotonic())
                    else:
                        self._publish_recovery_handoff(time.monotonic())
            self._flush_final_responses()
        finally:
            self._close_provider_descriptors()
            for connection in tuple(self._connections.values()):
                self._close_connection(connection)
            if self._server is not None:
                try:
                    self._selector.unregister(self._server)
                except (KeyError, ValueError):
                    pass
                self._server.close()
            try:
                self._selector.unregister(self._owner_pipe)
            except (KeyError, ValueError):
                pass
            os.close(self._owner_pipe)
            os.close(self._attempt_lock)
            self._selector.close()

    def _tick(self) -> None:
        now = time.monotonic()
        self._expire_connections(now)
        self._advance_process(now)
        timeout = self._next_timeout(now)
        try:
            events = self._selector.select(timeout)
        except OSError:
            self._begin_termination("SUPERVISION", now)
            return
        for key, mask in events:
            if key.data == "server":
                self._accept_connection(now)
            elif key.data == "owner":
                self._read_owner(now)
            elif isinstance(key.data, _Connection):
                self._service_connection(key.data, mask, now)
            else:
                self._service_provider(int(key.fd), str(key.data), mask, now)

    def _next_timeout(self, now: float) -> float:
        deadlines = [
            connection.response_deadline
            if connection.output_bytes is not None
            else (
                connection.accepted_at + CONTROL_FRAME_TIMEOUT_SECONDS
                if connection.operation is None
                else None
            )
            for connection in self._connections.values()
        ]
        deadlines.append(self._termination_deadline)
        finite = [deadline for deadline in deadlines if deadline is not None]
        if not finite:
            return 0.05
        return max(0.0, min(0.05, min(finite) - now))

    def _accept_connection(self, now: float) -> None:
        if self._server is None:
            return
        try:
            connection, _address = self._server.accept()
        except BlockingIOError:
            return
        connection.setblocking(False)
        state = _Connection(connection, now)
        descriptor = connection.fileno()
        if "UNCLASSIFIED" in self._slots:
            self._connections[descriptor] = state
            self._selector.register(connection, selectors.EVENT_WRITE, state)
            self._queue_response(state, {"type": "BUSY"}, now)
            return
        self._slots["UNCLASSIFIED"] = descriptor
        self._connections[descriptor] = state
        self._selector.register(connection, selectors.EVENT_READ, state)

    def _read_owner(self, now: float) -> None:
        try:
            data = os.read(self._owner_pipe, 1)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            return
        try:
            self._selector.unregister(self._owner_pipe)
        except (KeyError, ValueError):
            pass
        self._owner_dead = True
        self._begin_termination("OWNER_DEATH", now)

    def _service_connection(self, state: _Connection, mask: int, now: float) -> None:
        if mask & selectors.EVENT_READ:
            self._read_connection(state, now)
        if state.socket.fileno() >= 0 and mask & selectors.EVENT_WRITE:
            self._write_connection(state, now)

    def _read_connection(self, state: _Connection, now: float) -> None:
        try:
            remaining = MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES + 1 - len(state.input_bytes)
            chunk = state.socket.recv(max(1, min(65_536, remaining)))
        except BlockingIOError:
            return
        except OSError:
            self._close_connection(state)
            return
        if chunk:
            state.input_bytes.extend(chunk)
            if len(state.input_bytes) > MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES:
                self._queue_response(state, {"type": "FRAME_TOO_LARGE"}, now)
            return
        self._classify_request(state, bytes(state.input_bytes), now)

    def _classify_request(self, state: _Connection, frame: bytes, now: float) -> None:
        try:
            request = json.loads(frame.decode("ascii"))
            if not isinstance(request, dict) or encode_control_frame(request) != frame:
                raise ValueError
            operation = request.get("operation")
            if not isinstance(operation, str):
                raise TypeError
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            self._queue_response(state, {"type": "MALFORMED"}, now)
            return
        slot = {
            "WAIT": "WAIT",
            "CANCEL": "TERMINAL_CONTROL",
            "FINALIZE": "TERMINAL_CONTROL",
            "LAUNCH": "LAUNCH_RETRY",
        }.get(operation)
        if slot is None:
            self._queue_response(state, {"type": "MALFORMED"}, now)
            return
        descriptor = state.socket.fileno()
        self._release_slot(state)
        if slot in self._slots:
            self._queue_response(state, {"type": "BUSY"}, now)
            return
        self._slots[slot] = descriptor
        state.slot = slot
        state.operation = operation
        if operation == "LAUNCH":
            self._handle_launch(state, request, frame, now)
        elif operation == "WAIT":
            self._handle_wait(state, now)
        elif operation == "CANCEL":
            self._handle_cancel(state, now)
        else:
            self._handle_finalize(state, now)

    def _handle_launch(
        self,
        connection: _Connection,
        request: dict[str, Any],
        frame: bytes,
        now: float,
    ) -> None:
        if self._launch_frame is not None:
            response = (
                self._launch_response
                if frame == self._launch_frame
                else encode_control_frame({"type": "LAUNCH_MISMATCH"})
            )
            assert response is not None
            self._queue_encoded_response(connection, response, now)
            return
        self._launch_frame = frame
        if self._state is not _CoordinatorState.READY:
            self._launch_response = encode_control_frame(
                {
                    "outcome": "STOPPED",
                    "type": "TERMINAL_BEFORE_START",
                }
            )
            self._publish_wait({"type": "STOPPED"}, now)
            self._termination_disposition = "NEVER_LAUNCHED"
            self._queue_encoded_response(connection, self._launch_response, now)
            return
        self._state = _CoordinatorState.LAUNCHING
        try:
            arguments, working_directory, environment, standard_input = (
                _decode_launch_request(request)
            )
            guarded = (
                sys.executable,
                "-m",
                "atelier2.adapters.agent_process_exec_guard",
                "--cgroup",
                str(self._cgroup),
                "--watchdog-pid",
                str(os.getpid()),
                "--",
                *arguments,
            )
            process = subprocess.Popen(
                guarded,
                cwd=working_directory,
                env={
                    **os.environ,
                    "ATELIER2_AGENT_ENVIRONMENT_B64": base64.b64encode(
                        json.dumps(
                            sorted(environment.items()), separators=(",", ":")
                        ).encode("utf-8")
                    ).decode("ascii"),
                },
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self._process = process
            self._standard_input = standard_input
            self._launch_response = encode_control_frame({"type": "STARTED"})
            try:
                self._configure_provider_descriptors(process)
            except (OSError, RuntimeError, ValueError):
                self._close_provider_descriptors()
                self._begin_termination("SUPERVISION", now)
            else:
                self._state = _CoordinatorState.RUNNING
        except (KeyError, OSError, subprocess.SubprocessError, TypeError, ValueError):
            self._close_provider_descriptors()
            self._launch_response = encode_control_frame(
                {
                    "outcome": "SUPERVISION_FAILED",
                    "type": "TERMINAL_BEFORE_START",
                }
            )
            self._termination_disposition = "REAPED_AFTER_PROCESS_BOUNDARY_FAILURE"
            self._publish_wait({"type": "SUPERVISION_FAILED"}, now)
        self._queue_encoded_response(connection, self._launch_response, now)

    def _handle_wait(self, connection: _Connection, now: float) -> None:
        if self._wait_response is not None:
            self._queue_encoded_response(connection, self._wait_response, now)

    def _handle_cancel(self, connection: _Connection, now: float) -> None:
        if self._cancel_response is not None:
            self._queue_encoded_response(connection, self._cancel_response, now)
            return
        if self._wait_response is not None:
            disposition = self._termination_disposition or "EXITED_BEFORE_SIGNAL"
            self._cancel_response = encode_control_frame(
                {"disposition": disposition, "type": "CANCELLED"}
            )
            self._queue_encoded_response(connection, self._cancel_response, now)
            return
        self._begin_termination("CANCEL", now)

    def _handle_finalize(self, connection: _Connection, now: float) -> None:
        if self._wait_response is None:
            self._queue_response(connection, {"type": "FINALIZE_REFUSED"}, now)
            return
        self._state = _CoordinatorState.FINALIZING
        self._stop_admission()
        self._finalize_after_write = True
        self._queue_response(connection, {"type": "FINALIZE_ACCEPTED"}, now)

    def _configure_provider_descriptors(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("provider pipes are absent")
        for stream, role, events in (
            (process.stdin, "stdin", selectors.EVENT_WRITE),
            (process.stdout, "stdout", selectors.EVENT_READ),
            (process.stderr, "stderr", selectors.EVENT_READ),
        ):
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            self._provider_streams[descriptor] = role
            self._selector.register(descriptor, events, role)
        if not self._standard_input:
            self._close_provider_stream("stdin")

    def _service_provider(
        self, descriptor: int, role: str, _mask: int, now: float
    ) -> None:
        try:
            if role == "stdin":
                self._write_standard_input(descriptor)
            else:
                self._read_provider_output(descriptor, role, now)
        except BrokenPipeError:
            if role == "stdin":
                self._close_provider_stream(role)
            else:
                self._begin_termination("SUPERVISION", now)
        except OSError:
            self._begin_termination("SUPERVISION", now)

    def _write_standard_input(self, descriptor: int) -> None:
        try:
            written = os.write(
                descriptor, self._standard_input[self._standard_input_offset :]
            )
        except BlockingIOError:
            return
        self._standard_input_offset += written
        if self._standard_input_offset == len(self._standard_input):
            self._close_provider_stream("stdin")

    def _read_provider_output(self, descriptor: int, role: str, now: float) -> None:
        try:
            chunk = os.read(descriptor, 65_536)
        except BlockingIOError:
            return
        if not chunk:
            self._close_provider_stream(role)
            return
        if self._termination_owner is not None:
            return
        target = self._standard_output if role == "stdout" else self._standard_error
        target.extend(chunk)
        limit = (
            MAXIMUM_AGENT_OUTPUT_BYTES_V2
            if role == "stdout"
            else MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
        )
        if len(target) > limit:
            self._standard_output.clear()
            self._standard_error.clear()
            self._standard_input = b""
            self._close_provider_stream("stdin")
            self._begin_termination("OVERFLOW", now)

    def _advance_process(self, now: float) -> None:
        process = self._process
        if process is None:
            return
        return_code = process.poll()
        if self._termination_owner is None:
            if (
                return_code is not None
                and not self._has_provider_stream("stdout")
                and not self._has_provider_stream("stderr")
                and not _cgroup_populated(self._cgroup)
            ):
                process.wait()
                self._publish_wait(
                    {
                        "return_code": int(return_code),
                        "standard_error": base64.b64encode(self._standard_error).decode(
                            "ascii"
                        ),
                        "standard_output": base64.b64encode(
                            self._standard_output
                        ).decode("ascii"),
                        "type": "COMPLETED",
                    },
                    now,
                )
            return
        if (
            process.poll() is not None
            and not _cgroup_populated(self._cgroup)
            and not self._has_provider_stream("stdout")
            and not self._has_provider_stream("stderr")
        ):
            process.wait()
            self._finish_termination(now)
            return
        if self._termination_deadline is not None and now >= self._termination_deadline:
            self._escalate_termination(now)

    def _begin_termination(self, owner: str, now: float) -> None:
        if self._termination_owner is not None:
            if owner == "OWNER_DEATH" and self._wait_response is not None:
                self._state = _CoordinatorState.FINALIZING
            return
        self._termination_owner = owner
        self._termination_escalated = False
        self._standard_input = b""
        self._close_provider_stream("stdin")
        if self._process is None:
            self._termination_disposition = "NEVER_LAUNCHED"
            if owner == "CANCEL":
                self._publish_wait({"type": "STOPPED"}, now)
                self._publish_cancel(now)
            elif owner == "OWNER_DEATH":
                self._state = _CoordinatorState.FINALIZING
            else:
                arm = (
                    "OUTPUT_LIMIT_EXCEEDED"
                    if owner == "OVERFLOW"
                    else "SUPERVISION_FAILED"
                )
                self._publish_wait({"type": arm}, now)
            return
        self._state = {
            "CANCEL": _CoordinatorState.CANCEL_TERMINATING,
            "OVERFLOW": _CoordinatorState.OVERFLOW_TERMINATING,
            "SUPERVISION": _CoordinatorState.SUPERVISION_TERMINATING,
            "OWNER_DEATH": _CoordinatorState.OWNER_DEATH_TERMINATING,
        }[owner]
        if self._process.poll() is not None and not _cgroup_populated(self._cgroup):
            self._termination_disposition = "EXITED_BEFORE_SIGNAL"
            if not self._has_provider_stream(
                "stdout"
            ) and not self._has_provider_stream("stderr"):
                self._process.wait()
                self._finish_termination(now)
                return
            self._termination_deadline = now + self._grace
            return
        signalled = False
        if self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGTERM)
                signalled = True
            except ProcessLookupError:
                pass
        if signalled:
            self._termination_disposition = "REAPED_AFTER_TERM"
        self._termination_deadline = now + self._grace

    def _escalate_termination(self, now: float) -> None:
        if self._termination_escalated:
            self._publish_recovery_handoff(now)
            return
        self._termination_escalated = True
        self._termination_deadline = None
        if _cgroup_populated(self._cgroup):
            (self._cgroup / "cgroup.kill").write_text("1", encoding="ascii")
            self._termination_disposition = "REAPED_AFTER_KILL"
        if self._process is not None and self._process.poll() is None:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._termination_deadline = now + max(1.0, self._grace)

    def _finish_termination(self, now: float) -> None:
        owner = self._termination_owner
        if owner == "OWNER_DEATH" or self._owner_dead:
            self._state = _CoordinatorState.FINALIZING
            return
        if owner == "CANCEL":
            self._publish_wait({"type": "STOPPED"}, now)
            self._publish_cancel(now)
            return
        if owner == "OVERFLOW":
            self._termination_disposition = "REAPED_AFTER_PROCESS_BOUNDARY_FAILURE"
            self._publish_wait({"type": "OUTPUT_LIMIT_EXCEEDED"}, now)
            self._publish_cancel(now)
            return
        self._termination_disposition = "REAPED_AFTER_PROCESS_BOUNDARY_FAILURE"
        self._publish_wait({"type": "SUPERVISION_FAILED"}, now)
        self._publish_cancel(now)

    def _publish_recovery_handoff(self, now: float) -> None:
        encoded = encode_control_frame({"type": "RECOVERY_HANDOFF"})
        self._wait_response = encoded
        self._cancel_response = encoded
        self._state = _CoordinatorState.FINALIZING
        self._stop_admission()
        self._finalize_after_write = True
        for connection in tuple(self._connections.values()):
            if (
                connection.operation in {"WAIT", "CANCEL"}
                and connection.output_bytes is None
            ):
                self._queue_encoded_response(connection, encoded, now)

    def _publish_cancel(self, now: float) -> None:
        disposition = self._termination_disposition or "EXITED_BEFORE_SIGNAL"
        self._cancel_response = encode_control_frame(
            {"disposition": disposition, "type": "CANCELLED"}
        )
        for connection in tuple(self._connections.values()):
            if connection.operation == "CANCEL" and connection.output_bytes is None:
                self._queue_encoded_response(connection, self._cancel_response, now)

    def _publish_wait(self, response: dict[str, object], now: float) -> None:
        if self._wait_response is not None:
            return
        encoded = encode_control_frame(response)
        if len(encoded) > MAXIMUM_AGENT_WAIT_RESPONSE_BYTES_V2:
            encoded = encode_control_frame({"type": "SUPERVISION_FAILED"})
        self._wait_response = encoded
        self._state = _CoordinatorState.TERMINATED
        for connection in tuple(self._connections.values()):
            if connection.operation == "WAIT" and connection.output_bytes is None:
                self._queue_encoded_response(connection, encoded, now)

    def _expire_connections(self, now: float) -> None:
        for connection in tuple(self._connections.values()):
            if connection.output_bytes is None:
                if (
                    connection.operation is None
                    and now >= connection.accepted_at + CONTROL_FRAME_TIMEOUT_SECONDS
                ):
                    self._queue_response(
                        connection, {"type": "CONTROL_FRAME_TIMEOUT"}, now
                    )
            elif (
                connection.response_deadline is not None
                and now >= connection.response_deadline
            ):
                self._close_connection(connection)

    def _queue_response(
        self, connection: _Connection, response: dict[str, object], now: float
    ) -> None:
        self._queue_encoded_response(connection, encode_control_frame(response), now)

    def _queue_encoded_response(
        self, connection: _Connection, response: bytes, now: float
    ) -> None:
        connection.output_bytes = response
        connection.output_offset = 0
        connection.response_deadline = now + CONTROL_FRAME_TIMEOUT_SECONDS
        try:
            self._selector.modify(connection.socket, selectors.EVENT_WRITE, connection)
        except (KeyError, ValueError):
            self._close_connection(connection)

    def _write_connection(self, connection: _Connection, _now: float) -> None:
        response = connection.output_bytes
        if response is None:
            return
        try:
            sent = connection.socket.send(response[connection.output_offset :])
        except BlockingIOError:
            return
        except OSError:
            self._close_connection(connection)
            return
        connection.output_offset += sent
        if connection.output_offset == len(response):
            self._close_connection(connection)

    def _release_slot(self, connection: _Connection) -> None:
        descriptor = connection.socket.fileno()
        if self._slots.get(connection.slot) == descriptor:
            self._slots.pop(connection.slot, None)
        connection.slot = ""

    def _close_connection(self, connection: _Connection) -> None:
        descriptor = connection.socket.fileno()
        self._release_slot(connection)
        self._connections.pop(descriptor, None)
        try:
            self._selector.unregister(connection.socket)
        except (KeyError, ValueError):
            pass
        connection.socket.close()

    def _has_provider_stream(self, role: str) -> bool:
        return role in self._provider_streams.values()

    def _close_provider_stream(self, role: str) -> None:
        descriptor = next(
            (fd for fd, current in self._provider_streams.items() if current == role),
            None,
        )
        if descriptor is None:
            return
        self._provider_streams.pop(descriptor, None)
        try:
            self._selector.unregister(descriptor)
        except (KeyError, ValueError):
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass

    def _close_provider_descriptors(self) -> None:
        for role in tuple(self._provider_streams.values()):
            self._close_provider_stream(role)

    def _stop_admission(self) -> None:
        if self._server is None:
            return
        try:
            self._selector.unregister(self._server)
        except (KeyError, ValueError):
            pass
        self._server.close()
        self._server = None

    def _flush_final_responses(self) -> None:
        if not self._finalize_after_write:
            return
        deadline = time.monotonic() + CONTROL_FRAME_TIMEOUT_SECONDS
        while self._connections and time.monotonic() < deadline:
            for key, mask in self._selector.select(
                max(0.0, deadline - time.monotonic())
            ):
                if isinstance(key.data, _Connection) and mask & selectors.EVENT_WRITE:
                    self._write_connection(key.data, time.monotonic())


def _decode_launch_request(
    request: dict[str, Any],
) -> tuple[tuple[str, ...], str, dict[str, str], bytes]:
    if set(request) != {
        "arguments",
        "environment",
        "operation",
        "standard_input",
        "working_directory",
    }:
        raise ValueError("launch request has unexpected fields")
    arguments_value = request["arguments"]
    if (
        type(arguments_value) is not list
        or not arguments_value
        or any(type(value) is not str or not value for value in arguments_value)
    ):
        raise ValueError("launch arguments are malformed")
    working_directory_value = request["working_directory"]
    if (
        type(working_directory_value) is not str
        or not Path(working_directory_value).is_absolute()
    ):
        raise ValueError("launch working directory is malformed")
    environment_value = request["environment"]
    if type(environment_value) is not list:
        raise ValueError("launch environment is malformed")
    environment_pairs: list[tuple[str, str]] = []
    for pair in environment_value:
        if (
            type(pair) is not list
            or len(pair) != 2
            or type(pair[0]) is not str
            or not pair[0]
            or type(pair[1]) is not str
        ):
            raise ValueError("launch environment is malformed")
        environment_pairs.append((pair[0], pair[1]))
    environment = dict(environment_pairs)
    if len(environment) != len(environment_pairs):
        raise ValueError("launch environment names are duplicated")
    standard_input_value = request["standard_input"]
    if type(standard_input_value) is not str:
        raise ValueError("launch standard input is malformed")
    standard_input = base64.b64decode(standard_input_value, validate=True)
    if len(standard_input) > MAXIMUM_AGENT_PROCESS_INPUT_BYTES:
        raise ValueError("launch standard input exceeds its exact bound")
    return (
        tuple(arguments_value),
        working_directory_value,
        environment,
        standard_input,
    )


def _cgroup_populated(cgroup: Path) -> bool:
    events = (cgroup / "cgroup.events").read_text(encoding="ascii").splitlines()
    return "populated 1" in events


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="atelier2-agent-watchdog")
    parser.add_argument("--endpoint", type=Path, required=True)
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--owner-pipe", type=int, required=True)
    parser.add_argument("--attempt-lock", type=int, required=True)
    parser.add_argument("--grace", type=float, required=True)
    parsed = parser.parse_args(arguments)
    Watchdog(
        parsed.endpoint,
        parsed.cgroup,
        parsed.owner_pipe,
        parsed.attempt_lock,
        parsed.grace,
    ).serve()


if __name__ == "__main__":
    main()
