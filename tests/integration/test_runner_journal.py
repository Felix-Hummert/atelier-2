from __future__ import annotations

from pathlib import Path

import pytest

from atelier2.adapters.runner_journal import RunnerJournal
from atelier2.application.run_runner_session import (
    RunnerSessionRefusal,
    require_matching_evidence_hash,
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
from atelier2.contracts.runner_manifests import CANDIDATE_JOURNAL_BYTES
from atelier2.contracts.runner_terminal_evidence_codec import (
    encode_runner_terminal_evidence_record,
)


def _envelope() -> RunnerTerminalEvidenceEnvelope:
    return RunnerTerminalEvidenceEnvelope(
        RunnerGenerationBinding(
            AgentAttemptId("a" * 64),
            AgentExecutionRequestHash("b" * 64),
            RunnerGenerationId("generation"),
            RunnerManifestId("c" * 64),
        ),
        RunnerInvocationId("invocation"),
        RunnerProviderResult(AgentExecutionResult(b"complete")),
    )


@pytest.mark.proves("runner-journal-release")
def test_journal_retains_one_envelope_then_only_its_ack_tombstone(
    tmp_path: Path,
) -> None:
    journal = RunnerJournal(tmp_path)
    envelope = _envelope()

    journal.publish(envelope, CANDIDATE_JOURNAL_BYTES)
    evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
    tombstone = journal.acknowledge(envelope, evidence_hash)

    assert journal.readback(envelope.binding) == tombstone
    assert [entry.name for entry in tmp_path.iterdir()] == ["terminal-record"]


def test_wrong_ack_preserves_the_exact_envelope(tmp_path: Path) -> None:
    journal = RunnerJournal(tmp_path)
    envelope = _envelope()
    journal.publish(envelope, CANDIDATE_JOURNAL_BYTES)

    with pytest.raises(ValueError, match="runner-ack-hash-mismatch"):
        journal.acknowledge(envelope, RunnerTerminalEvidenceHash("d" * 64))

    assert journal.readback(envelope.binding) == envelope


def test_release_removes_only_the_acknowledged_tombstone(tmp_path: Path) -> None:
    journal = RunnerJournal(tmp_path)
    envelope = _envelope()
    journal.publish(envelope, CANDIDATE_JOURNAL_BYTES)
    evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
    journal.acknowledge(envelope, evidence_hash)

    journal.release(envelope.binding, evidence_hash)

    assert list(tmp_path.iterdir()) == []


def test_release_before_ack_preserves_the_envelope(tmp_path: Path) -> None:
    journal = RunnerJournal(tmp_path)
    envelope = _envelope()
    journal.publish(envelope, CANDIDATE_JOURNAL_BYTES)

    with pytest.raises(TypeError, match="runner-release-before-ack"):
        journal.release(
            envelope.binding, RunnerTerminalEvidenceHash.for_envelope(envelope)
        )

    assert journal.readback(envelope.binding) == envelope


def test_ack_and_release_payload_mismatch_keeps_the_envelope(tmp_path: Path) -> None:
    journal = RunnerJournal(tmp_path)
    envelope = _envelope()
    journal.publish(envelope, CANDIDATE_JOURNAL_BYTES)
    retained = RunnerTerminalEvidenceHash.for_envelope(envelope)

    with pytest.raises(RunnerSessionRefusal, match="runner-ack-hash-mismatch"):
        require_matching_evidence_hash((b"0" * 64,), retained)
        journal.acknowledge(envelope, retained)
    assert journal.readback(envelope.binding) == envelope

    journal.acknowledge(envelope, retained)
    with pytest.raises(RunnerSessionRefusal, match="runner-ack-hash-mismatch"):
        require_matching_evidence_hash((b"0" * 64,), retained)
        journal.release(envelope.binding, retained)
    record = journal.readback(envelope.binding)
    assert isinstance(record, RunnerTerminalEvidenceAckTombstone)
    assert record.evidence_hash == retained


def test_publish_accepts_a_record_exactly_at_its_declared_bound(
    tmp_path: Path,
) -> None:
    journal = RunnerJournal(tmp_path)
    envelope = _envelope()
    exact_bound = len(encode_runner_terminal_evidence_record(envelope))

    journal.publish(envelope, exact_bound)

    assert journal.readback(envelope.binding) == envelope


def test_publish_refuses_a_record_that_exceeds_its_declared_bound(
    tmp_path: Path,
) -> None:
    """A tmpfs mount used to refuse an oversized write at the filesystem
    layer; the durable volume `#15-B5` moved the journal onto does not, so
    `publish` is the one remaining place that bound stays enforced."""
    journal = RunnerJournal(tmp_path)
    envelope = _envelope()
    encoded_length = len(encode_runner_terminal_evidence_record(envelope))

    with pytest.raises(ValueError, match="runner-journal-record-exceeds-bound"):
        journal.publish(envelope, encoded_length - 1)

    with pytest.raises(ValueError, match="runner-terminal-record-missing"):
        journal.readback(envelope.binding)
