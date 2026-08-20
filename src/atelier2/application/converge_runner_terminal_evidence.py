from __future__ import annotations

from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    RunnerGenerationBinding,
    RunnerTerminalEvidenceAckTombstone,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.ports.agent_attempts import (
    RunnerTerminalEvidenceCommitRefused,
    RunnerTerminalEvidenceSource,
    RunnerTerminalEvidenceStore,
)


def converge_runner_terminal_evidence(
    execution: AgentAttemptExecution,
    binding: RunnerGenerationBinding,
    source: RunnerTerminalEvidenceSource,
    store: RunnerTerminalEvidenceStore,
) -> AgentAttempt | RunnerTerminalEvidenceCommitRefused:
    readback = source.readback(binding)
    if isinstance(readback, RunnerTerminalEvidenceAckTombstone):
        return store.mark_runner_evidence_acknowledged(execution, readback)

    committed = store.commit_runner_terminal_evidence(execution, readback)
    if isinstance(committed, RunnerTerminalEvidenceCommitRefused):
        return committed
    tombstone = source.acknowledge(readback, committed.evidence_hash)
    return store.mark_runner_evidence_acknowledged(execution, tombstone)
