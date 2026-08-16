from __future__ import annotations

import fcntl
import math
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from atelier2.adapters.bounded_processes import bounded_process_answer
from atelier2.adapters.systemd_agent_collector import (
    DIRECT_SYSTEMD_LAUNCH_CREDENTIAL_NAME,
    DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME,
    encode_direct_systemd_launch_envelope,
    fsync_direct_systemd_directory,
    read_direct_systemd_launch_envelope,
)
from atelier2.adapters.systemd_generation_records import (
    DirectSystemdGenerationInspection,
    DirectSystemdGenerationRecords,
    DirectSystemdIntent,
    DirectSystemdRecoveryState,
    DirectSystemdResult,
)
from atelier2.adapters.systemd_timespans import (
    DirectSystemdHostFailure,
    DirectSystemdUnitConflict,
    normalize_direct_systemd_timespan,
)
from atelier2.contracts.agent_attempts import AgentAttemptId, WatchdogGenerationId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.ports.agent_executions import AgentProcessInvocation

_MAXIMUM_SYSTEMD_SECONDS = (2**64 - 2) // 1_000_000
_MAXIMUM_VERSION_OUTPUT_BYTES = 4096
_MAXIMUM_UNIT_STATE_OUTPUT_BYTES = len(b"not-found\n")
_MAXIMUM_QUIET_OUTPUT_BYTES = 0
_COLLECTOR_MODULE = "atelier2.adapters.systemd_agent_collector"
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}")
_DurationIdentity = tuple[tuple[int, int], dict[tuple[str, int], int]]


class DirectSystemdPossiblyRan(RuntimeError): ...


class _DirectSystemdCommandNotStarted(DirectSystemdHostFailure): ...


class _DirectSystemdResultPublished(RuntimeError): ...


class _DirectSystemdEvidenceChanged(RuntimeError): ...


@dataclass(frozen=True)
class DirectSystemdAgentProcessConfiguration:
    systemd_run: Path
    systemctl: Path
    systemd_analyze: Path
    python: Path
    user_runtime_directory: Path
    timeout_stop_seconds: int
    runtime_max_seconds: int
    command_timeout_seconds: float
    preparation_timeout_seconds: float
    reobservation_timeout_seconds: float
    observation_poll_seconds: float

    def __post_init__(self) -> None:
        paths = (
            self.systemd_run,
            self.systemctl,
            self.systemd_analyze,
            self.python,
            self.user_runtime_directory,
        )
        if not all(path.is_absolute() for path in paths):
            raise ValueError("direct systemd executables must be absolute")
        if any(
            type(value) is not int or not 1 <= value <= _MAXIMUM_SYSTEMD_SECONDS
            for value in (self.timeout_stop_seconds, self.runtime_max_seconds)
        ):
            raise ValueError("direct systemd unit bounds are outside systemd's domain")
        if any(
            type(value) not in (int, float) or not math.isfinite(value) or value <= 0
            for value in (
                self.command_timeout_seconds,
                self.preparation_timeout_seconds,
                self.reobservation_timeout_seconds,
                self.observation_poll_seconds,
            )
        ):
            raise ValueError(
                "direct systemd observation bounds must be finite and positive"
            )


def direct_systemd_unit_name(
    attempt_id: AgentAttemptId, generation_id: WatchdogGenerationId
) -> str:
    identity = attempt_id.value.encode("ascii") + b"\0" + generation_id.value.encode()
    digest = Sha256Hash.of(b"atelier2-direct-systemd-unit/v1\0" + identity)
    return f"atelier2-agent-{digest.value}.service"


