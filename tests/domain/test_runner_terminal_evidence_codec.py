from __future__ import annotations

import struct
from collections.abc import Callable
from typing import cast

import pytest

from atelier2.contracts.agent_attempts import (
    MAXIMUM_RUNNER_STANDARD_ERROR_BYTES,
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
    RunnerTerminalEvidence,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
    RunnerTerminalEvidenceReadback,
)
from atelier2.contracts.agent_transcripts import (
    MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES,
    MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    AssistantTurn,
    AttemptTranscript,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentExecutionRequestHash,
    AgentExecutionResult,
)
from atelier2.contracts.runner_manifests import CANDIDATE_JOURNAL_BYTES
from atelier2.contracts.runner_terminal_evidence_codec import (
    MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES,
    RunnerTerminalEvidenceExchangeVersionRefused,
    RunnerTerminalEvidenceRecordCorrupt,
    RunnerTerminalEvidenceRecordMissing,
    RunnerTerminalEvidenceRecordOversized,
    decode_runner_terminal_evidence_record,
    encode_runner_terminal_evidence_record,
)

_INVOCATION = RunnerInvocationId("invocation-1")


def _binding(
    generation: str = "generation-1",
) -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        AgentAttemptId("a" * 64),
        AgentExecutionRequestHash("b" * 64),
        RunnerGenerationId(generation),
        RunnerManifestId("c" * 64),
    )


def _envelope(
    evidence: RunnerTerminalEvidence,
    *,
    binding: RunnerGenerationBinding | None = None,
    invocation: RunnerInvocationId | None = _INVOCATION,
) -> RunnerTerminalEvidenceEnvelope:
    return RunnerTerminalEvidenceEnvelope(
        _binding() if binding is None else binding,
        invocation,
        evidence,
    )


def _variants() -> tuple[RunnerTerminalEvidenceEnvelope, ...]:
    return (
        _envelope(RunnerProviderResult(AgentExecutionResult(b"answer\x00bytes"))),
        _envelope(RunnerProviderFailure(ProcessExitSignature(-9, b"stderr\x00bytes"))),
        _envelope(
            RunnerOutputLimitExceeded(
                frozenset(
                    {
                        RunnerOutputStream.STANDARD_OUTPUT,
                        RunnerOutputStream.STANDARD_ERROR,
                    }
                )
            )
        ),
        _envelope(RunnerProcessBoundaryFailure()),
        _envelope(
            RunnerCancellation(
                "cancel-ä", RunnerCancellationObservation.REAPED_AFTER_TERM
            )
        ),
        _envelope(RunnerInvocationLost()),
    )


