from __future__ import annotations

import struct
from dataclasses import dataclass

from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
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
from atelier2.contracts.agent_transcripts import AttemptTranscript
from atelier2.contracts.agents import (
    AgentExecutionRequestHash,
    AgentExecutionResult,
)
from atelier2.contracts.hashing import frame

MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES = 1_106_413
"""Largest canonical V2 envelope or ACK tombstone in exact encoded bytes."""

_DOMAIN = "runner-terminal-evidence-exchange/v2"
_FRAME_HEADER = frame(_DOMAIN)
_V1_DOMAIN = "runner-terminal-evidence-exchange/v1"
_V1_FRAME_HEADER = frame(_V1_DOMAIN)
_FIELD_COUNT = 15
_ENVELOPE_TAG = b"envelope"
_ACK_TOMBSTONE_TAG = b"ack-tombstone"


@dataclass(frozen=True)
class RunnerTerminalEvidenceRecordMissing:
    """The Runner retains no record for the requested generation."""


@dataclass(frozen=True)
class RunnerTerminalEvidenceRecordCorrupt:
    """The retained bytes are not one canonical self-checking V2 record."""


@dataclass(frozen=True)
class RunnerTerminalEvidenceRecordOversized:
    """The retained bytes cross the V2 bound and were refused before parsing."""


@dataclass(frozen=True)
class RunnerTerminalEvidenceExchangeVersionRefused(RunnerTerminalEvidenceRecordCorrupt):
    """A record names a retired exchange version this decoder will not read."""

    refused_exchange: str
    reason: str


type RunnerTerminalEvidenceRecordDecodeResult = (
    RunnerTerminalEvidenceReadback
    | RunnerTerminalEvidenceRecordMissing
    | RunnerTerminalEvidenceRecordCorrupt
    | RunnerTerminalEvidenceRecordOversized
    | RunnerTerminalEvidenceExchangeVersionRefused
)


def encode_runner_terminal_evidence_record(
    record: RunnerTerminalEvidenceReadback,
) -> bytes:
    """Encode exact typed evidence or its payload-free ACK replacement."""

    if isinstance(record, RunnerTerminalEvidenceEnvelope):
        evidence_fields = _encode_evidence(record)
        tag = _ENVELOPE_TAG
        evidence_hash = RunnerTerminalEvidenceHash.for_envelope(record)
    elif isinstance(record, RunnerTerminalEvidenceAckTombstone):
        tag = _ACK_TOMBSTONE_TAG
        evidence_hash = record.evidence_hash
        evidence_fields = (b"",) * 8
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
        *evidence_fields,
    )
    if len(encoded) > MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES:
        raise ValueError("runner terminal-evidence record exceeds its V2 byte bound")
    return encoded


def decode_runner_terminal_evidence_record(
    record: bytes | None,
) -> RunnerTerminalEvidenceRecordDecodeResult:
    """Decode only canonical bounded V2 bytes, without guessing damaged truth."""

    if record is None:
        return RunnerTerminalEvidenceRecordMissing()
    if type(record) is not bytes:
        return RunnerTerminalEvidenceRecordCorrupt()
    if len(record) > MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES:
        return RunnerTerminalEvidenceRecordOversized()
    if record.startswith(_V1_FRAME_HEADER):
        return RunnerTerminalEvidenceExchangeVersionRefused(
            _V1_DOMAIN,
            f"{_V1_DOMAIN} was refused because only {_DOMAIN} is supported",
        )

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
) -> tuple[bytes, bytes, bytes, bytes, bytes, bytes, bytes, bytes]:
    match envelope.evidence:
        case RunnerProviderResult(result):
            transcript_presence, transcript_document = _encode_transcript(
                result.transcript
            )
            return (
                b"provider-result",
                result.output_bytes,
                b"",
                b"",
                b"",
                b"",
                transcript_presence,
                transcript_document,
            )
        case RunnerProviderFailure(exit_signature, failure_code, transcript):
            transcript_presence, transcript_document = _encode_transcript(transcript)
            return (
                b"provider-failure",
                b"",
                b"",
                failure_code.value.encode("ascii"),
                struct.pack(">q", exit_signature.return_code),
                exit_signature.standard_error,
                transcript_presence,
                transcript_document,
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
                b"",
                b"",
                b"",
                b"",
                b"",
            )
        case RunnerProcessBoundaryFailure():
            return b"process-boundary-failure", b"", b"", b"", b"", b"", b"", b""
        case RunnerCancellation(command_id, observation):
            return (
                b"cancellation",
                command_id.encode("utf-8"),
                observation.value.encode("ascii"),
                b"",
                b"",
                b"",
                b"",
                b"",
            )
        case RunnerInvocationLost():
            return b"invocation-lost", b"", b"", b"", b"", b"", b"", b""


def _encode_transcript(transcript: AttemptTranscript | None) -> tuple[bytes, bytes]:
    if transcript is None:
        return b"absent", b""
    return b"present", transcript.document


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
        failure_code,
        exit_code,
        standard_error,
        transcript_presence,
        transcript_document,
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
        if any(fields[7:]):
            raise ValueError("runner ACK tombstone payload slots must be empty")
        return RunnerTerminalEvidenceAckTombstone(binding, invocation, semantic_hash)
    if tag != _ENVELOPE_TAG:
        raise ValueError("runner evidence record tag is unknown")

    envelope = RunnerTerminalEvidenceEnvelope(
        binding,
        invocation,
        _decode_evidence(
            variant,
            first_payload,
            second_payload,
            failure_code,
            exit_code,
            standard_error,
            transcript_presence,
            transcript_document,
        ),
    )
    if RunnerTerminalEvidenceHash.for_envelope(envelope) != semantic_hash:
        raise ValueError("runner evidence record semantic hash differs")
    return envelope


def _decode_evidence(
    variant: bytes,
    first_payload: bytes,
    second_payload: bytes,
    failure_code: bytes,
    exit_code: bytes,
    standard_error: bytes,
    transcript_presence: bytes,
    transcript_document: bytes,
) -> (
    RunnerProviderResult
    | RunnerProviderFailure
    | RunnerOutputLimitExceeded
    | RunnerProcessBoundaryFailure
    | RunnerCancellation
    | RunnerInvocationLost
):
    if variant == b"provider-result":
        _require_empty(second_payload, failure_code, exit_code, standard_error)
        return RunnerProviderResult(
            AgentExecutionResult(
                first_payload,
                _decode_transcript(transcript_presence, transcript_document),
            )
        )
    if variant == b"provider-failure":
        _require_empty(first_payload, second_payload)
        if len(exit_code) != 8:
            raise ValueError("runner provider failure return code is malformed")
        return RunnerProviderFailure(
            ProcessExitSignature(struct.unpack(">q", exit_code)[0], standard_error),
            AgentAttemptFailureCode(_ascii(failure_code)),
            _decode_transcript(transcript_presence, transcript_document),
        )
    _require_empty(
        failure_code,
        exit_code,
        standard_error,
        transcript_presence,
        transcript_document,
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


def _decode_transcript(presence: bytes, document: bytes) -> AttemptTranscript | None:
    if presence == b"absent":
        _require_empty(document)
        return None
    if presence == b"present":
        return AttemptTranscript.from_document(document)
    raise ValueError("runner evidence transcript presence is malformed")


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
