from __future__ import annotations

from pathlib import Path

import pytest

from atelier2.adapters.runner_journal import RunnerJournal
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
    RunnerProviderResult,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import AgentExecutionRequestHash, AgentExecutionResult


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

    journal.publish(envelope)
    evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
    tombstone = journal.acknowledge(envelope, evidence_hash)

    assert journal.readback(envelope.binding) == tombstone
    assert [entry.name for entry in tmp_path.iterdir()] == ["terminal-record"]


def test_wrong_ack_preserves_the_exact_envelope(tmp_path: Path) -> None:
    journal = RunnerJournal(tmp_path)
    envelope = _envelope()
    journal.publish(envelope)

    with pytest.raises(ValueError, match="runner-ack-hash-mismatch"):
        journal.acknowledge(envelope, RunnerTerminalEvidenceHash("d" * 64))

    assert journal.readback(envelope.binding) == envelope


def test_release_removes_only_the_acknowledged_tombstone(tmp_path: Path) -> None:
    journal = RunnerJournal(tmp_path)
    envelope = _envelope()
    journal.publish(envelope)
    evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
    journal.acknowledge(envelope, evidence_hash)

    journal.release(envelope.binding, evidence_hash)

    assert list(tmp_path.iterdir()) == []


def test_release_before_ack_preserves_the_envelope(tmp_path: Path) -> None:
    journal = RunnerJournal(tmp_path)
    envelope = _envelope()
    journal.publish(envelope)

    with pytest.raises(TypeError, match="runner-release-before-ack"):
        journal.release(
            envelope.binding, RunnerTerminalEvidenceHash.for_envelope(envelope)
        )

    assert journal.readback(envelope.binding) == envelope
