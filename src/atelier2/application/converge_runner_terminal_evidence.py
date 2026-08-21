from __future__ import annotations

from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    RunnerGenerationBinding,
    RunnerTerminalEvidenceAckTombstone,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.runner_terminal_evidence_codec import (
    RunnerTerminalEvidenceRecordCorrupt,
    RunnerTerminalEvidenceRecordMissing,
    RunnerTerminalEvidenceRecordOversized,
)
from atelier2.ports.agent_attempts import (
    RunnerTerminalEvidenceAcknowledgementUnavailable,
    RunnerTerminalEvidenceCommitRefused,
    RunnerTerminalEvidenceSource,
    RunnerTerminalEvidenceStore,
)

type RunnerTerminalEvidenceConvergenceResult = (
    AgentAttempt
    | RunnerTerminalEvidenceCommitRefused
    | RunnerTerminalEvidenceRecordMissing
    | RunnerTerminalEvidenceRecordCorrupt
    | RunnerTerminalEvidenceRecordOversized
    | RunnerTerminalEvidenceAcknowledgementUnavailable
)


def converge_runner_terminal_evidence(
    execution: AgentAttemptExecution,
    binding: RunnerGenerationBinding,
    source: RunnerTerminalEvidenceSource,
    store: RunnerTerminalEvidenceStore,
) -> RunnerTerminalEvidenceConvergenceResult:
    readback = source.readback(binding)
    if isinstance(
        readback,
        (
            RunnerTerminalEvidenceRecordMissing,
            RunnerTerminalEvidenceRecordCorrupt,
            RunnerTerminalEvidenceRecordOversized,
        ),
    ):
        return readback
    if isinstance(readback, RunnerTerminalEvidenceAckTombstone):
        return store.mark_runner_evidence_acknowledged(execution, readback)

    committed = store.commit_runner_terminal_evidence(execution, readback)
    if isinstance(committed, RunnerTerminalEvidenceCommitRefused):
        return committed
    tombstone = source.acknowledge(readback, committed.evidence_hash)
    if isinstance(tombstone, RunnerTerminalEvidenceAcknowledgementUnavailable):
        return tombstone
    return store.mark_runner_evidence_acknowledged(execution, tombstone)