class DirectSystemdAgentProcessManager:
    def __init__(self, configuration: DirectSystemdAgentProcessConfiguration) -> None:
        self._configuration = configuration
        self._collector = (str(configuration.python), "-m", _COLLECTOR_MODULE)
        self._environment = {
            "LC_ALL": "C",
            "SYSTEMD_COLORS": "0",
            "SYSTEMD_URLIFY": "0",
            "XDG_RUNTIME_DIR": str(configuration.user_runtime_directory),
        }

    def probe(self) -> None:
        deadline = time.monotonic() + self._configuration.command_timeout_seconds
        self._duration_identity(deadline)
        for command, maximum_output_bytes in (
            (
                (str(self._configuration.systemd_run), "--version"),
                _MAXIMUM_VERSION_OUTPUT_BYTES,
            ),
            (
                self._systemctl_command("show", "--property=Version", "--no-pager"),
                _MAXIMUM_VERSION_OUTPUT_BYTES,
            ),
        ):
            if _run_direct_systemd_command(
                command,
                _command_timeout(self._configuration, deadline),
                maximum_output_bytes,
                self._environment,
            )[0]:
                raise DirectSystemdHostFailure("direct systemd capability probe failed")

    def start(
        self,
        attempt_id: AgentAttemptId,
        generation_id: WatchdogGenerationId,
        generation_directory: Path,
        invocation: AgentProcessInvocation,
    ) -> DirectSystemdGenerationInspection:
        if not generation_directory.is_absolute():
            raise ValueError("direct systemd generation directory must be absolute")
        deadline = time.monotonic() + (
            self._configuration.command_timeout_seconds
            + self._configuration.preparation_timeout_seconds
            + self._configuration.reobservation_timeout_seconds
        )
        identity = self._duration_identity(deadline)
        _ensure_generation_directory(generation_directory)
        with _generation_lock(
            generation_directory,
            min(
                deadline,
                time.monotonic() + self._configuration.preparation_timeout_seconds,
            ),
            self._configuration.observation_poll_seconds,
        ) as (locked_directory, generation_descriptor):
            try:
                records = self._prepare(
                    attempt_id, generation_id, locked_directory, invocation
                )
            except OSError as error:
                raise DirectSystemdHostFailure(
                    "direct systemd generation could not be prepared"
                ) from error
            except ValueError as error:
                raise DirectSystemdUnitConflict(
                    "direct systemd generation preparation conflicts"
                ) from error
            _require_same_inode(generation_directory, generation_descriptor)
            launched = False
            original: DirectSystemdHostFailure | None = None
            while True:
                inspection = _inspect(records)
                try:
                    live: bool | None = self._observe_unit(
                        inspection.intent,
                        generation_directory,
                        identity,
                        deadline,
                    )
                except DirectSystemdHostFailure as error:
                    original, live = original or error, None
                if live is False:
                    inspection = _inspect(records)
                    if inspection.result is not None:
                        return inspection
                    if inspection.state is DirectSystemdRecoveryState.POSSIBLY_RAN:
                        raise DirectSystemdPossiblyRan("provider possibly ran")
                    if launched:
                        if original is not None:
                            raise original
                        raise DirectSystemdHostFailure("unit vanished before STARTED")
                    launched = True
                    _require_same_inode(generation_directory, generation_descriptor)
                    command = self._start_command(
                        inspection.intent, generation_directory
                    )
                    try:
                        return_code = _run_direct_systemd_command(
                            command,
                            _command_timeout(self._configuration, deadline),
                            _MAXIMUM_QUIET_OUTPUT_BYTES,
                            self._environment,
                            before_spawn=lambda: _require_same_inode(
                                generation_directory, generation_descriptor
                            ),
                        )[0]
                        if return_code:
                            raise DirectSystemdHostFailure(
                                "systemd refused direct agent start"
                            )
                    except _DirectSystemdCommandNotStarted:
                        self._remove_source_before_launch(records)
                        raise
                    except DirectSystemdHostFailure as error:
                        original = error
                elif (
                    live is True
                    and inspection.started is not None
                    and inspection.result is None
                ):
                    return inspection
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if inspection.state is DirectSystemdRecoveryState.POSSIBLY_RAN:
                        raise DirectSystemdPossiblyRan("provider possibly ran")
                    if original is not None:
                        raise original
                    raise DirectSystemdHostFailure(
                        "systemd unit did not reach a durable state"
                    )
                time.sleep(min(self._configuration.observation_poll_seconds, remaining))

    def wait(self, generation_directory: Path) -> DirectSystemdResult:
        records = DirectSystemdGenerationRecords(generation_directory)
        intent = _inspect(records).intent
        deadline = (
            time.monotonic()
            + self._configuration.command_timeout_seconds
            + self._configuration.runtime_max_seconds
            + self._configuration.timeout_stop_seconds
            + self._configuration.reobservation_timeout_seconds
        )
        identity = self._duration_identity(deadline)
        original: DirectSystemdHostFailure | None = None
        failure_deadline: float | None = None
        while True:
            observation_deadline = failure_deadline or deadline
            try:
                live: bool | None = self._observe_unit(
                    intent, generation_directory, identity, observation_deadline
                )
            except DirectSystemdHostFailure as error:
                original = original or error
                failure_deadline = failure_deadline or min(
                    deadline,
                    time.monotonic()
                    + self._configuration.reobservation_timeout_seconds,
                )
                live = None
            if live is False:
                inspection = _inspect(records)
                if inspection.result is not None:
                    return inspection.result
                if inspection.state is DirectSystemdRecoveryState.POSSIBLY_RAN:
                    raise DirectSystemdPossiblyRan("provider possibly ran")
                raise DirectSystemdHostFailure("unit disappeared before STARTED")
            if live is True:
                original = None
                failure_deadline = None
            remaining = (failure_deadline or deadline) - time.monotonic()
            if remaining <= 0:
                if original is not None:
                    raise original
                raise DirectSystemdHostFailure("systemd unit outlived its bounds")
            time.sleep(min(self._configuration.observation_poll_seconds, remaining))

    def stop(self, generation_directory: Path) -> DirectSystemdGenerationInspection:
        records = DirectSystemdGenerationRecords(generation_directory)
        intent = _inspect(records).intent
        deadline = time.monotonic() + (
            self._configuration.timeout_stop_seconds
            + self._configuration.command_timeout_seconds
            + self._configuration.reobservation_timeout_seconds
        )
        identity = self._duration_identity(deadline)
        original: DirectSystemdHostFailure | None = None
        requested = False
        while True:
            inspection = _inspect(records)
            observed_started = inspection.started
            try:
                live = self._observe_unit(
                    intent, generation_directory, identity, deadline
                )
            except DirectSystemdHostFailure as error:
                original, live = original or error, True
            if not live:
                return _inspect(records)
            inspection = _inspect(records)
            if inspection.started != observed_started:
                continue
            evidence_changed = False
            try:
                _refuse_stop_after_evidence(records, inspection)
            except (_DirectSystemdResultPublished, _DirectSystemdEvidenceChanged):
                evidence_changed = True
            if (
                inspection.result is None
                and original is None
                and not requested
                and not evidence_changed
            ):
                requested = True
                command = self._systemctl_command("stop", intent.unit_name)
                try:
                    if _run_direct_systemd_command(
                        command,
                        _command_timeout(self._configuration, deadline),
                        _MAXIMUM_QUIET_OUTPUT_BYTES,
                        self._environment,
                        before_spawn=lambda: self._attest_stop_admission(
                            records,
                            intent,
                            generation_directory,
                            identity,
                            deadline,
                        ),
                        timeout_at_spawn=lambda: _command_timeout(
                            self._configuration, deadline
                        ),
                    )[0]:
                        raise DirectSystemdHostFailure(
                            "systemd refused direct agent stop"
                        )
                except _DirectSystemdResultPublished:
                    pass
                except _DirectSystemdEvidenceChanged:
                    requested = False
                except DirectSystemdHostFailure as error:
                    original = error
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if _partial_stop_evidence_is_visible(records, _inspect(records)):
                    raise DirectSystemdPossiblyRan(
                        "direct systemd stop evidence is incomplete"
                    )
                if original is not None:
                    raise original
                raise DirectSystemdHostFailure("systemd unit outlived its stop bound")
            time.sleep(min(self._configuration.observation_poll_seconds, remaining))

    def _attest_stop_admission(
        self,
        records: DirectSystemdGenerationRecords,
        intent: DirectSystemdIntent,
        generation_directory: Path,
        identity: _DurationIdentity,
        deadline: float,
    ) -> None:
        before = _inspect(records)
        _refuse_stop_after_evidence(records, before)
        if not self._observe_unit(intent, generation_directory, identity, deadline):
            raise DirectSystemdHostFailure(
                "direct systemd unit vanished before stop admission"
            )
        after = _inspect(records)
        _refuse_stop_after_evidence(records, after)
        if after.started != before.started:
            if not self._observe_unit(intent, generation_directory, identity, deadline):
                raise DirectSystemdHostFailure(
                    "direct systemd unit vanished before STARTED attestation"
                )
            _refuse_stop_after_evidence(records, _inspect(records))

    def _prepare(
        self,
        attempt_id: AgentAttemptId,
        generation_id: WatchdogGenerationId,
        locked_directory: Path,
        invocation: AgentProcessInvocation,
    ) -> DirectSystemdGenerationRecords:
        records = DirectSystemdGenerationRecords(locked_directory)
        source = locked_directory / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME
        envelope = encode_direct_systemd_launch_envelope(invocation)
        intent = DirectSystemdIntent(
            attempt_id,
            generation_id,
            direct_systemd_unit_name(attempt_id, generation_id),
            Sha256Hash.of(envelope),
            invocation.standard_output_frame_bytes,
        )
        if os.path.lexists(records.intent_path):
            if _inspect(records).intent != intent:
                raise DirectSystemdUnitConflict("direct systemd INTENT changed")
            if os.path.lexists(records.started_path):
                return records
            if os.path.lexists(source):
                try:
                    if _read_source(source) != envelope:
                        raise DirectSystemdUnitConflict("launch envelope changed")
                except FileNotFoundError:
                    if os.path.lexists(records.started_path):
                        return records
                    _publish_source(source, envelope)
            elif not os.path.lexists(records.started_path):
                _publish_source(source, envelope)
            return records
        if os.path.lexists(records.started_path) or os.path.lexists(
            records.result_path
        ):
            raise DirectSystemdUnitConflict("systemd evidence exists without INTENT")
        if os.path.lexists(source):
            if _read_source(source) != envelope:
                raise DirectSystemdUnitConflict("source has no matching INTENT")
        else:
            _publish_source(source, envelope)
        records.publish_intent(intent)
        return records

    def _remove_source_before_launch(
        self, records: DirectSystemdGenerationRecords
    ) -> None:
        inspection = _inspect(records)
        if inspection.state is not DirectSystemdRecoveryState.SAFE_TO_RETRY:
            raise DirectSystemdPossiblyRan("provider possibly ran")
        source = records.intent_path.parent / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME
        try:
            if (
                Sha256Hash.of(_read_source(source))
                != inspection.intent.launch_envelope_hash
            ):
                raise DirectSystemdUnitConflict("unstarted launch source changed")
            source.unlink()
            records.fsync_directory()
        except OSError as error:
            raise DirectSystemdHostFailure(
                "unstarted direct systemd source could not be removed"
            ) from error

    def _observe_unit(
        self,
        intent: DirectSystemdIntent,
        generation_directory: Path,
        identity: _DurationIdentity,
        deadline: float,
    ) -> bool:
        state_command = self._systemctl_command(
            "show",
            intent.unit_name,
            "--property=LoadState",
            "--value",
            "--no-pager",
        )
        return_code, output = _run_direct_systemd_command(
            state_command,
            _command_timeout(self._configuration, deadline),
            _MAXIMUM_UNIT_STATE_OUTPUT_BYTES,
            self._environment,
        )
        if return_code:
            raise DirectSystemdHostFailure("systemd unit state observation failed")
        if output == b"not-found\n":
            return False
        if output != b"loaded\n":
            raise DirectSystemdUnitConflict("systemd unit state is malformed")
        expected = {
            "LoadState": "loaded",
            "Id": intent.unit_name,
            "Type": "exec",
            "ExitType": "main",
            "KillMode": "control-group",
            "SendSIGKILL": "yes",
            "CollectMode": "inactive-or-failed",
            "WorkingDirectory": str(generation_directory),
        }
        durations = ("TimeoutStopUSec", "RuntimeMaxUSec")
        names = (*expected, *durations, "InvocationID", "ExecStart")
        command = self._systemctl_command(
            "show",
            intent.unit_name,
            *(f"--property={name}" for name in names),
            "--no-pager",
        )
        return_code, output = _run_direct_systemd_command(
            command,
            _command_timeout(self._configuration, deadline),
            _maximum_properties_output_bytes(
                self._configuration, intent, generation_directory
            ),
            self._environment,
        )
        if return_code:
            raise DirectSystemdHostFailure("systemd unit observation failed")
        properties = _properties(output)
        if properties.get("LoadState") == "not-found":
            return False
        if set(properties) != set(names):
            raise DirectSystemdUnitConflict("systemd returned incomplete properties")
        if any(properties[name] != value for name, value in expected.items()):
            raise DirectSystemdUnitConflict("direct systemd unit properties changed")
        configured, observed = identity
        for name, expected_microseconds in zip(durations, configured, strict=True):
            key = (properties[name], expected_microseconds)
            if key not in observed:
                observed[key] = self._normalize_duration(*key, deadline)
        invocation_id = properties["InvocationID"]
        if _INVOCATION_ID.fullmatch(invocation_id) is None:
            raise DirectSystemdUnitConflict("direct systemd InvocationID is malformed")
        started = _inspect(DirectSystemdGenerationRecords(generation_directory)).started
        if started is not None and invocation_id != started.invocation_id.value:
            raise DirectSystemdUnitConflict("direct systemd InvocationID changed")
        prefix = (
            f"{{ path={self._configuration.python} ; "
            f"argv[]={' '.join(self._collector)} ; ignore_errors=no ; "
        )
        dynamic = (
            r"start_time=\[[^\]\r\n]+\] ; stop_time=\[[^\]\r\n]+\] ; "
            r"pid=[0-9]+ ; code=(?:\(null\)|exited|killed|dumped) ; "
            r"status=[0-9]+/[A-Za-z0-9_-]+ \}"
        )
        if re.fullmatch(re.escape(prefix) + dynamic, properties["ExecStart"]) is None:
            raise DirectSystemdUnitConflict("direct systemd ExecStart changed")
        source = generation_directory / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME
        cat_command = self._systemctl_command("cat", intent.unit_name, "--no-pager")
        return_code, output = _run_direct_systemd_command(
            cat_command,
            _command_timeout(self._configuration, deadline),
            _maximum_unit_file_output_bytes(
                self._configuration, intent, generation_directory
            ),
            self._environment,
        )
        if return_code:
            raise DirectSystemdHostFailure("systemd credential observation failed")
        expected_load_credential_line = (
            f"LoadCredential={DIRECT_SYSTEMD_LAUNCH_CREDENTIAL_NAME}:{source}"
        )
        try:
            load_credential_lines = [
                line.strip()
                for line in output.decode("utf-8", "strict").splitlines()
                if line.strip().startswith("LoadCredential=")
            ]
        except UnicodeDecodeError as error:
            raise DirectSystemdUnitConflict(
                "systemd credential output is malformed"
            ) from error
        if load_credential_lines != [expected_load_credential_line]:
            raise DirectSystemdUnitConflict("direct systemd credential changed")
        return True

    def _duration_identity(self, deadline: float) -> _DurationIdentity:
        configured = tuple(
            self._normalize_duration(f"{seconds}s", seconds * 1_000_000, deadline)
            for seconds in (
                self._configuration.timeout_stop_seconds,
                self._configuration.runtime_max_seconds,
            )
        )
        return (configured[0], configured[1]), {}

    def _normalize_duration(self, value: str, expected: int, deadline: float) -> int:
        return normalize_direct_systemd_timespan(
            self._configuration.systemd_analyze,
            value,
            command_timeout_seconds=_command_timeout(self._configuration, deadline),
            maximum_output_bytes=128 + 4 * len(value.encode()),
            expected_microseconds=expected,
        )

    def _start_command(
        self, intent: DirectSystemdIntent, generation_directory: Path
    ) -> tuple[str, ...]:
        configuration = self._configuration
        source = generation_directory / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME
        return (
            str(configuration.systemd_run),
            "--user",
            "--quiet",
            "--collect",
            "--no-ask-password",
            "--expand-environment=no",
            f"--unit={intent.unit_name}",
            "--property=Type=exec",
            "--property=ExitType=main",
            "--property=KillMode=control-group",
            f"--property=TimeoutStopSec={configuration.timeout_stop_seconds}s",
            f"--property=RuntimeMaxSec={configuration.runtime_max_seconds}s",
            "--property=SendSIGKILL=yes",
            f"--property=WorkingDirectory={generation_directory}",
            f"--property=LoadCredential={DIRECT_SYSTEMD_LAUNCH_CREDENTIAL_NAME}:{source}",
            *self._collector,
        )

    def _systemctl_command(self, *arguments: str) -> tuple[str, ...]:
        return (str(self._configuration.systemctl), "--user", *arguments)