_FROZEN_V1_RECORD_HEX = (
    (
        "4154454c49455232000000002472756e6e65722d7465726d696e616c2d65766964656e63652d6578"
        "6368616e67652f76310000000000000008656e76656c6f7065000000000000004061616161616161"
        "61616161616161616161616161616161616161616161616161616161616161616161616161616161"
        "61616161616161616161616161616161610000000000000040626262626262626262626262626262"
        "62626262626262626262626262626262626262626262626262626262626262626262626262626262"
        "626262626262626262000000000000000c67656e65726174696f6e2d310000000000000040636363"
        "63636363636363636363636363636363636363636363636363636363636363636363636363636363"
        "636363636363636363636363636363636363636363000000000000000c696e766f636174696f6e2d"
        "31000000000000004066363165346235383033386239613137646636653134306635343165323031"
        "38646335613534623566323738613666343939663061623637353832333536623400000000000000"
        "0f70726f76696465722d726573756c74000000000000000c616e7377657200627974657300000000"
        "00000000"
    ),
    (
        "4154454c49455232000000002472756e6e65722d7465726d696e616c2d65766964656e63652d6578"
        "6368616e67652f76310000000000000008656e76656c6f7065000000000000004061616161616161"
        "61616161616161616161616161616161616161616161616161616161616161616161616161616161"
        "61616161616161616161616161616161610000000000000040626262626262626262626262626262"
        "62626262626262626262626262626262626262626262626262626262626262626262626262626262"
        "626262626262626262000000000000000c67656e65726174696f6e2d310000000000000040636363"
        "63636363636363636363636363636363636363636363636363636363636363636363636363636363"
        "636363636363636363636363636363636363636363000000000000000c696e766f636174696f6e2d"
        "31000000000000004030353663396438363337633530633263366437373065323665353930373937"
        "39346434633963643965656535646130613935343034643461333463633436316100000000000000"
        "1070726f76696465722d6661696c7572650000000000000008fffffffffffffff700000000000000"
        "0c737464657272006279746573"
    ),
    (
        "4154454c49455232000000002472756e6e65722d7465726d696e616c2d65766964656e63652d6578"
        "6368616e67652f76310000000000000008656e76656c6f7065000000000000004061616161616161"
        "61616161616161616161616161616161616161616161616161616161616161616161616161616161"
        "61616161616161616161616161616161610000000000000040626262626262626262626262626262"
        "62626262626262626262626262626262626262626262626262626262626262626262626262626262"
        "626262626262626262000000000000000c67656e65726174696f6e2d310000000000000040636363"
        "63636363636363636363636363636363636363636363636363636363636363636363636363636363"
        "636363636363636363636363636363636363636363000000000000000c696e766f636174696f6e2d"
        "31000000000000004061343665653065616363366431366162313530353333376430356561326263"
        "62353163383562393539636431663036623834643963626339383436623766653700000000000000"
        "156f75747075742d6c696d69742d6578636565646564000000000000000e5354414e444152445f45"
        "52524f52000000000000000f5354414e444152445f4f5554505554"
    ),
    (
        "4154454c49455232000000002472756e6e65722d7465726d696e616c2d65766964656e63652d6578"
        "6368616e67652f76310000000000000008656e76656c6f7065000000000000004061616161616161"
        "61616161616161616161616161616161616161616161616161616161616161616161616161616161"
        "61616161616161616161616161616161610000000000000040626262626262626262626262626262"
        "62626262626262626262626262626262626262626262626262626262626262626262626262626262"
        "626262626262626262000000000000000c67656e65726174696f6e2d310000000000000040636363"
        "63636363636363636363636363636363636363636363636363636363636363636363636363636363"
        "636363636363636363636363636363636363636363000000000000000c696e766f636174696f6e2d"
        "31000000000000004032306432366430323736383335636636346166623162306434376166616161"
        "64386135646663306234626536623565326461653733316530613231376535323000000000000000"
        "1870726f636573732d626f756e646172792d6661696c757265000000000000000000000000000000"
        "00"
    ),
    (
        "4154454c49455232000000002472756e6e65722d7465726d696e616c2d65766964656e63652d6578"
        "6368616e67652f76310000000000000008656e76656c6f7065000000000000004061616161616161"
        "61616161616161616161616161616161616161616161616161616161616161616161616161616161"
        "61616161616161616161616161616161610000000000000040626262626262626262626262626262"
        "62626262626262626262626262626262626262626262626262626262626262626262626262626262"
        "626262626262626262000000000000000c67656e65726174696f6e2d310000000000000040636363"
        "63636363636363636363636363636363636363636363636363636363636363636363636363636363"
        "636363636363636363636363636363636363636363000000000000000c696e766f636174696f6e2d"
        "31000000000000004035313163303066656438323633376433353930366531626665373738633230"
        "32623866656536303561616565303662353061383264653235326636626238333300000000000000"
        "0c63616e63656c6c6174696f6e000000000000000963616e63656c2dc3a400000000000000115245"
        "415045445f41465445525f5445524d"
    ),
    (
        "4154454c49455232000000002472756e6e65722d7465726d696e616c2d65766964656e63652d6578"
        "6368616e67652f76310000000000000008656e76656c6f7065000000000000004061616161616161"
        "61616161616161616161616161616161616161616161616161616161616161616161616161616161"
        "61616161616161616161616161616161610000000000000040626262626262626262626262626262"
        "62626262626262626262626262626262626262626262626262626262626262626262626262626262"
        "626262626262626262000000000000000c67656e65726174696f6e2d310000000000000040636363"
        "63636363636363636363636363636363636363636363636363636363636363636363636363636363"
        "636363636363636363636363636363636363636363000000000000000c696e766f636174696f6e2d"
        "31000000000000004033653732363433336164356230653834383535636164373033393135393862"
        "34353136626337646339366535326164663963376230653566663631333931643400000000000000"
        "0f696e766f636174696f6e2d6c6f737400000000000000000000000000000000"
    ),
    (
        "4154454c49455232000000002472756e6e65722d7465726d696e616c2d65766964656e63652d6578"
        "6368616e67652f7631000000000000000d61636b2d746f6d6273746f6e6500000000000000406161"
        "61616161616161616161616161616161616161616161616161616161616161616161616161616161"
        "61616161616161616161616161616161616161616161000000000000004062626262626262626262"
        "62626262626262626262626262626262626262626262626262626262626262626262626262626262"
        "6262626262626262626262626262000000000000000c67656e65726174696f6e2d31000000000000"
        "00406363636363636363636363636363636363636363636363636363636363636363636363636363"
        "6363636363636363636363636363636363636363636363636363000000000000000c696e766f6361"
        "74696f6e2d3100000000000000406636316534623538303338623961313764663665313430663534"
        "31653230313864633561353462356632373861366634393966306162363735383233353662340000"
        "00000000000000000000000000000000000000000000"
    ),
)


