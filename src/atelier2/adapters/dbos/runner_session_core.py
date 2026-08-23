from __future__ import annotations

from dataclasses import dataclass, field

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.application.run_runner_session import (
    RunnerSessionRefusal,
    cancellation_refusal_code,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptReplacement,
    CancelAgentAttemptRequest,
    RunnerEvidenceAcceptancePhase,
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
    RunnerTerminalEvidenceCommitted,
)


@dataclass(frozen=True)
class DbosRunnerSessionCore:
    """Bind one authenticated session to the existing durable attempt owners."""

    execution: AgentAttemptExecution
    store: DbosAgentAttemptStore
    cancellation_command_id: str
    # A single-item box, not a plain attribute: the dataclass is frozen (its
    # wire-facing fields must never be reassigned mid-session), but the one
    # commit `commit_terminal_record` ever durably wins still has to reach a
    # caller driving this session -- `#540` C-3.4's own Core-side driver reads
    # `committed_evidence` once RELEASED, for the `NodeCompletion`
    # `commit_runner_terminal_evidence` already computed and this class used
    # to discard along with the rest of the store's answer.
    _committed: list[RunnerTerminalEvidenceCommitted | None] = field(
        default_factory=lambda: [None], repr=False, compare=False
    )

    @property
    def committed_evidence(self) -> RunnerTerminalEvidenceCommitted | None:
        """What this session's one terminal-record commit durably won, once it
        has happened -- `None` before `commit_terminal_record` is ever called,
        or on the resumed-tombstone replay `_require_already_acknowledged`
        answers without a fresh store commit."""
        return self._committed[0]

    def arm(
        self, binding: RunnerGenerationBinding, invocation: RunnerInvocationId
    ) -> None:
        self.store.arm_runner_invocation(self.execution, binding, invocation)

    def commit_terminal_record(
        self, binding: RunnerGenerationBinding, record: bytes
    ) -> RunnerTerminalEvidenceHash:
        decoded = decode_runner_terminal_evidence_record(record)
        if isinstance(decoded, RunnerTerminalEvidenceAckTombstone):
            return self._require_already_acknowledged(binding, decoded)
        if not isinstance(decoded, RunnerTerminalEvidenceEnvelope):
            raise TypeError("runner-terminal-record-corrupt")
        if decoded.binding != binding:
            raise ValueError("runner-terminal-record-corrupt")
        committed = self.store.commit_runner_terminal_evidence(self.execution, decoded)
        if isinstance(committed, RunnerTerminalEvidenceCommitRefused):
            raise TypeError("runner-terminal-record-refused")
        self._committed[0] = committed
        return committed.evidence_hash

    def _require_already_acknowledged(
        self,
        binding: RunnerGenerationBinding,
        tombstone: RunnerTerminalEvidenceAckTombstone,
    ) -> RunnerTerminalEvidenceHash:
        """Resume's answer when the candidate's journal already tombstoned this.

        A resumed candidate whose journal already collected its ACK before it
        died has no envelope left to resend -- only the tombstone the durable
        attempt record can confirm. Anything else is a foreign or premature
        claim, never a fresh evidence commit in its own right.

        The durable attempt may still read `CORE_COMMITTED` rather than
        `ACKNOWLEDGED`: the candidate tombstones its own journal the moment
        it receives Core's `ACK` -- proof Core already durably committed this
        exact hash -- but Core itself only advances to `ACKNOWLEDGED` once it
        later processes the `ACK_TOMBSTONE` frame the tombstone accompanies.
        A candidate that dies in that exact window must not be refused
        forever; the resumed `ACK_TOMBSTONE` this tombstone travels with is
        what carries Core the rest of the way, through the store's own
        already-idempotent `mark_runner_evidence_acknowledged`.
        """
        if tombstone.binding != binding:
            raise ValueError("runner-terminal-record-corrupt")
        attempt = self.store.load(self.execution.attempt_id)
        if (
            attempt.runner_invocation_id != tombstone.invocation_id
            or attempt.runner_terminal_evidence_hash != tombstone.evidence_hash
            or attempt.runner_evidence_acceptance_phase
            not in (
                RunnerEvidenceAcceptancePhase.CORE_COMMITTED,
                RunnerEvidenceAcceptancePhase.ACKNOWLEDGED,
            )
        ):
            raise TypeError("runner-terminal-record-refused")
        return tombstone.evidence_hash

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
