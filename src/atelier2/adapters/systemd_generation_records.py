from __future__ import annotations

import base64
import json
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, cast

from atelier2.contracts.agent_attempts import AgentAttemptId, WatchdogGenerationId
from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.contracts.hashing import Sha256Hash
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
    AgentProcessCompletion,
)

_RECORD_VERSION = 1
_INTENT_NAME = "INTENT"
_STARTED_NAME = "STARTED"
_RESULT_NAME = "RESULT"
_INVOCATION_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class DirectSystemdInvocationId:
    value: str

    def __post_init__(self) -> None:
        if _INVOCATION_ID.fullmatch(self.value) is None:
            raise ValueError(
                "systemd invocation id must be 32 lowercase hexadecimal characters"
            )


@dataclass(frozen=True)
class DirectSystemdIntent:
    attempt_id: AgentAttemptId
    generation_id: WatchdogGenerationId
    unit_name: str
    launch_envelope_hash: Sha256Hash
    standard_output_limit: int

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, AgentAttemptId):
            raise TypeError("systemd intent attempt id must be typed")
        if not isinstance(self.generation_id, WatchdogGenerationId):
            raise TypeError("systemd intent generation id must be typed")
        if (
            not self.unit_name.endswith(".service")
            or not self.unit_name.isascii()
            or not 1 <= len(self.unit_name) <= 255
        ):
            raise ValueError("systemd intent unit name must be a bounded service name")
        if not isinstance(self.launch_envelope_hash, Sha256Hash):
            raise TypeError("systemd intent launch envelope hash must be typed")
        if (
            type(self.standard_output_limit) is not int
            or self.standard_output_limit < 1
            or self.standard_output_limit > MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES
        ):
            raise ValueError("systemd intent output limit exceeds the portable bound")


@dataclass(frozen=True)
class DirectSystemdStarted:
    intent_hash: Sha256Hash
    invocation_id: DirectSystemdInvocationId

    def __post_init__(self) -> None:
        if not isinstance(self.intent_hash, Sha256Hash):
            raise TypeError("systemd STARTED intent hash must be typed")
        if not isinstance(self.invocation_id, DirectSystemdInvocationId):
            raise TypeError("systemd STARTED invocation id must be typed")


class DirectSystemdResultOutcome(StrEnum):
    COMPLETED = "COMPLETED"
    OUTPUT_LIMIT_EXCEEDED = "OUTPUT_LIMIT_EXCEEDED"
    PROCESS_BOUNDARY_FAILED = "PROCESS_BOUNDARY_FAILED"