@pytest.mark.parametrize(
    "record",
    (*_variants(),),
    ids=(
        "provider-result",
        "provider-failure",
        "output-limit",
        "process-boundary",
        "cancellation",
        "invocation-lost",
    ),
)
def test_all_six_envelopes_round_trip_as_one_canonical_self_checking_record(
    record: RunnerTerminalEvidenceReadback,
) -> None:
    encoded = encode_runner_terminal_evidence_record(record)
    decoded = decode_runner_terminal_evidence_record(encoded)

    assert decoded == record
    assert isinstance(
        decoded, (RunnerTerminalEvidenceEnvelope, RunnerTerminalEvidenceAckTombstone)
    )
    assert encode_runner_terminal_evidence_record(decoded) == encoded


@pytest.mark.parametrize(
    "evidence",
    (
        RunnerProviderResult(
            AgentExecutionResult(
                b"answer",
                AttemptTranscript.of([AssistantTurn("I read the file.")]),
            )
        ),
        RunnerProviderFailure(
            ProcessExitSignature(0, b"refusal stderr"),
            AgentAttemptFailureCode.AGENT_REFUSED,
            AttemptTranscript.of([AssistantTurn("I could not comply.")]),
        ),
        RunnerProviderFailure(
            ProcessExitSignature(17, b"process stderr"),
            AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
            AttemptTranscript.of([AssistantTurn("The process stopped.")]),
        ),
    ),
    ids=("result", "agent-refused", "process-exited-unsuccessfully"),
)
def test_each_transcript_carrying_provider_outcome_round_trips_byte_canonically(
    evidence: RunnerProviderResult | RunnerProviderFailure,
) -> None:
    envelope = _envelope(evidence)

    encoded = encode_runner_terminal_evidence_record(envelope)
    decoded = decode_runner_terminal_evidence_record(encoded)

    assert decoded == envelope
    assert isinstance(decoded, RunnerTerminalEvidenceEnvelope)
    assert encode_runner_terminal_evidence_record(decoded) == encoded


