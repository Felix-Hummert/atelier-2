from __future__ import annotations

import base64
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from atelier2.adapters.agent_process_watchdog import (
    CONTROL_FRAME_TIMEOUT_SECONDS,
    MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES,
    MAXIMUM_AGENT_WAIT_RESPONSE_BYTES_V2,
    encode_control_frame,
)
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptState,
    AgentProcessOwnerId,
    WatchdogGenerationId,
)
from atelier2.contracts.agents import MAXIMUM_AGENT_OUTPUT_BYTES_V2
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.ports.agent_attempts import AgentAttemptStore
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    AgentProcessCompletion,
    AgentProcessInvocation,
    AgentProcessOwnerNotLocal,
)


@dataclass
class _OwnedWatchdog:
    owner: AgentProcessOwnerId
    generation: WatchdogGenerationId
    endpoint: Path
    cgroup: Path
    process: subprocess.Popen[bytes]
    owner_pipe: int
    launched: bool = False
    wait_completed: threading.Event = field(default_factory=threading.Event)


class AgentProcessSupervisor:
    """Live signal authority; durable state contains no PID or invocation."""

    def __init__(
        self,
        store: AgentAttemptStore,
        control_root: Path,
        cgroup_root: Path,
        *,
        grace_seconds: float,
        ready_timeout_seconds: float = 5.0,
    ) -> None:
        self._store = store
        self._control_root = control_root.resolve()
        self._cgroup_root = cgroup_root.resolve()
        self._grace_seconds = grace_seconds
        self._ready_timeout_seconds = ready_timeout_seconds
        self._owner = AgentProcessOwnerId(uuid.uuid4().hex)
        self._registry_lock = threading.Lock()
        self._attempt_locks: dict[AgentAttemptId, threading.Lock] = {}
        self._owned: dict[AgentAttemptId, _OwnedWatchdog | None] = {}
        if grace_seconds <= 0 or ready_timeout_seconds <= 0:
            raise ValueError("agent process bounds must be positive")

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        lock = self._attempt_lock(execution.attempt_id)
        with lock:
            existing = self._owned.get(execution.attempt_id)
            if existing is not None:
                return self._store.load(execution.attempt_id)
            durable = self._store.load(execution.attempt_id)
            if durable.state is not AgentAttemptState.PREPARED:
                return durable
            generation = WatchdogGenerationId(uuid.uuid4().hex)
            endpoint = self._control_root / f"{execution.attempt_id.value}.sock"
            cgroup = self._cgroup_root / (
                "atelier2-"
                + execution.attempt_id.value[:16]
                + "-"
                + generation.value[:8]
            )
            self._control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self._control_root, 0o700)
            cgroup.mkdir(mode=0o700)
            read_pipe, write_pipe = os.pipe()
            control_directory = os.open(
                self._control_root, os.O_RDONLY | os.O_DIRECTORY
            )
            try:
                process = subprocess.Popen(
                    (
                        sys.executable,
                        "-m",
                        "atelier2.adapters.agent_process_watchdog",
                        "--endpoint",
                        str(
                            Path("/proc/self/fd")
                            / str(control_directory)
                            / endpoint.name
                        ),
                        "--cgroup",
                        str(cgroup),
                        "--owner-pipe",
                        str(read_pipe),
                        "--grace",
                        str(self._grace_seconds),
                    ),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    pass_fds=(read_pipe, control_directory),
                )
            finally:
                os.close(control_directory)
            os.close(read_pipe)
            owned = _OwnedWatchdog(
                self._owner, generation, endpoint, cgroup, process, write_pipe
            )
            try:
                self._wait_ready(owned)
                durable = self._store.bind_watchdog(execution, self._owner, generation)
            except BaseException:
                os.close(write_pipe)
                process.kill()
                process.wait()
                endpoint.unlink(missing_ok=True)
                try:
                    cgroup.rmdir()
                except OSError:
                    pass
                raise
            self._owned[execution.attempt_id] = owned
            return durable

    def launch_and_wait(
        self, execution: AgentAttemptExecution, invocation: AgentProcessInvocation
    ) -> AgentProcessCompletion:
        launch_request = _launch_request(invocation)
        launch_frame = encode_control_frame(launch_request)
        if len(launch_frame) > MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES:
            raise ValueError(
                "encoded agent launch exceeds "
                f"{MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES} bytes"
            )
        lock = self._attempt_lock(execution.attempt_id)
        with lock:
            owned = self._owned.get(execution.attempt_id)
            durable = self._store.load(execution.attempt_id)
            if (
                owned is None
                or durable.state is not AgentAttemptState.LAUNCH_ARMED
                or durable.process_owner_id != owned.owner
                or durable.watchdog_generation_id != owned.generation
            ):
                raise AgentProcessOwnerNotLocal
            response = self._request_with_retry(
                owned.endpoint,
                launch_request,
                timeout_seconds=CONTROL_FRAME_TIMEOUT_SECONDS,
                maximum_response_bytes=4_096,
            )
            if response.get("type") == "STARTED":
                owned.launched = True
                self._store.observe_process(execution, owned.owner, owned.generation)
            elif response.get("type") == "TERMINAL_BEFORE_START":
                raise AgentProcessOwnerNotLocal
            else:
                raise RuntimeError("watchdog refused the exact launch")
        try:
            response = self._request_with_retry(
                owned.endpoint,
                {"operation": "WAIT"},
                timeout_seconds=None,
                maximum_response_bytes=MAXIMUM_AGENT_WAIT_RESPONSE_BYTES_V2,
            )
            return _completion_from_response(response)
        finally:
            owned.wait_completed.set()

    def cancel(
        self, attempt: AgentAttempt
    ) -> tuple[
        AgentAttemptCancellationDisposition,
        AgentProcessOwnerId,
        WatchdogGenerationId,
    ]:
        lock = self._attempt_lock(attempt.attempt_id)
        with lock:
            owned = self._owned.get(attempt.attempt_id)
            if (
                owned is None
                or attempt.process_owner_id != owned.owner
                or attempt.watchdog_generation_id != owned.generation
            ):
                raise AgentProcessOwnerNotLocal
        response = self._request_with_retry(
            owned.endpoint,
            {"operation": "CANCEL"},
            timeout_seconds=(self._grace_seconds * 2) + 2,
            maximum_response_bytes=4_096,
        )
        if response.get("type") == "RECOVERY_HANDOFF":
            raise AgentProcessOwnerNotLocal
        if response.get("type") != "CANCELLED":
            raise RuntimeError("watchdog did not acknowledge cancellation")
        disposition_value = response.get("disposition")
        if type(disposition_value) is not str:
            raise RuntimeError("watchdog cancellation response is malformed")
        disposition = AgentAttemptCancellationDisposition(disposition_value)
        if owned.launched and not owned.wait_completed.wait(
            timeout=max(1.0, self._grace_seconds + 1.0)
        ):
            raise RuntimeError("watchdog did not return the reaped process completion")
        return disposition, owned.owner, owned.generation

    def recover(
        self, attempt: AgentAttempt
    ) -> tuple[
        AgentAttemptCancellationDisposition,
        AgentProcessOwnerId,
        WatchdogGenerationId,
    ]:
        if attempt.process_owner_id is None or attempt.watchdog_generation_id is None:
            raise AgentProcessOwnerNotLocal
        lock = self._attempt_lock(attempt.attempt_id)
        with lock:
            if self._owned.get(attempt.attempt_id) is not None:
                raise AgentProcessOwnerNotLocal
            cgroup = self._cgroup_for(attempt)
            if not cgroup.is_dir():
                raise AgentProcessOwnerNotLocal
            _kill_cgroup_and_wait_empty(cgroup, self._ready_timeout_seconds)
        return (
            AgentAttemptCancellationDisposition.OWNER_LOST_AFTER_PARENT_DEATH,
            attempt.process_owner_id,
            attempt.watchdog_generation_id,
        )

    def release(self, attempt: AgentAttempt) -> None:
        if (
            attempt.state
            not in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED}
            or attempt.process_phase is not AgentAttemptProcessPhase.CLEANUP_ATTESTED
            or attempt.cancellation is None
            or attempt.cancellation.disposition is None
        ):
            raise RuntimeError(
                "only durably attested cancellation cleanup can release its witness"
            )
        if attempt.process_owner_id is None or attempt.watchdog_generation_id is None:
            if (
                attempt.cancellation.disposition
                is AgentAttemptCancellationDisposition.NEVER_LAUNCHED
            ):
                return
            raise RuntimeError("cleanup attestation has no watchdog identity")
        lock = self._attempt_lock(attempt.attempt_id)
        with lock:
            owned = self._owned.get(attempt.attempt_id)
            if owned is not None:
                if (
                    owned.owner != attempt.process_owner_id
                    or owned.generation != attempt.watchdog_generation_id
                ):
                    raise AgentProcessOwnerNotLocal
                self._finalize_owned(owned)
                self._owned.pop(attempt.attempt_id, None)
                return
            cgroup = self._cgroup_for(attempt)
            if cgroup.is_dir():
                _kill_cgroup_and_wait_empty(cgroup, self._ready_timeout_seconds)
                cgroup.rmdir()
            (self._control_root / f"{attempt.attempt_id.value}.sock").unlink(
                missing_ok=True
            )

    def finalize(self, execution: AgentAttemptExecution) -> None:
        lock = self._attempt_lock(execution.attempt_id)
        with lock:
            owned = self._owned.get(execution.attempt_id)
            if owned is None:
                return
            terminal = self._store.load(execution.attempt_id)
            if terminal.state not in {
                AgentAttemptState.SUCCEEDED,
                AgentAttemptState.FAILED,
            }:
                raise RuntimeError("only a terminal attempt can release its watchdog")
            self._owned[execution.attempt_id] = None
        try:
            self._finalize_owned(owned)
        except BaseException:
            with lock:
                if (
                    execution.attempt_id in self._owned
                    and self._owned[execution.attempt_id] is None
                ):
                    self._owned[execution.attempt_id] = owned
            raise

    def close(self) -> None:
        with self._registry_lock:
            owned = tuple(value for value in self._owned.values() if value is not None)
            self._owned.clear()
        for handle in owned:
            self._detach_owned(handle)

    def _attempt_lock(self, attempt_id: AgentAttemptId) -> threading.Lock:
        with self._registry_lock:
            return self._attempt_locks.setdefault(attempt_id, threading.Lock())

    def _cgroup_for(self, attempt: AgentAttempt) -> Path:
        generation = attempt.watchdog_generation_id
        if generation is None:
            raise AgentProcessOwnerNotLocal
        return self._cgroup_root / (
            "atelier2-" + attempt.attempt_id.value[:16] + "-" + generation.value[:8]
        )

    def _wait_ready(self, owned: _OwnedWatchdog) -> None:
        if owned.process.stdout is None:
            raise RuntimeError("watchdog ready channel is absent")
        ready, _write, _error = select.select(
            [owned.process.stdout], [], [], self._ready_timeout_seconds
        )
        if not ready or owned.process.stdout.readline() != b"READY\n":
            detail = b""
            if owned.process.poll() is not None and owned.process.stderr is not None:
                detail = owned.process.stderr.read(2_001)
            raise RuntimeError(
                "watchdog did not prove readiness: "
                + detail.decode("utf-8", errors="replace")[:2_000]
            )
        if (
            not owned.endpoint.is_socket()
            or not (owned.cgroup / "cgroup.kill").is_file()
            or _cgroup_populated(owned.cgroup)
        ):
            raise RuntimeError("watchdog readiness attestation disagrees")

    def _request_with_retry(
        self,
        endpoint: Path,
        request: dict[str, object],
        *,
        timeout_seconds: float | None,
        maximum_response_bytes: int,
    ) -> dict[str, object]:
        last_error: BaseException | None = None
        for retry in range(2):
            if retry:
                time.sleep(CONTROL_FRAME_TIMEOUT_SECONDS)
            try:
                response = self._request(
                    endpoint, request, timeout_seconds=timeout_seconds
                )
                if len(encode_control_frame(response)) > maximum_response_bytes:
                    raise RuntimeError("watchdog response exceeds its exact bound")
                if response.get("type") in {
                    "BUSY",
                    "CONTROL_FRAME_TIMEOUT",
                    "FRAME_TOO_LARGE",
                    "MALFORMED",
                    "LAUNCH_MISMATCH",
                }:
                    raise RuntimeError("watchdog refused the exact control frame")
                return response
            except (OSError, TimeoutError, RuntimeError, ValueError) as error:
                last_error = error
        raise AgentProcessOwnerNotLocal from last_error

    @staticmethod
    def _request(
        endpoint: Path,
        request: dict[str, object],
        *,
        timeout_seconds: float | None = 30,
    ) -> dict[str, object]:
        frame = encode_control_frame(request)
        maximum_response_bytes = (
            MAXIMUM_AGENT_WAIT_RESPONSE_BYTES_V2
            if request.get("operation") == "WAIT"
            else 4_096
        )
        control_directory = os.open(endpoint.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            short_endpoint = (
                Path("/proc/self/fd") / str(control_directory) / endpoint.name
            )
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(CONTROL_FRAME_TIMEOUT_SECONDS)
                connection.connect(str(short_endpoint))
                connection.sendall(frame)
                connection.shutdown(socket.SHUT_WR)
                connection.settimeout(timeout_seconds)
                response_bytes = bytearray()
                while True:
                    chunk = connection.recv(
                        min(65_536, maximum_response_bytes + 1 - len(response_bytes))
                    )
                    if not chunk:
                        break
                    response_bytes.extend(chunk)
                    if len(response_bytes) > maximum_response_bytes:
                        raise RuntimeError("watchdog response exceeds its exact bound")
        finally:
            os.close(control_directory)
        try:
            response = json.loads(bytes(response_bytes).decode("ascii"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RuntimeError("watchdog response is malformed") from error
        if not isinstance(response, dict) or encode_control_frame(response) != bytes(
            response_bytes
        ):
            raise RuntimeError("watchdog response is noncanonical")
        return response

    def _finalize_owned(self, owned: _OwnedWatchdog) -> None:
        response = self._request_with_retry(
            owned.endpoint,
            {"operation": "FINALIZE"},
            timeout_seconds=CONTROL_FRAME_TIMEOUT_SECONDS,
            maximum_response_bytes=4_096,
        )
        if response.get("type") != "FINALIZE_ACCEPTED":
            raise RuntimeError("watchdog refused terminal finalization")
        self._close_owned(owned)

    def _close_owned(self, owned: _OwnedWatchdog) -> None:
        try:
            os.close(owned.owner_pipe)
        except OSError:
            pass
        try:
            owned.process.wait(timeout=self._ready_timeout_seconds)
        except subprocess.TimeoutExpired:
            owned.process.terminate()
            try:
                owned.process.wait(timeout=self._ready_timeout_seconds)
            except subprocess.TimeoutExpired:
                owned.process.kill()
                owned.process.wait()
        _close_watchdog_pipes(owned.process)
        owned.endpoint.unlink(missing_ok=True)
        if owned.cgroup.is_dir():
            owned.cgroup.rmdir()

    def _detach_owned(self, owned: _OwnedWatchdog) -> None:
        """Stop this owner while retaining evidence for durable recovery."""

        try:
            os.close(owned.owner_pipe)
        except OSError:
            pass
        try:
            owned.process.wait(timeout=(self._grace_seconds * 2) + 2)
        except subprocess.TimeoutExpired:
            _kill_cgroup_and_wait_empty(owned.cgroup, self._ready_timeout_seconds)
            owned.process.kill()
            owned.process.wait()
        _close_watchdog_pipes(owned.process)


def _close_watchdog_pipes(process: subprocess.Popen[bytes]) -> None:
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


def _launch_request(invocation: AgentProcessInvocation) -> dict[str, object]:
    return {
        "arguments": invocation.arguments,
        "environment": invocation.environment,
        "operation": "LAUNCH",
        "standard_input": base64.b64encode(invocation.standard_input).decode("ascii"),
        "working_directory": str(invocation.working_directory),
    }


def _completion_from_response(response: dict[str, object]) -> AgentProcessCompletion:
    response_type = response.get("type")
    if response_type in {"RECOVERY_HANDOFF", "STOPPED"}:
        raise AgentProcessOwnerNotLocal
    if response_type != "COMPLETED":
        raise RuntimeError("watchdog did not return a process completion")
    return_code = response.get("return_code")
    standard_output_value = response.get("standard_output")
    standard_error_value = response.get("standard_error")
    if (
        type(return_code) is not int
        or type(standard_output_value) is not str
        or type(standard_error_value) is not str
    ):
        raise RuntimeError("watchdog process completion is malformed")
    try:
        standard_output = base64.b64decode(standard_output_value, validate=True)
        standard_error = base64.b64decode(standard_error_value, validate=True)
    except ValueError as error:
        raise RuntimeError("watchdog process completion is malformed") from error
    if (
        len(standard_output) > MAXIMUM_AGENT_OUTPUT_BYTES_V2
        or len(standard_error) > MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
    ):
        raise RuntimeError("watchdog process completion exceeds its exact bound")
    return AgentProcessCompletion(return_code, standard_output, standard_error)


def _cgroup_populated(cgroup: Path) -> bool:
    events = (cgroup / "cgroup.events").read_text(encoding="ascii").splitlines()
    return "populated 1" in events


def _kill_cgroup_and_wait_empty(cgroup: Path, timeout_seconds: float) -> None:
    (cgroup / "cgroup.kill").write_text("1", encoding="ascii")
    deadline = time.monotonic() + timeout_seconds
    while _cgroup_populated(cgroup) and time.monotonic() < deadline:
        time.sleep(0.01)
    if _cgroup_populated(cgroup):
        raise RuntimeError("agent cgroup did not become empty in bounds")


def delegated_cgroup_root() -> Path:
    relative = next(
        (
            line.removeprefix("0::/").strip()
            for line in Path("/proc/self/cgroup")
            .read_text(encoding="ascii")
            .splitlines()
            if line.startswith("0::/")
        ),
        None,
    )
    if relative is None:
        raise RuntimeError("agent supervision requires cgroup v2")
    root = (Path("/sys/fs/cgroup") / relative).resolve()
    if not (root / "cgroup.procs").is_file() or not os.access(root, os.W_OK):
        raise RuntimeError("agent supervision requires a writable delegated cgroup")
    return root
