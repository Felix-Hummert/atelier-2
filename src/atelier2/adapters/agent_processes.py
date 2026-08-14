from __future__ import annotations

import base64
import fcntl
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
import uuid
import weakref
from dataclasses import dataclass
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
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.ports.agent_attempts import (
    AgentAttemptExecutionOutcome,
    AgentAttemptFailed,
    AgentAttemptPossiblyRan,
    AgentAttemptStore,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_executions import (
    AgentProcessExited,
    AgentProcessInvocation,
    AgentProcessOutcome,
    AgentProcessOutputLimitExceeded,
    AgentProcessOwnerNotLocal,
    AgentProcessStopped,
    AgentProcessSupervisionFailed,
)

_SMALL_CONTROL_RESPONSE_BYTES = 4_096


class _ControlBoundaryFailure(RuntimeError):
    pass


@dataclass
class _OwnedWatchdog:
    owner: AgentProcessOwnerId
    generation: WatchdogGenerationId
    endpoint: Path
    cgroup: Path
    attempt_lock_path: Path
    attempt_lock: int
    launch_frame: bytes
    process: subprocess.Popen[bytes] | None = None
    owner_pipe: int | None = None
    recovery_only: bool = False
    process_outcome_claimed: bool = False


class AgentProcessSupervisor:
    """Live authority for one exact attempt generation and its witnesses."""

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
        self._attempt_locks: weakref.WeakValueDictionary[
            AgentAttemptId, threading.RLock
        ] = weakref.WeakValueDictionary()
        self._owned: dict[AgentAttemptId, _OwnedWatchdog] = {}
        self._registry_file: int | None = None
        if grace_seconds <= 0 or ready_timeout_seconds <= 0:
            raise ValueError("agent process bounds must be positive")

    def prepare(
        self, execution: AgentAttemptExecution, invocation: AgentProcessInvocation
    ) -> AgentAttempt:
        launch_frame = _launch_frame(invocation)
        if len(launch_frame) > MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES:
            raise ValueError(
                "encoded agent launch exceeds "
                f"{MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES} bytes"
            )
        lock = self._attempt_lock(execution.attempt_id)
        with lock:
            existing = self._owned.get(execution.attempt_id)
            if existing is not None:
                if existing.launch_frame != launch_frame:
                    raise RuntimeError("local watchdog invocation changed")
                return self._store.load(execution.attempt_id)
            durable = self._store.load(execution.attempt_id)
            if (
                durable.state is AgentAttemptState.PREPARED
                and durable.process_phase is AgentAttemptProcessPhase.NONE
            ):
                return self._prepare_new(execution, durable, launch_frame)
            if (
                durable.state is AgentAttemptState.PREPARED
                and durable.process_phase is AgentAttemptProcessPhase.WATCHDOG_READY
            ):
                return self._resume_prepared(execution, durable, launch_frame)
            self._collect_terminal_lock_if_safe(durable)
            return durable

    def launch_and_wait(
        self, execution: AgentAttemptExecution
    ) -> AgentProcessOutcome | AgentAttemptExecutionOutcome:
        lock = self._attempt_lock(execution.attempt_id)
        with lock:
            owned = self._owned.get(execution.attempt_id)
            durable = self._store.load(execution.attempt_id)
            if durable.state in {
                AgentAttemptState.SUCCEEDED,
                AgentAttemptState.FAILED,
            }:
                outcome = self._terminal_execution_outcome(execution)
                self._finalize_terminal(execution.attempt_id, durable)
                return outcome
            if durable.state in {
                AgentAttemptState.CANCELLED,
                AgentAttemptState.INTERRUPTED,
            }:
                if (
                    durable.process_phase is AgentAttemptProcessPhase.CLEANUP_ATTESTED
                    and durable.cancellation is not None
                    and durable.cancellation.disposition is not None
                ):
                    self._finalize_terminal(execution.attempt_id, durable)
                return AgentAttemptPossiblyRan(durable)
            if owned is None:
                return AgentAttemptPossiblyRan(durable)
            if (
                durable.process_owner_id != owned.owner
                or durable.watchdog_generation_id != owned.generation
            ):
                raise AgentProcessOwnerNotLocal
            if durable.state not in {
                AgentAttemptState.LAUNCH_ARMED,
                AgentAttemptState.CANCEL_REQUESTED,
            }:
                raise AgentProcessOwnerNotLocal
            returns_process_outcome = not owned.process_outcome_claimed
            owned.process_outcome_claimed = True
            if (
                durable.state is AgentAttemptState.LAUNCH_ARMED
                and durable.process_phase is AgentAttemptProcessPhase.LAUNCH_AUTHORIZED
            ):
                try:
                    launch = self._request_with_retry(
                        owned,
                        owned.launch_frame,
                        timeout_seconds=CONTROL_FRAME_TIMEOUT_SECONDS,
                        maximum_response_bytes=_SMALL_CONTROL_RESPONSE_BYTES,
                    )
                except _ControlBoundaryFailure:
                    return self._outcome_after_local_boundary_failure(execution, owned)
                response_type = launch.get("type")
                if response_type == "STARTED":
                    self._store.observe_process(
                        execution, owned.owner, owned.generation
                    )
                elif response_type == "TERMINAL_BEFORE_START":
                    if returns_process_outcome:
                        return _terminal_before_start_outcome(launch)
                    return self._outcome_after_reap(execution)
                else:
                    self._reap_and_forget(execution.attempt_id, owned)
                    return self._outcome_after_reap(execution)
            elif (
                durable.state is AgentAttemptState.LAUNCH_ARMED
                and durable.process_phase
                is not AgentAttemptProcessPhase.PROCESS_OBSERVED
            ):
                raise AgentProcessOwnerNotLocal
        wait_frame = encode_control_frame({"operation": "WAIT"})
        try:
            response = self._request_with_retry(
                owned,
                wait_frame,
                timeout_seconds=None,
                maximum_response_bytes=MAXIMUM_AGENT_WAIT_RESPONSE_BYTES_V2,
            )
        except _ControlBoundaryFailure:
            return self._outcome_after_local_boundary_failure(execution, owned)
        if response.get("type") == "RECOVERY_HANDOFF":
            self._reap_and_forget(execution.attempt_id, owned)
            return self._outcome_after_reap(execution)
        if not returns_process_outcome:
            return self._outcome_after_reap(execution)
        return _process_outcome(response)

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
                or owned.recovery_only
            ):
                raise AgentProcessOwnerNotLocal
        try:
            response = self._request_with_retry(
                owned,
                encode_control_frame({"operation": "CANCEL"}),
                timeout_seconds=(self._grace_seconds * 2) + 2,
                maximum_response_bytes=_SMALL_CONTROL_RESPONSE_BYTES,
            )
        except _ControlBoundaryFailure as error:
            self._close_attempt_lock_share(owned)
            self._forget_owned(attempt.attempt_id, owned)
            raise AgentProcessOwnerNotLocal from error
        if response.get("type") == "RECOVERY_HANDOFF":
            self._reap_and_forget(attempt.attempt_id, owned)
            raise AgentProcessOwnerNotLocal
        if response.get("type") != "CANCELLED":
            self._reap_and_forget(attempt.attempt_id, owned)
            raise AgentProcessOwnerNotLocal
        try:
            disposition = AgentAttemptCancellationDisposition(
                str(response["disposition"])
            )
        except (KeyError, ValueError) as error:
            self._reap_and_forget(attempt.attempt_id, owned)
            raise AgentProcessOwnerNotLocal from error
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
            if attempt.attempt_id in self._owned:
                raise AgentProcessOwnerNotLocal
            attempt_lock_path = self._lock_path(attempt.attempt_id)
            attempt_lock = self._open_existing_attempt_lock(attempt_lock_path)
            cgroup = self._cgroup_for(attempt.attempt_id)
            endpoint = self._endpoint_for(attempt.attempt_id)
            try:
                if not cgroup.is_dir() or not endpoint.is_socket():
                    raise AgentProcessOwnerNotLocal
                if _cgroup_populated(cgroup):
                    _kill_cgroup_and_wait_empty(cgroup, self._ready_timeout_seconds)
            except BaseException:
                os.close(attempt_lock)
                raise
            self._owned[attempt.attempt_id] = _OwnedWatchdog(
                attempt.process_owner_id,
                attempt.watchdog_generation_id,
                endpoint,
                cgroup,
                attempt_lock_path,
                attempt_lock,
                b"",
                recovery_only=True,
            )
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
                self._collect_terminal_lock_if_safe(attempt)
                return
            raise RuntimeError("cleanup attestation has no watchdog identity")
        self._finalize_terminal(attempt.attempt_id, attempt)

    def finalize(self, execution: AgentAttemptExecution) -> None:
        terminal = self._store.load(execution.attempt_id)
        if terminal.state not in {
            AgentAttemptState.SUCCEEDED,
            AgentAttemptState.FAILED,
        }:
            raise RuntimeError("only a terminal attempt can finalize its watchdog")
        self._finalize_terminal(execution.attempt_id, terminal)

    def close(self) -> None:
        with self._registry_lock:
            owned = tuple(self._owned.items())
            self._owned.clear()
        for attempt_id, handle in owned:
            with self._attempt_lock(attempt_id):
                if handle.recovery_only:
                    self._close_attempt_lock_share(handle)
                else:
                    self._reap_local_watchdog(handle)
                    self._close_attempt_lock_share(handle)
        with self._registry_lock:
            if self._registry_file is not None:
                os.close(self._registry_file)
                self._registry_file = None

    def _prepare_new(
        self,
        execution: AgentAttemptExecution,
        durable: AgentAttempt,
        launch_frame: bytes,
    ) -> AgentAttempt:
        self._ensure_control_root()
        attempt_lock_path = self._lock_path(execution.attempt_id)
        attempt_lock = self._open_or_reuse_prepared_lock(attempt_lock_path, durable)
        owner = self._owner
        generation = WatchdogGenerationId(uuid.uuid4().hex)
        owned = _OwnedWatchdog(
            owner,
            generation,
            self._endpoint_for(execution.attempt_id),
            self._cgroup_for(execution.attempt_id),
            attempt_lock_path,
            attempt_lock,
            launch_frame,
        )
        try:
            self._prepare_namespace(owned)
            self._start_watchdog(owned)
            updated = self._store.bind_watchdog(execution, owner, generation)
        except BaseException:
            self._discard_failed_prebind(owned)
            raise
        self._owned[execution.attempt_id] = owned
        return updated

    def _discard_failed_prebind(self, owned: _OwnedWatchdog) -> None:
        self._reap_local_watchdog(owned)
        if owned.cgroup.is_dir() and _cgroup_populated(owned.cgroup):
            _close_descriptor(owned.attempt_lock)
            return
        try:
            self._cleanup_namespace_without_signal(owned)
            self._unlink_attempt_lock(owned, durable_required=True)
        except (AgentProcessOwnerNotLocal, OSError, RuntimeError):
            _close_descriptor(owned.attempt_lock)

    def _resume_prepared(
        self,
        execution: AgentAttemptExecution,
        durable: AgentAttempt,
        launch_frame: bytes,
    ) -> AgentAttempt:
        owner = durable.process_owner_id
        generation = durable.watchdog_generation_id
        assert owner is not None and generation is not None
        self._ensure_control_root()
        attempt_lock_path = self._lock_path(execution.attempt_id)
        attempt_lock = self._open_existing_attempt_lock(attempt_lock_path)
        owned = _OwnedWatchdog(
            owner,
            generation,
            self._endpoint_for(execution.attempt_id),
            self._cgroup_for(execution.attempt_id),
            attempt_lock_path,
            attempt_lock,
            launch_frame,
        )
        try:
            self._cleanup_namespace_without_signal(owned)
            self._prepare_namespace(owned)
            self._start_watchdog(owned)
            rebound = self._store.bind_watchdog(execution, owner, generation)
        except BaseException:
            self._reap_local_watchdog(owned)
            try:
                self._cleanup_namespace_without_signal(owned)
            finally:
                _close_descriptor(owned.attempt_lock)
            raise
        self._owned[execution.attempt_id] = owned
        return rebound

    def _prepare_namespace(self, owned: _OwnedWatchdog) -> None:
        if owned.endpoint.exists():
            raise AgentProcessOwnerNotLocal
        if owned.cgroup.exists():
            if _cgroup_populated(owned.cgroup):
                raise AgentProcessOwnerNotLocal
            owned.cgroup.rmdir()
        owned.cgroup.mkdir(mode=0o700)
        if _cgroup_populated(owned.cgroup):
            raise RuntimeError("new agent cgroup is unexpectedly populated")

    def _start_watchdog(self, owned: _OwnedWatchdog) -> None:
        read_pipe, write_pipe = os.pipe()
        control_directory = os.open(self._control_root, os.O_RDONLY | os.O_DIRECTORY)
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
                        / owned.endpoint.name
                    ),
                    "--cgroup",
                    str(owned.cgroup),
                    "--owner-pipe",
                    str(read_pipe),
                    "--attempt-lock",
                    str(owned.attempt_lock),
                    "--grace",
                    str(self._grace_seconds),
                ),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(
                    read_pipe,
                    control_directory,
                    owned.attempt_lock,
                ),
            )
        except BaseException:
            os.close(write_pipe)
            raise
        finally:
            os.close(control_directory)
            os.close(read_pipe)
        owned.process = process
        owned.owner_pipe = write_pipe
        self._wait_ready(owned)

    def _wait_ready(self, owned: _OwnedWatchdog) -> None:
        process = owned.process
        if process is None or process.stdout is None:
            raise RuntimeError("watchdog ready channel is absent")
        ready, _write, _error = select.select(
            [process.stdout], [], [], self._ready_timeout_seconds
        )
        if not ready or process.stdout.readline() != b"READY\n":
            detail = b""
            if process.poll() is not None and process.stderr is not None:
                detail = process.stderr.read(2_001)
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
        owned: _OwnedWatchdog,
        frame: bytes,
        *,
        timeout_seconds: float | None,
        maximum_response_bytes: int,
    ) -> dict[str, object]:
        last_error: BaseException | None = None
        for retry in range(2):
            if retry:
                time.sleep(CONTROL_FRAME_TIMEOUT_SECONDS)
            try:
                response = self._request_once(
                    owned.endpoint,
                    frame,
                    timeout_seconds=timeout_seconds,
                    maximum_response_bytes=maximum_response_bytes,
                )
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
        self._reap_local_watchdog(owned)
        raise _ControlBoundaryFailure from last_error

    @staticmethod
    def _request_once(
        endpoint: Path,
        frame: bytes,
        *,
        timeout_seconds: float | None,
        maximum_response_bytes: int,
    ) -> dict[str, object]:
        control_directory = os.open(endpoint.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            short_endpoint = (
                Path("/proc/self/fd") / str(control_directory) / endpoint.name
            )
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout_seconds)
                connection.connect(str(short_endpoint))
                connection.sendall(frame)
                connection.shutdown(socket.SHUT_WR)
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

    def _outcome_after_local_boundary_failure(
        self, execution: AgentAttemptExecution, owned: _OwnedWatchdog
    ) -> AgentAttemptExecutionOutcome:
        self._close_attempt_lock_share(owned)
        self._forget_owned(execution.attempt_id, owned)
        return self._outcome_after_reap(execution)

    def _outcome_after_reap(
        self, execution: AgentAttemptExecution
    ) -> AgentAttemptExecutionOutcome:
        durable = self._store.load(execution.attempt_id)
        if durable.state in {
            AgentAttemptState.SUCCEEDED,
            AgentAttemptState.FAILED,
        }:
            return self._terminal_execution_outcome(execution)
        return AgentAttemptPossiblyRan(durable)

    def _terminal_execution_outcome(
        self, execution: AgentAttemptExecution
    ) -> AgentAttemptSucceeded | AgentAttemptFailed:
        outcome = self._store.claim(execution)
        if isinstance(outcome, (AgentAttemptSucceeded, AgentAttemptFailed)):
            return outcome
        raise RuntimeError("durable terminal attempt has no exact execution outcome")

    def _finalize_terminal(
        self, attempt_id: AgentAttemptId, terminal: AgentAttempt
    ) -> None:
        lock = self._attempt_lock(attempt_id)
        with lock:
            owned = self._owned.get(attempt_id)
            if owned is None:
                if not self._collect_terminal_lock_if_safe(terminal):
                    raise AgentProcessOwnerNotLocal
                return
            if terminal.process_owner_id is not None and (
                terminal.process_owner_id != owned.owner
                or terminal.watchdog_generation_id != owned.generation
            ):
                raise AgentProcessOwnerNotLocal
            if not owned.recovery_only:
                try:
                    response = self._request_with_retry(
                        owned,
                        encode_control_frame({"operation": "FINALIZE"}),
                        timeout_seconds=CONTROL_FRAME_TIMEOUT_SECONDS,
                        maximum_response_bytes=_SMALL_CONTROL_RESPONSE_BYTES,
                    )
                except _ControlBoundaryFailure as error:
                    self._close_attempt_lock_share(owned)
                    self._forget_owned(attempt_id, owned)
                    raise AgentProcessOwnerNotLocal from error
                if response.get("type") != "FINALIZE_ACCEPTED":
                    self._reap_and_forget(attempt_id, owned)
                    raise AgentProcessOwnerNotLocal
                try:
                    self._wait_watchdog_exit(owned)
                except AgentProcessOwnerNotLocal:
                    self._close_attempt_lock_share(owned)
                    self._forget_owned(attempt_id, owned)
                    raise
            self._cleanup_namespace_without_signal(owned)
            self._unlink_attempt_lock(owned, durable_required=True)
            self._forget_owned(attempt_id, owned)

    def _reap_and_forget(
        self, attempt_id: AgentAttemptId, owned: _OwnedWatchdog
    ) -> None:
        self._reap_local_watchdog(owned)
        self._close_attempt_lock_share(owned)
        self._forget_owned(attempt_id, owned)

    @staticmethod
    def _close_attempt_lock_share(owned: _OwnedWatchdog) -> None:
        if owned.attempt_lock >= 0:
            _close_descriptor(owned.attempt_lock)
            owned.attempt_lock = -1

    def _reap_local_watchdog(self, owned: _OwnedWatchdog) -> None:
        if owned.owner_pipe is not None:
            try:
                os.close(owned.owner_pipe)
            except OSError:
                pass
            owned.owner_pipe = None
        process = owned.process
        if process is None:
            return
        try:
            process.wait(timeout=self._ready_timeout_seconds)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=self._ready_timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        finally:
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
            owned.process = None

    def _wait_watchdog_exit(self, owned: _OwnedWatchdog) -> None:
        process = owned.process
        if process is None:
            return
        try:
            process.wait(timeout=self._ready_timeout_seconds)
        except subprocess.TimeoutExpired:
            self._reap_local_watchdog(owned)
            raise AgentProcessOwnerNotLocal from None
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()
        if owned.owner_pipe is not None:
            try:
                os.close(owned.owner_pipe)
            except OSError:
                pass
            owned.owner_pipe = None
        owned.process = None

    def _cleanup_namespace_without_signal(self, owned: _OwnedWatchdog) -> None:
        if owned.process is not None and owned.process.poll() is None:
            raise AgentProcessOwnerNotLocal
        if owned.cgroup.is_dir() and _cgroup_populated(owned.cgroup):
            raise AgentProcessOwnerNotLocal
        owned.endpoint.unlink(missing_ok=True)
        if owned.cgroup.is_dir():
            owned.cgroup.rmdir()

    def _collect_terminal_lock_if_safe(self, durable: AgentAttempt) -> bool:
        if durable.state not in {
            AgentAttemptState.CANCELLED,
            AgentAttemptState.INTERRUPTED,
            AgentAttemptState.SUCCEEDED,
            AgentAttemptState.FAILED,
        }:
            return False
        lock_path = self._lock_path(durable.attempt_id)
        try:
            lock_fd = self._open_existing_attempt_lock(lock_path)
        except AgentProcessOwnerNotLocal:
            return (
                not self._endpoint_for(durable.attempt_id).exists()
                and not self._cgroup_for(durable.attempt_id).exists()
            )
        owned = _OwnedWatchdog(
            durable.process_owner_id or self._owner,
            durable.watchdog_generation_id or WatchdogGenerationId("terminal"),
            self._endpoint_for(durable.attempt_id),
            self._cgroup_for(durable.attempt_id),
            lock_path,
            lock_fd,
            b"",
            recovery_only=True,
        )
        try:
            self._cleanup_namespace_without_signal(owned)
            self._unlink_attempt_lock(owned, durable_required=True)
        except AgentProcessOwnerNotLocal:
            os.close(lock_fd)
            return False
        return True

    def _open_or_reuse_prepared_lock(self, path: Path, durable: AgentAttempt) -> int:
        descriptor = self._open_and_lock(path, create=True)
        current = self._store.load(durable.attempt_id)
        if (
            current.state is not AgentAttemptState.PREPARED
            or current.process_phase is not AgentAttemptProcessPhase.NONE
        ):
            late_lock = _OwnedWatchdog(
                current.process_owner_id or self._owner,
                current.watchdog_generation_id or WatchdogGenerationId("late-lock"),
                self._endpoint_for(current.attempt_id),
                self._cgroup_for(current.attempt_id),
                path,
                descriptor,
                b"",
                recovery_only=True,
            )
            if current.state in {
                AgentAttemptState.CANCELLED,
                AgentAttemptState.INTERRUPTED,
                AgentAttemptState.SUCCEEDED,
                AgentAttemptState.FAILED,
            }:
                try:
                    self._cleanup_namespace_without_signal(late_lock)
                    self._unlink_attempt_lock(late_lock, durable_required=True)
                except (AgentProcessOwnerNotLocal, OSError, RuntimeError):
                    self._close_attempt_lock_share(late_lock)
            else:
                self._close_attempt_lock_share(late_lock)
            raise AgentProcessOwnerNotLocal
        return descriptor

    def _open_existing_attempt_lock(self, path: Path) -> int:
        return self._open_and_lock(path, create=False)

    def _open_and_lock(self, path: Path, *, create: bool) -> int:
        deadline = time.monotonic() + self._ready_timeout_seconds
        while True:
            with self._registry_flock():
                try:
                    descriptor = os.open(
                        path,
                        os.O_RDWR | (os.O_CREAT if create else 0),
                        0o600,
                    )
                except FileNotFoundError as error:
                    raise AgentProcessOwnerNotLocal from error
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(descriptor)
                else:
                    descriptor_stat = os.fstat(descriptor)
                    try:
                        path_stat = path.stat()
                    except FileNotFoundError:
                        os.close(descriptor)
                    else:
                        if (descriptor_stat.st_dev, descriptor_stat.st_ino) == (
                            path_stat.st_dev,
                            path_stat.st_ino,
                        ):
                            return descriptor
                        os.close(descriptor)
            if time.monotonic() >= deadline:
                raise AgentProcessOwnerNotLocal from None
            time.sleep(0.01)

    def _unlink_attempt_lock(
        self, owned: _OwnedWatchdog, *, durable_required: bool
    ) -> None:
        with self._registry_flock():
            try:
                path_stat = owned.attempt_lock_path.stat()
                descriptor_stat = os.fstat(owned.attempt_lock)
            except FileNotFoundError:
                self._close_attempt_lock_share(owned)
                return
            if (path_stat.st_dev, path_stat.st_ino) != (
                descriptor_stat.st_dev,
                descriptor_stat.st_ino,
            ):
                self._close_attempt_lock_share(owned)
                raise AgentProcessOwnerNotLocal
            if durable_required:
                durable = self._store.load(AgentAttemptId(owned.attempt_lock_path.stem))
                if durable.state not in {
                    AgentAttemptState.CANCELLED,
                    AgentAttemptState.INTERRUPTED,
                    AgentAttemptState.SUCCEEDED,
                    AgentAttemptState.FAILED,
                } and not (
                    durable.state is AgentAttemptState.PREPARED
                    and durable.process_phase is AgentAttemptProcessPhase.NONE
                ):
                    raise AgentProcessOwnerNotLocal
            owned.attempt_lock_path.unlink()
        self._close_attempt_lock_share(owned)

    def _ensure_control_root(self) -> None:
        self._control_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self._control_root, 0o700)

    class _RegistryFlock:
        def __init__(self, supervisor: AgentProcessSupervisor) -> None:
            self._supervisor = supervisor
            self._descriptor: int | None = None

        def __enter__(self) -> None:
            supervisor = self._supervisor
            supervisor._ensure_control_root()
            supervisor._registry_lock.acquire()
            try:
                if supervisor._registry_file is None:
                    supervisor._registry_file = os.open(
                        supervisor._control_root / ".registry.lock",
                        os.O_RDWR | os.O_CREAT,
                        0o600,
                    )
                self._descriptor = supervisor._registry_file
                fcntl.flock(self._descriptor, fcntl.LOCK_EX)
            except BaseException:
                supervisor._registry_lock.release()
                raise

        def __exit__(self, *_arguments: object) -> None:
            assert self._descriptor is not None
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            self._supervisor._registry_lock.release()

    def _registry_flock(self) -> _RegistryFlock:
        return self._RegistryFlock(self)

    def _forget_owned(self, attempt_id: AgentAttemptId, owned: _OwnedWatchdog) -> None:
        lock = self._attempt_lock(attempt_id)
        with lock:
            if self._owned.get(attempt_id) is owned:
                self._owned.pop(attempt_id, None)

    def _attempt_lock(self, attempt_id: AgentAttemptId) -> threading.RLock:
        with self._registry_lock:
            return self._attempt_locks.setdefault(attempt_id, threading.RLock())

    def _endpoint_for(self, attempt_id: AgentAttemptId) -> Path:
        return self._control_root / f"{attempt_id.value}.sock"

    def _lock_path(self, attempt_id: AgentAttemptId) -> Path:
        return self._control_root / f"{attempt_id.value}.lock"

    def _cgroup_for(self, attempt_id: AgentAttemptId) -> Path:
        return self._cgroup_root / f"atelier2-{attempt_id.value}"