def _maximum_transcript() -> AttemptTranscript:
    # Full-width steps plus this exact tail spend every canonical document byte.
    transcript = AttemptTranscript.of(
        [
            *(
                AssistantTurn("x" * MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
                for _ in range(127)
            ),
            AssistantTurn("x" * 1_237),
        ]
    )
    assert len(transcript.document) == MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES
    return transcript


def test_largest_admissible_v2_record_equals_its_codec_bound_and_fits_journal() -> None:
    maximum_utf8_identity = chr(0x10FFFF) * MAXIMUM_AGENT_FIELD_CHARACTERS
    binding = _binding(maximum_utf8_identity)
    invocation = RunnerInvocationId(maximum_utf8_identity)
    transcript = _maximum_transcript()
    result_envelope = _envelope(
        RunnerProviderResult(
            AgentExecutionResult(
                b"x" * MAXIMUM_AGENT_OUTPUT_BYTES_V2,
                transcript,
            )
        ),
        binding=binding,
        invocation=invocation,
    )
    failure_envelope = _envelope(
        RunnerProviderFailure(
            ProcessExitSignature(-(2**63), b"x" * MAXIMUM_RUNNER_STANDARD_ERROR_BYTES),
            AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
            transcript,
        ),
        binding=binding,
        invocation=invocation,
    )

    result_record = encode_runner_terminal_evidence_record(result_envelope)
    failure_record = encode_runner_terminal_evidence_record(failure_envelope)

    assert len(result_record) < len(failure_record)
    assert len(failure_record) == MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES
    assert len(result_record) <= CANDIDATE_JOURNAL_BYTES
    assert len(failure_record) <= CANDIDATE_JOURNAL_BYTES
    assert decode_runner_terminal_evidence_record(result_record) == result_envelope
    assert decode_runner_terminal_evidence_record(failure_record) == failure_envelope


def test_ack_tombstone_round_trips_without_the_provider_payload() -> None:
    provider_payload = b"answer\x00bytes"
    envelope = _envelope(RunnerProviderResult(AgentExecutionResult(provider_payload)))
    tombstone = RunnerTerminalEvidenceAckTombstone(
        envelope.binding,
        envelope.invocation_id,
        RunnerTerminalEvidenceHash.for_envelope(envelope),
    )
    encoded = encode_runner_terminal_evidence_record(tombstone)

    decoded = decode_runner_terminal_evidence_record(encoded)

    assert decoded == tombstone
    assert provider_payload not in encoded


def test_one_byte_past_the_v2_bound_refuses_before_record_parsing() -> None:
    record = b"not a frame".ljust(
        MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES + 1, b"x"
    )

    assert decode_runner_terminal_evidence_record(record) == (
        RunnerTerminalEvidenceRecordOversized()
    )


@pytest.mark.parametrize(
    ("record", "outcome"),
    (
        (None, RunnerTerminalEvidenceRecordMissing()),
        (cast(bytes | None, "not-bytes"), RunnerTerminalEvidenceRecordCorrupt()),
        (b"", RunnerTerminalEvidenceRecordCorrupt()),
    ),
    ids=("missing", "foreign-type", "empty"),
)
def test_missing_and_malformed_records_return_payload_free_typed_outcomes(
    record: bytes | None,
    outcome: object,
) -> None:
    assert decode_runner_terminal_evidence_record(record) == outcome


def _fields(record: bytes) -> list[tuple[int, int]]:
    domain_length = struct.unpack(">I", record[9:13])[0]
    cursor = 13 + domain_length
    answer: list[tuple[int, int]] = []
    while cursor < len(record):
        length = struct.unpack(">Q", record[cursor : cursor + 8])[0]
        start = cursor + 8
        answer.append((start, start + length))
        cursor = start + length
    assert cursor == len(record)
    return answer


def _replace_field(record: bytes, field: int, payload: bytes) -> bytes:
    fields = _fields(record)
    start, end = fields[field]
    return (
        record[: start - 8] + struct.pack(">Q", len(payload)) + payload + record[end:]
    )


@pytest.mark.parametrize(
    "golden_hex",
    _FROZEN_V1_RECORD_HEX,
    ids=(
        "provider-result",
        "provider-failure",
        "output-limit",
        "process-boundary",
        "cancellation",
        "invocation-lost",
        "ack-tombstone",
    ),
)
def test_every_frozen_v1_golden_vector_is_refused_without_parallel_decode(
    golden_hex: str,
) -> None:
    golden = bytes.fromhex(golden_hex)

    refusal = decode_runner_terminal_evidence_record(golden)

    assert isinstance(refusal, RunnerTerminalEvidenceExchangeVersionRefused)
    assert refusal.refused_exchange == "runner-terminal-evidence-exchange/v1"
    assert refusal.reason == (
        "runner-terminal-evidence-exchange/v1 was refused because only "
        "runner-terminal-evidence-exchange/v2 is supported"
    )


def test_semantically_valid_but_unsorted_output_limit_record_is_corrupt() -> None:
    canonical = encode_runner_terminal_evidence_record(_variants()[2])
    unsorted = _replace_field(
        _replace_field(canonical, 8, b"STANDARD_OUTPUT"),
        9,
        b"STANDARD_ERROR",
    )

    assert decode_runner_terminal_evidence_record(unsorted) == (
        RunnerTerminalEvidenceRecordCorrupt()
    )


def test_envelope_frame_owns_the_exact_domain_field_order_and_tags() -> None:
    envelope = _envelope(RunnerProviderResult(AgentExecutionResult(b"answer")))
    encoded = encode_runner_terminal_evidence_record(envelope)
    fields = tuple(encoded[start:end] for start, end in _fields(encoded))

    assert encoded.startswith(
        b"ATELIER2\x00\x00\x00\x00$runner-terminal-evidence-exchange/v2"
    )
    assert fields == (
        b"envelope",
        b"a" * 64,
        b"b" * 64,
        b"generation-1",
        b"c" * 64,
        b"invocation-1",
        RunnerTerminalEvidenceHash.for_envelope(envelope).value.encode("ascii"),
        b"provider-result",
        b"answer",
        b"",
        b"",
        b"",
        b"",
        b"absent",
        b"",
    )


def test_transcript_failure_frame_freezes_every_v2_evidence_slot() -> None:
    transcript = AttemptTranscript.of([AssistantTurn("The provider refused.")])
    envelope = _envelope(
        RunnerProviderFailure(
            ProcessExitSignature(17, b"provider stderr"),
            AgentAttemptFailureCode.AGENT_REFUSED,
            transcript,
        )
    )
    encoded = encode_runner_terminal_evidence_record(envelope)
    fields = tuple(encoded[start:end] for start, end in _fields(encoded))

    assert fields == (
        b"envelope",
        b"a" * 64,
        b"b" * 64,
        b"generation-1",
        b"c" * 64,
        b"invocation-1",
        RunnerTerminalEvidenceHash.for_envelope(envelope).value.encode("ascii"),
        b"provider-failure",
        b"",
        b"",
        b"AGENT_REFUSED",
        struct.pack(">q", 17),
        b"provider stderr",
        b"present",
        transcript.document,
    )


@pytest.mark.parametrize(
    ("field", "payload"),
    (
        (10, b"OUTPUT_SCHEMA_REFUSED"),
        (13, b"unknown"),
        (
            14,
            (
                b'{ "kind":"attempt-transcript/v1","events":['
                b'{"event":"assistant-turn","text":"read","redacted":false}]}'
            ),
        ),
    ),
    ids=("non-admitted-failure-code", "transcript-presence", "transcript-document"),
)
def test_provider_failure_decoder_refuses_each_noncanonical_v2_field(
    field: int,
    payload: bytes,
) -> None:
    canonical = encode_runner_terminal_evidence_record(
        _envelope(
            RunnerProviderFailure(
                ProcessExitSignature(17, b"provider stderr"),
                AgentAttemptFailureCode.AGENT_REFUSED,
                AttemptTranscript.of([AssistantTurn("read")]),
            )
        )
    )

    assert (
        decode_runner_terminal_evidence_record(
            _replace_field(canonical, field, payload)
        )
        == RunnerTerminalEvidenceRecordCorrupt()
    )


@pytest.mark.parametrize(
    "mutate",
    (
        lambda record: b"X" + record[1:],
        lambda record: _replace_field(record, 0, b"unknown-record"),
        lambda record: _replace_field(record, 1, b"A" * 64),
        lambda record: _replace_field(record, 3, b"\xff"),
        lambda record: _replace_field(record, 6, b"0" * 64),
        lambda record: _replace_field(record, 7, b"unknown-variant"),
        lambda record: _replace_field(record, 9, b"unexpected"),
        lambda record: record + b"remainder",
        lambda record: record[:-1],
    ),
    ids=(
        "prefix",
        "record-tag",
        "binding",
        "strict-utf8",
        "semantic-hash",
        "variant-tag",
        "nonempty-slot",
        "remainder",
        "declared-length",
    ),
)
def test_every_noncanonical_or_self_inconsistent_mutation_is_corrupt(
    mutate: Callable[[bytes], bytes],
) -> None:
    encoded = encode_runner_terminal_evidence_record(_variants()[0])

    assert decode_runner_terminal_evidence_record(mutate(encoded)) == (
        RunnerTerminalEvidenceRecordCorrupt()
    )


def test_tombstone_requires_its_exact_tag_and_eight_empty_payload_slots() -> None:
    envelope = _variants()[0]
    tombstone = RunnerTerminalEvidenceAckTombstone(
        envelope.binding,
        envelope.invocation_id,
        RunnerTerminalEvidenceHash.for_envelope(envelope),
    )
    encoded = encode_runner_terminal_evidence_record(tombstone)

    for field in range(7, 15):
        assert (
            decode_runner_terminal_evidence_record(
                _replace_field(encoded, field, b"unexpected")
            )
            == RunnerTerminalEvidenceRecordCorrupt()
        )


@pytest.mark.parametrize(
    "construct",
    (
        lambda invalid: RunnerGenerationId(invalid),
        lambda invalid: RunnerInvocationId(invalid),
        lambda invalid: RunnerCancellation(
            invalid, RunnerCancellationObservation.NEVER_LAUNCHED
        ),
    ),
    ids=("generation", "invocation", "cancellation-command"),
)
def test_runner_record_text_owners_refuse_non_utf8_encodable_values(
    construct: Callable[[str], object],
) -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        construct("\ud800")


def test_a_result_with_no_transcript_still_round_trips_byte_for_byte() -> None:
    envelope = _envelope(RunnerProviderResult(AgentExecutionResult(b"answer")))
    record = encode_runner_terminal_evidence_record(envelope)

    decoded = decode_runner_terminal_evidence_record(record)

    assert decoded == envelope
    assert isinstance(decoded, RunnerTerminalEvidenceEnvelope)
    assert isinstance(decoded.evidence, RunnerProviderResult)
    assert decoded.evidence.result.transcript is None
    assert encode_runner_terminal_evidence_record(decoded) == record
