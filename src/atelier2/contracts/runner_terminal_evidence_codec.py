from __future__ import annotations

import struct
from dataclasses import dataclass

from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    ProcessExitSignature,
    RunnerCancellation,
    RunnerCancellationObservation,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerInvocationLost,
    RunnerManifestId,
    RunnerOutputLimitExceeded,
    RunnerOutputStream,
    RunnerProcessBoundaryFailure,
    RunnerProviderFailure,
    RunnerProviderResult,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
    RunnerTerminalEvidenceReadback,
)
from atelier2.contracts.agents import (
    AgentExecutionRequestHash,
    AgentExecutionResult,
)
from atelier2.contracts.hashing import frame

MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES = 57_761
"""Largest canonical V1 envelope or ACK tombstone in exact encoded bytes."""

_DOMAIN = "runner-terminal-evidence-exchange/v1"
_FRAME_HEADER = frame(_DOMAIN)
_FIELD_COUNT = 10
_ENVELOPE_TAG = b"envelope"
_ACK_TOMBSTONE_TAG = b"ack-tombstone"


@dataclass(frozen=True)
class RunnerTerminalEvidenceRecordMissing:
    """The Runner retains no record for the requested generation."""


@dataclass(frozen=True)
class RunnerTerminalEvidenceRecordCorrupt:
    """The retained bytes are not one canonical self-checking V1 record."""


@dataclass(frozen=True)
class RunnerTerminalEvidenceRecordOversized:
    """The retained bytes cross the V1 bound and were refused before parsing."""


type RunnerTerminalEvidenceRecordDecodeResult = (
    RunnerTerminalEvidenceReadback
    | RunnerTerminalEvidenceRecordMissing
    | RunnerTerminalEvidenceRecordCorrupt
    | RunnerTerminalEvidenceRecordOversized
)


def encode_runner_terminal_evidence_record(
    record: RunnerTerminalEvidenceReadback,
) -> bytes:
    """Encode exact typed evidence or its payload-free ACK replacement."""

    if isinstance(record, RunnerTerminalEvidenceEnvelope):
        variant, first_payload, second_payload = _encode_evidence(record)
        tag = _ENVELOPE_TAG
        evidence_hash = RunnerTerminalEvidenceHash.for_envelope(record)
    elif isinstance(record, RunnerTerminalEvidenceAckTombstone):
        tag = _ACK_TOMBSTONE_TAG
        evidence_hash = record.evidence_hash
        variant = first_payload = second_payload = b""
    else:
        raise TypeError("runner evidence codec requires an envelope or ACK tombstone")

    binding = record.binding
    invocation = record.invocation_id
    encoded = frame(
        _DOMAIN,
        tag,
        binding.attempt_id.value.encode("ascii"),
        binding.request_hash.value.encode("ascii"),
        binding.generation_id.value.encode("utf-8"),
        binding.manifest_id.value.encode("ascii"),
        b"" if invocation is None else invocation.value.encode("utf-8"),
        evidence_hash.value.encode("ascii"),
        variant,
        first_payload,
        second_payload,
    )
    if len(encoded) > MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES:
        raise ValueError("runner terminal-evidence record exceeds its V1 byte bound")
    return encoded


def decode_runner_terminal_evidence_record(
    record: bytes | None,
) -> RunnerTerminalEvidenceRecordDecodeResult:
    """Decode only canonical bounded V1 bytes, without guessing damaged truth."""

    if record is None:
        return RunnerTerminalEvidenceRecordMissing()
    if type(record) is not bytes:
        return RunnerTerminalEvidenceRecordCorrupt()
    if len(record) > MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES:
        return RunnerTerminalEvidenceRecordOversized()

    try:
        fields = _decode_fields(record)
        decoded = _decode_record(fields)
        if encode_runner_terminal_evidence_record(decoded) != record:
            raise ValueError("runner evidence record is not canonical")
    except (UnicodeError, ValueError, TypeError, struct.error):
        return RunnerTerminalEvidenceRecordCorrupt()
    return decoded


def _encode_evidence(
    envelope: RunnerTerminalEvidenceEnvelope,
) -> tuple[bytes, bytes, bytes]:
    match envelope.evidence:
        case RunnerProviderResult(result):
            return b"provider-result", result.output_bytes, b""
        case RunnerProviderFailure(exit_signature):
            return (
                b"provider-failure",
                struct.pack(">q", exit_signature.return_code),
                exit_signature.standard_error,
            )
        case RunnerOutputLimitExceeded(exceeded_streams):
            ordered = tuple(
                stream.value.encode("ascii")
                for stream in sorted(exceeded_streams, key=lambda item: item.value)
            )
            return (
                b"output-limit-exceeded",
                ordered[0],
                b"" if len(ordered) == 1 else ordered[1],
            )
        case RunnerProcessBoundaryFailure():
            return b"process-boundary-failure", b"", b""
        case RunnerCancellation(command_id, observation):
            return (
                b"cancellation",
                command_id.encode("utf-8"),
                observation.value.encode("ascii"),
            )
        case RunnerInvocationLost():
            return b"invocation-lost", b"", b""