def _run_direct_systemd_command(
    command: tuple[str, ...],
    timeout_seconds: float,
    maximum_output_bytes: int,
    environment: dict[str, str],
    *,
    before_spawn: Callable[[], None] = lambda: None,
    timeout_at_spawn: Callable[[], float] | None = None,
) -> tuple[int, bytes]:
    try:
        before_spawn()
        if timeout_at_spawn is not None:
            timeout_seconds = timeout_at_spawn()
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env=environment,
        )
    except (OSError, ValueError) as error:
        raise _DirectSystemdCommandNotStarted(
            "direct systemd command did not start"
        ) from error
    try:
        return bounded_process_answer(process, timeout_seconds, maximum_output_bytes)
    except OSError as error:
        raise DirectSystemdHostFailure("direct systemd command failed") from error


def _inspect(
    records: DirectSystemdGenerationRecords,
) -> DirectSystemdGenerationInspection:
    try:
        return records.inspect()
    except OSError as error:
        raise DirectSystemdHostFailure(
            "direct systemd evidence could not be read"
        ) from error
    except (RuntimeError, ValueError) as error:
        raise DirectSystemdUnitConflict("direct systemd evidence conflicts") from error


def _refuse_stop_after_evidence(
    records: DirectSystemdGenerationRecords,
    inspection: DirectSystemdGenerationInspection,
) -> None:
    if os.path.lexists(records.result_path):
        if inspection.result is not None:
            raise _DirectSystemdResultPublished
        raise _DirectSystemdEvidenceChanged
    if os.path.lexists(records.started_path) and inspection.started is None:
        raise _DirectSystemdEvidenceChanged


