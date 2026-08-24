"""What Serve reads back off a launcher's retained terminal record (`#585`).

The launcher lays a Runner's own journal record down verbatim in the handoff;
this is the read side. The tests write exactly the bytes the launcher's
`read_file_in_volume` would copy -- a canonical codec record -- and prove Serve
decodes precisely what the Runner wrote, and answers every other shape with a
named codec non-result rather than a guess.
"""

from __future__ import annotations

from pathlib import Path

from atelier2.adapters.file_runner_terminal_evidence import (
    FileRunnerTerminalEvidenceSource,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
    RunnerProviderResult,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import AgentExecutionRequestHash, AgentExecutionResult
from atelier2.contracts.runner_terminal_evidence_codec import (
    MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES,
    RunnerTerminalEvidenceRecordCorrupt,
    RunnerTerminalEvidenceRecordMissing,
    RunnerTerminalEvidenceRecordOversized,
    encode_runner_terminal_evidence_record,
)

_ATTEMPT = "a" * 64
_OTHER_ATTEMPT = "b" * 64


def _binding(attempt_id: str = _ATTEMPT) -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        AgentAttemptId(attempt_id),
        AgentExecutionRequestHash("c" * 64),
        RunnerGenerationId("runner-generation-1"),
        RunnerManifestId("d" * 64),
    )


def _envelope(binding: RunnerGenerationBinding) -> RunnerTerminalEvidenceEnvelope:
    return RunnerTerminalEvidenceEnvelope(
        binding,
        RunnerInvocationId("runner-invocation-1"),
        RunnerProviderResult(AgentExecutionResult(b'{"answer": "done"}')),
    )


def _retain(attempts_root: Path, attempt_id: str, payload: bytes) -> None:
    handoff = attempts_root / attempt_id / "handoff"
    handoff.mkdir(mode=0o700, parents=True)
    (handoff / "retained-terminal-record").write_bytes(payload)


def test_a_retained_envelope_reads_back_as_the_runner_wrote_it(tmp_path: Path) -> None:
    binding = _binding()
    envelope = _envelope(binding)
    _retain(tmp_path, _ATTEMPT, encode_runner_terminal_evidence_record(envelope))

    assert FileRunnerTerminalEvidenceSource(tmp_path).readback(binding) == envelope


def test_a_retained_ack_tombstone_reads_back(tmp_path: Path) -> None:
    binding = _binding()
    envelope = _envelope(binding)
    tombstone = RunnerTerminalEvidenceAckTombstone(
        binding,
        envelope.invocation_id,
        RunnerTerminalEvidenceHash.for_envelope(envelope),
    )
    _retain(tmp_path, _ATTEMPT, encode_runner_terminal_evidence_record(tombstone))

    assert FileRunnerTerminalEvidenceSource(tmp_path).readback(binding) == tombstone


def test_no_retained_record_reads_back_as_missing(tmp_path: Path) -> None:
    assert isinstance(
        FileRunnerTerminalEvidenceSource(tmp_path).readback(_binding()),
        RunnerTerminalEvidenceRecordMissing,
    )


def test_garbage_bytes_read_back_as_corrupt(tmp_path: Path) -> None:
    _retain(tmp_path, _ATTEMPT, b"not a canonical record")

    assert isinstance(
        FileRunnerTerminalEvidenceSource(tmp_path).readback(_binding()),
        RunnerTerminalEvidenceRecordCorrupt,
    )


def test_a_record_over_the_bound_reads_back_as_oversized(tmp_path: Path) -> None:
    _retain(
        tmp_path,
        _ATTEMPT,
        b"x" * (MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES + 1),
    )

    assert isinstance(
        FileRunnerTerminalEvidenceSource(tmp_path).readback(_binding()),
        RunnerTerminalEvidenceRecordOversized,
    )


def test_a_record_for_another_generation_is_refused_as_corrupt(tmp_path: Path) -> None:
    """A record whose binding is another Attempt's is never read as this one's
    terminal fact -- committing it would converge a lie."""
    other = _binding(_OTHER_ATTEMPT)
    _retain(
        tmp_path,
        _ATTEMPT,
        encode_runner_terminal_evidence_record(_envelope(other)),
    )

    assert isinstance(
        FileRunnerTerminalEvidenceSource(tmp_path).readback(_binding()),
        RunnerTerminalEvidenceRecordCorrupt,
    )


def test_acknowledge_yields_the_tombstone_for_a_gone_runner(tmp_path: Path) -> None:
    binding = _binding()
    envelope = _envelope(binding)
    accepted = RunnerTerminalEvidenceHash.for_envelope(envelope)

    tombstone = FileRunnerTerminalEvidenceSource(tmp_path).acknowledge(
        envelope, accepted
    )

    assert tombstone == RunnerTerminalEvidenceAckTombstone(
        binding, envelope.invocation_id, accepted
    )
