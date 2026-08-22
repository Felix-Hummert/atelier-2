from __future__ import annotations

from dataclasses import dataclass

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.application.run_runner_session import (
    RunnerSessionRefusal,
    cancellation_refusal_code,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptReplacement,
    CancelAgentAttemptRequest,
    RunnerGenerationBinding,
    RunnerInvocationId,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.runner_terminal_evidence_codec import (
    decode_runner_terminal_evidence_record,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    RunnerTerminalEvidenceCommitRefused,
)


@dataclass(frozen=True)
class DbosRunnerSessionCore:
    """Bind one authenticated session to the existing durable attempt owners."""

    execution: AgentAttemptExecution
    store: DbosAgentAttemptStore
    cancellation_command_id: str

    def arm(
        self, binding: RunnerGenerationBinding, invocation: RunnerInvocationId
    ) -> None:
        self.store.arm_runner_invocation(self.execution, binding, invocation)

    def commit_terminal_record(
        self, binding: RunnerGenerationBinding, record: bytes
    ) -> RunnerTerminalEvidenceHash:
        decoded = decode_runner_terminal_evidence_record(record)
        if not isinstance(decoded, RunnerTerminalEvidenceEnvelope):
            raise TypeError("runner-terminal-record-corrupt")
        if decoded.binding != binding:
            raise ValueError("runner-terminal-record-corrupt")
        committed = self.store.commit_runner_terminal_evidence(self.execution, decoded)
        if isinstance(committed, RunnerTerminalEvidenceCommitRefused):
            raise TypeError("runner-terminal-record-refused")
        return committed.evidence_hash

    def acknowledge(
        self,
        binding: RunnerGenerationBinding,
        evidence_hash: RunnerTerminalEvidenceHash,
        tombstone: bytes,
    ) -> None:
        decoded = decode_runner_terminal_evidence_record(tombstone)
        if not isinstance(decoded, RunnerTerminalEvidenceAckTombstone):
            raise TypeError("runner-ack-hash-mismatch")
        if decoded.binding != binding or decoded.evidence_hash != evidence_hash:
            raise ValueError("runner-ack-hash-mismatch")
        self.store.mark_runner_evidence_acknowledged(self.execution, decoded)

    def cancel(self) -> CancelAgentAttemptRequest:
        attempt = self.store.load(self.execution.attempt_id)
        request = CancelAgentAttemptRequest(
            self.execution.request.run_id,
            self.execution.attempt_id,
            self.cancellation_command_id,
            attempt.state_version,
            AgentAttemptReplacement.NONE,
        )
        result = self.store.request_cancellation(request)
        if isinstance(result, AgentAttemptCancellationAccepted):
            return request
        raise RunnerSessionRefusal(cancellation_refusal_code(result))