def _partial_stop_evidence_is_visible(
    records: DirectSystemdGenerationRecords,
    inspection: DirectSystemdGenerationInspection,
) -> bool:
    return (os.path.lexists(records.result_path) and inspection.result is None) or (
        os.path.lexists(records.started_path) and inspection.started is None
    )


def _ensure_generation_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, exist_ok=True)
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise DirectSystemdUnitConflict(
                "direct systemd generation directory must be mode 0700"
            )
        fsync_direct_systemd_directory(path.parent)
    except OSError as error:
        raise DirectSystemdHostFailure(
            "direct systemd generation directory could not be prepared"
        ) from error


@contextmanager
def _generation_lock(
    path: Path, deadline: float, poll: float
) -> Iterator[tuple[Path, int]]:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as error:
        raise DirectSystemdHostFailure(
            "direct systemd generation lock could not be opened"
        ) from error
    try:
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DirectSystemdHostFailure("systemd preparation lock timed out")
                time.sleep(min(poll, remaining))
            except OSError as error:
                raise DirectSystemdHostFailure(
                    "direct systemd generation lock failed"
                ) from error
        _require_same_inode(path, descriptor)
        yield Path(f"/proc/self/fd/{descriptor}"), descriptor
        _require_same_inode(path, descriptor)
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _require_same_inode(path: Path, descriptor: int) -> None:
    try:
        opened, named = os.fstat(descriptor), path.lstat()
    except OSError as error:
        raise DirectSystemdUnitConflict("systemd generation inode changed") from error
    if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
        raise DirectSystemdUnitConflict("systemd generation inode changed")