@dataclass(frozen=True)
class DirectSystemdResult:
    started_hash: Sha256Hash
    invocation_id: DirectSystemdInvocationId
    outcome: DirectSystemdResultOutcome
    return_code: int | None
    standard_output: bytes
    standard_error: bytes
    standard_output_overflow: bool
    standard_error_overflow: bool

    def __post_init__(self) -> None:
        if not isinstance(self.started_hash, Sha256Hash):
            raise TypeError("systemd RESULT STARTED hash must be typed")
        if not isinstance(self.invocation_id, DirectSystemdInvocationId):
            raise TypeError("systemd RESULT invocation id must be typed")
        if not isinstance(self.outcome, DirectSystemdResultOutcome):
            raise TypeError("systemd RESULT outcome must be typed")
        if self.return_code is not None and type(self.return_code) is not int:
            raise TypeError("systemd RESULT return code must be an integer or null")
        if self.return_code is not None and not (
            -MAXIMUM_SIGNED_INT64 - 1 <= self.return_code <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError("systemd RESULT return code must fit signed int64")
        if len(self.standard_error) > MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES:
            raise ValueError("systemd RESULT standard error exceeds its exact bound")
        if self.outcome is DirectSystemdResultOutcome.COMPLETED:
            if (
                self.return_code is None
                or self.standard_output_overflow
                or self.standard_error_overflow
            ):
                raise ValueError("completed systemd RESULT has a contradictory shape")
        elif self.outcome is DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED:
            if not (self.standard_output_overflow or self.standard_error_overflow):
                raise ValueError("overflow systemd RESULT names no overflowing stream")
        elif (
            self.return_code is not None
            or self.standard_output
            or self.standard_error
            or self.standard_output_overflow
            or self.standard_error_overflow
        ):
            raise ValueError(
                "failed process boundary RESULT must carry no process data"
            )

    @property
    def process_completion(self) -> AgentProcessCompletion | None:
        if self.outcome is not DirectSystemdResultOutcome.COMPLETED:
            return None
        return AgentProcessCompletion(
            cast(int, self.return_code), self.standard_output, self.standard_error
        )


class DirectSystemdRecoveryState(StrEnum):
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    POSSIBLY_RAN = "POSSIBLY_RAN"
    RESULT_PRESENT = "RESULT_PRESENT"


@dataclass(frozen=True)
class DirectSystemdGenerationInspection:
    state: DirectSystemdRecoveryState
    intent: DirectSystemdIntent
    started: DirectSystemdStarted | None = None
    result: DirectSystemdResult | None = None


@dataclass(frozen=True)
class RecordPublicationBarriers:
    after_file_fsync: Callable[[], None] = lambda: None
    after_directory_fsync: Callable[[], None] = lambda: None


_NO_RECORD_PUBLICATION_BARRIERS = RecordPublicationBarriers()


class DirectSystemdGenerationRecords:
    def __init__(self, root: Path) -> None:
        if not root.is_absolute() or not root.is_dir():
            raise ValueError("systemd generation root must be an existing directory")
        self.intent_path = root / _INTENT_NAME
        self.started_path = root / _STARTED_NAME
        self.result_path = root / _RESULT_NAME

    def publish_intent(self, intent: DirectSystemdIntent) -> None:
        self._publish(self.intent_path, encode_direct_systemd_intent(intent))

    def publish_started(
        self,
        started: DirectSystemdStarted,
        barriers: RecordPublicationBarriers = _NO_RECORD_PUBLICATION_BARRIERS,
    ) -> None:
        self._publish(
            self.started_path, encode_direct_systemd_started(started), barriers
        )

    def publish_result(self, result: DirectSystemdResult) -> None:
        self._publish(self.result_path, encode_direct_systemd_result(result))

    def read_intent(self) -> DirectSystemdIntent:
        return decode_direct_systemd_intent(
            _read_bounded(self.intent_path, _maximum_intent_bytes())
        )

    def inspect(self) -> DirectSystemdGenerationInspection:
        intent = self.read_intent()
        if not os.path.lexists(self.started_path):
            if os.path.lexists(self.result_path):
                raise RuntimeError("systemd RESULT exists without STARTED")
            return DirectSystemdGenerationInspection(
                DirectSystemdRecoveryState.SAFE_TO_RETRY, intent
            )
        try:
            started_bytes = _read_bounded(self.started_path, _maximum_started_bytes())
            started = decode_direct_systemd_started(started_bytes)
        except (OSError, ValueError):
            return DirectSystemdGenerationInspection(
                DirectSystemdRecoveryState.POSSIBLY_RAN, intent
            )
        if started.intent_hash != Sha256Hash.of(encode_direct_systemd_intent(intent)):
            raise RuntimeError("systemd STARTED does not bind the exact INTENT")
        if not os.path.lexists(self.result_path):
            return DirectSystemdGenerationInspection(
                DirectSystemdRecoveryState.POSSIBLY_RAN, intent, started
            )
        try:
            result_bytes = _read_bounded(
                self.result_path,
                _maximum_result_bytes(intent.standard_output_limit),
            )
            result = decode_direct_systemd_result(result_bytes)
        except (OSError, ValueError):
            return DirectSystemdGenerationInspection(
                DirectSystemdRecoveryState.POSSIBLY_RAN, intent, started
            )
        if result.started_hash != Sha256Hash.of(started_bytes):
            raise RuntimeError("systemd RESULT does not bind the exact STARTED")
        if result.invocation_id != started.invocation_id:
            raise RuntimeError("systemd RESULT invocation differs from STARTED")
        if len(result.standard_output) > intent.standard_output_limit:
            raise RuntimeError("systemd RESULT output exceeds its INTENT bound")
        return DirectSystemdGenerationInspection(
            DirectSystemdRecoveryState.RESULT_PRESENT, intent, started, result
        )

    def fsync_directory(self) -> None:
        descriptor = os.open(self.intent_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _publish(
        self,
        path: Path,
        payload: bytes,
        barriers: RecordPublicationBarriers = _NO_RECORD_PUBLICATION_BARRIERS,
    ) -> None:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
            barriers.after_file_fsync()
        finally:
            os.close(descriptor)
        self.fsync_directory()
        barriers.after_directory_fsync()


def encode_direct_systemd_intent(intent: DirectSystemdIntent) -> bytes:
    return encode_canonical_systemd_json(
        {
            "attempt_id": intent.attempt_id.value,
            "generation_id": intent.generation_id.value,
            "launch_envelope_sha256": intent.launch_envelope_hash.value,
            "standard_output_limit": intent.standard_output_limit,
            "type": _INTENT_NAME,
            "unit_name": intent.unit_name,
            "version": _RECORD_VERSION,
        }
    )


def decode_direct_systemd_intent(payload: bytes) -> DirectSystemdIntent:
    value = decode_canonical_systemd_json(payload)
    _require_fields(
        value,
        {
            "attempt_id",
            "generation_id",
            "launch_envelope_sha256",
            "standard_output_limit",
            "type",
            "unit_name",
            "version",
        },
        _INTENT_NAME,
    )
    return DirectSystemdIntent(
        AgentAttemptId(_require_string(value, "attempt_id")),
        WatchdogGenerationId(_require_string(value, "generation_id")),
        _require_string(value, "unit_name"),
        Sha256Hash(_require_string(value, "launch_envelope_sha256")),
        _require_integer(value, "standard_output_limit"),
    )


def encode_direct_systemd_started(started: DirectSystemdStarted) -> bytes:
    return encode_canonical_systemd_json(
        {
            "intent_sha256": started.intent_hash.value,
            "invocation_id": started.invocation_id.value,
            "type": _STARTED_NAME,
            "version": _RECORD_VERSION,
        }
    )


def decode_direct_systemd_started(payload: bytes) -> DirectSystemdStarted:
    value = decode_canonical_systemd_json(payload)
    _require_fields(
        value,
        {"intent_sha256", "invocation_id", "type", "version"},
        _STARTED_NAME,
    )
    return DirectSystemdStarted(
        Sha256Hash(_require_string(value, "intent_sha256")),
        DirectSystemdInvocationId(_require_string(value, "invocation_id")),
    )


def encode_direct_systemd_result(result: DirectSystemdResult) -> bytes:
    return encode_canonical_systemd_json(
        {
            "invocation_id": result.invocation_id.value,
            "outcome": result.outcome.value,
            "return_code": result.return_code,
            "standard_error": base64.b64encode(result.standard_error).decode("ascii"),
            "standard_error_overflow": result.standard_error_overflow,
            "standard_output": base64.b64encode(result.standard_output).decode("ascii"),
            "standard_output_overflow": result.standard_output_overflow,
            "started_sha256": result.started_hash.value,
            "type": _RESULT_NAME,
            "version": _RECORD_VERSION,
        }
    )


def decode_direct_systemd_result(payload: bytes) -> DirectSystemdResult:
    value = decode_canonical_systemd_json(payload)
    _require_fields(
        value,
        {
            "invocation_id",
            "outcome",
            "return_code",
            "standard_error",
            "standard_error_overflow",
            "standard_output",
            "standard_output_overflow",
            "started_sha256",
            "type",
            "version",
        },
        _RESULT_NAME,
    )
    raw_return_code = value["return_code"]
    if raw_return_code is not None and type(raw_return_code) is not int:
        raise ValueError("systemd RESULT return code is malformed")
    return DirectSystemdResult(
        Sha256Hash(_require_string(value, "started_sha256")),
        DirectSystemdInvocationId(_require_string(value, "invocation_id")),
        DirectSystemdResultOutcome(_require_string(value, "outcome")),
        raw_return_code,
        decode_canonical_systemd_base64(value, "standard_output"),
        decode_canonical_systemd_base64(value, "standard_error"),
        _require_boolean(value, "standard_output_overflow"),
        _require_boolean(value, "standard_error_overflow"),
    )


def encode_canonical_systemd_json(value: dict[str, object]) -> bytes:
    return (
        json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
        + "\n"
    ).encode("ascii")


def decode_canonical_systemd_json(payload: bytes) -> dict[str, Any]:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for name, field in pairs:
            if name in value:
                raise ValueError("systemd record has duplicate fields")
            value[name] = field
        return value

    try:
        decoded = json.loads(payload.decode("ascii"), object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("systemd record is malformed") from error
    if type(decoded) is not dict or encode_canonical_systemd_json(decoded) != payload:
        raise ValueError("systemd record is not canonical")
    return decoded


def _require_fields(value: dict[str, Any], fields: set[str], kind: str) -> None:
    if set(value) != fields:
        raise ValueError(f"systemd {kind} fields are malformed")
    if value["type"] != kind or value["version"] != _RECORD_VERSION:
        raise ValueError(f"systemd {kind} identity is malformed")


def _require_string(value: dict[str, Any], name: str) -> str:
    field = value[name]
    if type(field) is not str:
        raise ValueError(f"systemd record {name} is malformed")
    return field


def _require_integer(value: dict[str, Any], name: str) -> int:
    field = value[name]
    if type(field) is not int:
        raise ValueError(f"systemd record {name} is malformed")
    return field


def _require_boolean(value: dict[str, Any], name: str) -> bool:
    field = value[name]
    if type(field) is not bool:
        raise ValueError(f"systemd record {name} is malformed")
    return field


def decode_canonical_systemd_base64(value: dict[str, Any], name: str) -> bytes:
    encoded = _require_string(value, name)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise ValueError(f"systemd record {name} is malformed") from error
    if base64.b64encode(decoded).decode("ascii") != encoded:
        raise ValueError(f"systemd record {name} is not canonical")
    return decoded


def _maximum_intent_bytes() -> int:
    return len(
        encode_canonical_systemd_json(
            {
                "attempt_id": "f" * 64,
                "generation_id": "\U0010ffff" * 1024,
                "launch_envelope_sha256": "f" * 64,
                "standard_output_limit": MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
                "type": _INTENT_NAME,
                "unit_name": f"{'u' * 247}.service",
                "version": _RECORD_VERSION,
            }
        )
    )


def _maximum_started_bytes() -> int:
    return len(
        encode_canonical_systemd_json(
            {
                "intent_sha256": "f" * 64,
                "invocation_id": "f" * 32,
                "type": _STARTED_NAME,
                "version": _RECORD_VERSION,
            }
        )
    )


def _maximum_result_bytes(standard_output_limit: int) -> int:
    maximum_process_data_bytes = 4 * ((standard_output_limit + 2) // 3) + 4 * (
        (MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES + 2) // 3
    )
    widest_return_code = -MAXIMUM_SIGNED_INT64 - 1
    valid_shapes = (
        (DirectSystemdResultOutcome.COMPLETED, False, False),
        (DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED, True, False),
        (DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED, False, True),
        (DirectSystemdResultOutcome.OUTPUT_LIMIT_EXCEEDED, True, True),
    )
    return max(
        *(
            len(
                encode_canonical_systemd_json(
                    {
                        "invocation_id": "f" * 32,
                        "outcome": outcome.value,
                        "return_code": widest_return_code,
                        "standard_error": "",
                        "standard_error_overflow": standard_error_overflow,
                        "standard_output": "",
                        "standard_output_overflow": standard_output_overflow,
                        "started_sha256": "f" * 64,
                        "type": _RESULT_NAME,
                        "version": _RECORD_VERSION,
                    }
                )
            )
            + maximum_process_data_bytes
            for outcome, standard_output_overflow, standard_error_overflow in valid_shapes
        ),
        len(
            encode_canonical_systemd_json(
                {
                    "invocation_id": "f" * 32,
                    "outcome": DirectSystemdResultOutcome.PROCESS_BOUNDARY_FAILED.value,
                    "return_code": None,
                    "standard_error": "",
                    "standard_error_overflow": False,
                    "standard_output": "",
                    "standard_output_overflow": False,
                    "started_sha256": "f" * 64,
                    "type": _RESULT_NAME,
                    "version": _RECORD_VERSION,
                }
            )
        ),
    )


def _read_bounded(path: Path, maximum_bytes: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise ValueError(f"systemd record {path.name} is not a regular file")
    with os.fdopen(descriptor, "rb") as stream:
        payload = stream.read(maximum_bytes + 1)
    if len(payload) > maximum_bytes:
        raise ValueError(f"systemd record {path.name} exceeds its exact bound")
    return payload