def _close_descriptor(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _launch_frame(invocation: AgentProcessInvocation) -> bytes:
    return encode_control_frame(
        {
            "arguments": invocation.arguments,
            "environment": invocation.environment,
            "operation": "LAUNCH",
            "standard_input": base64.b64encode(invocation.standard_input).decode(
                "ascii"
            ),
            "working_directory": str(invocation.working_directory),
        }
    )


def _terminal_before_start_outcome(
    response: dict[str, object],
) -> AgentProcessOutcome:
    outcome = response.get("outcome")
    if outcome == "SUPERVISION_FAILED":
        return AgentProcessSupervisionFailed()
    if outcome == "STOPPED":
        return AgentProcessStopped()
    raise RuntimeError("watchdog returned an unknown pre-start outcome")


def _process_outcome(response: dict[str, object]) -> AgentProcessOutcome:
    response_type = response.get("type")
    if response_type == "COMPLETED":
        try:
            return_code = response["return_code"]
            if type(return_code) is not int:
                raise TypeError
            return AgentProcessExited(
                return_code,
                base64.b64decode(str(response["standard_output"]), validate=True),
                base64.b64decode(str(response["standard_error"]), validate=True),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError("watchdog completion is malformed") from error
    if response_type == "OUTPUT_LIMIT_EXCEEDED":
        return AgentProcessOutputLimitExceeded()
    if response_type == "SUPERVISION_FAILED":
        return AgentProcessSupervisionFailed()
    if response_type == "STOPPED":
        return AgentProcessStopped()
    if response_type == "RECOVERY_HANDOFF":
        raise AgentProcessOwnerNotLocal
    raise RuntimeError("watchdog returned an unknown process outcome")


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