def _read_source(path: Path) -> bytes:
    return read_direct_systemd_launch_envelope(path, allowed_modes=frozenset({0o600}))


def _publish_source(path: Path, payload: bytes) -> None:
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    fsync_direct_systemd_directory(path.parent)


def _properties(payload: bytes) -> dict[str, str]:
    try:
        lines = payload.decode("utf-8", "strict").splitlines()
    except UnicodeDecodeError as error:
        raise DirectSystemdUnitConflict(
            "systemd returned invalid properties"
        ) from error
    properties: dict[str, str] = {}
    for line in lines:
        name, separator, value = line.partition("=")
        if not separator or name in properties:
            raise DirectSystemdUnitConflict("systemd returned malformed properties")
        properties[name] = value
    return properties


def _command_timeout(
    configuration: DirectSystemdAgentProcessConfiguration, deadline: float
) -> float:
    available_seconds = deadline - time.monotonic()
    if available_seconds <= 0:
        raise DirectSystemdHostFailure("direct systemd phase deadline expired")
    return min(configuration.command_timeout_seconds, available_seconds)


def _maximum_properties_output_bytes(
    configuration: DirectSystemdAgentProcessConfiguration,
    intent: DirectSystemdIntent,
    generation_directory: Path,
) -> int:
    return 4096 + 4 * sum(
        len(os.fsencode(value))
        for value in (
            intent.unit_name,
            str(generation_directory),
            str(configuration.python),
            *_COLLECTOR_MODULE.split(),
        )
    )


def _maximum_unit_file_output_bytes(
    configuration: DirectSystemdAgentProcessConfiguration,
    intent: DirectSystemdIntent,
    generation_directory: Path,
) -> int:
    source = generation_directory / DIRECT_SYSTEMD_LAUNCH_ENVELOPE_NAME
    return 8192 + 4 * sum(
        len(os.fsencode(value))
        for value in (
            intent.unit_name,
            str(source),
            str(configuration.python),
            _COLLECTOR_MODULE,
        )
    )