def _decode_fields(record: bytes) -> tuple[bytes, ...]:
    if not record.startswith(_FRAME_HEADER):
        raise ValueError("runner evidence record frame domain is malformed")
    cursor = len(_FRAME_HEADER)
    fields: list[bytes] = []
    for _ in range(_FIELD_COUNT):
        if cursor + 8 > len(record):
            raise ValueError("runner evidence record field length is missing")
        field_size = struct.unpack(">Q", record[cursor : cursor + 8])[0]
        cursor += 8
        field_end = cursor + field_size
        if field_end > len(record):
            raise ValueError("runner evidence record field is truncated")
        fields.append(record[cursor:field_end])
        cursor = field_end
    if cursor != len(record):
        raise ValueError("runner evidence record has trailing bytes")
    return tuple(fields)


def _decode_record(fields: tuple[bytes, ...]) -> RunnerTerminalEvidenceReadback:
    (
        tag,
        attempt_id,
        request_hash,
        generation_id,
        manifest_id,
        invocation_id,
        evidence_hash,
        variant,
        first_payload,
        second_payload,
    ) = fields
    binding = RunnerGenerationBinding(
        AgentAttemptId(_ascii(attempt_id)),
        AgentExecutionRequestHash(_ascii(request_hash)),
        RunnerGenerationId(_text(generation_id)),
        RunnerManifestId(_ascii(manifest_id)),
    )
    invocation = None if not invocation_id else RunnerInvocationId(_text(invocation_id))
    semantic_hash = RunnerTerminalEvidenceHash(_ascii(evidence_hash))

    if tag == _ACK_TOMBSTONE_TAG:
        if variant or first_payload or second_payload:
            raise ValueError("runner ACK tombstone payload slots must be empty")
        return RunnerTerminalEvidenceAckTombstone(binding, invocation, semantic_hash)
    if tag != _ENVELOPE_TAG:
        raise ValueError("runner evidence record tag is unknown")

    envelope = RunnerTerminalEvidenceEnvelope(
        binding,
        invocation,
        _decode_evidence(variant, first_payload, second_payload),
    )
    if RunnerTerminalEvidenceHash.for_envelope(envelope) != semantic_hash:
        raise ValueError("runner evidence record semantic hash differs")
    return envelope


def _decode_evidence(
    variant: bytes, first_payload: bytes, second_payload: bytes
) -> (
    RunnerProviderResult
    | RunnerProviderFailure
    | RunnerOutputLimitExceeded
    | RunnerProcessBoundaryFailure
    | RunnerCancellation
    | RunnerInvocationLost
):
    if variant == b"provider-result":
        _require_empty(second_payload)
        return RunnerProviderResult(AgentExecutionResult(first_payload))
    if variant == b"provider-failure":
        if len(first_payload) != 8:
            raise ValueError("runner provider failure return code is malformed")
        return RunnerProviderFailure(
            ProcessExitSignature(struct.unpack(">q", first_payload)[0], second_payload)
        )
    if variant == b"output-limit-exceeded":
        encoded_streams = (first_payload,) + (
            () if not second_payload else (second_payload,)
        )
        return RunnerOutputLimitExceeded(
            frozenset(RunnerOutputStream(_ascii(stream)) for stream in encoded_streams)
        )
    if variant == b"process-boundary-failure":
        _require_empty(first_payload, second_payload)
        return RunnerProcessBoundaryFailure()
    if variant == b"cancellation":
        return RunnerCancellation(
            _text(first_payload), RunnerCancellationObservation(_ascii(second_payload))
        )
    if variant == b"invocation-lost":
        _require_empty(first_payload, second_payload)
        return RunnerInvocationLost()
    raise ValueError("runner terminal-evidence variant is unknown")


def _ascii(value: bytes) -> str:
    return value.decode("ascii")


def _text(value: bytes) -> str:
    decoded = value.decode("utf-8")
    if decoded.encode("utf-8") != value:
        raise ValueError("runner evidence record text is not canonical UTF-8")
    return decoded


def _require_empty(*values: bytes) -> None:
    if any(values):
        raise ValueError("runner evidence record reserved payload slot is not empty")
