from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    WatchdogGenerationId,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.workflows import NodeCompletion
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


@dataclass(frozen=True)
class AgentAttemptClaimedByThisCall:
    attempt: AgentAttempt


@dataclass(frozen=True)
class AgentAttemptSucceeded:
    attempt: AgentAttempt
    completion: NodeCompletion


@dataclass(frozen=True)
class AgentAttemptFailed:
    attempt: AgentAttempt


@dataclass(frozen=True)
class AgentAttemptPossiblyRan:
    attempt: AgentAttempt


type AgentAttemptClaimResult = (
    AgentAttemptClaimedByThisCall
    | AgentAttemptSucceeded
    | AgentAttemptFailed
    | AgentAttemptPossiblyRan
)
type AgentAttemptExecutionOutcome = (
    AgentAttemptSucceeded | AgentAttemptFailed | AgentAttemptPossiblyRan
)


@dataclass(frozen=True)
class AgentAttemptCancellationAccepted:
    attempt: AgentAttempt
    terminal: bool
    replacement_attempt_id: AgentAttemptId | None = None


@dataclass(frozen=True)
class AgentAttemptCancellationRunMissing:
    pass


@dataclass(frozen=True)
class AgentAttemptCancellationTargetMissing:
    pass


@dataclass(frozen=True)
class AgentAttemptCancellationNotCurrent:
    pass


@dataclass(frozen=True)
class AgentAttemptCancellationStale:
    pass


@dataclass(frozen=True)
class AgentAttemptCancellationTerminalConflict:
    pass


@dataclass(frozen=True)
class AgentAttemptCancellationCommandConflict:
    pass


@dataclass(frozen=True)
class AgentAttemptReplacementNotAllowed:
    pass


type AgentAttemptCancellationResult = (
    AgentAttemptCancellationAccepted
    | AgentAttemptCancellationRunMissing
    | AgentAttemptCancellationTargetMissing
    | AgentAttemptCancellationNotCurrent
    | AgentAttemptCancellationStale
    | AgentAttemptCancellationTerminalConflict
    | AgentAttemptCancellationCommandConflict
    | AgentAttemptReplacementNotAllowed
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class AgentAttemptStore(Protocol):
    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt: ...

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimResult: ...

    def complete_success(
        self, execution: AgentAttemptExecution, result: AgentExecutionResult
    ) -> AgentAttemptSucceeded: ...

    def complete_known_failure(
        self, execution: AgentAttemptExecution
    ) -> AgentAttemptFailed: ...

    def request_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationResult: ...

    def attest_cancellation_cleanup(
        self,
        request: CancelAgentAttemptRequest,
        disposition: AgentAttemptCancellationDisposition,
        process_owner_id: AgentProcessOwnerId | None,
        watchdog_generation_id: WatchdogGenerationId | None,
    ) -> AgentAttemptCancellationAccepted: ...

    def mark_cancellation_owner_not_local(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttempt: ...

    def bind_watchdog(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt: ...

    def observe_process(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt: ...

    def load(self, attempt_id: AgentAttemptId) -> AgentAttempt: ...


class TransactionalAgentAttemptCanceller(Protocol):
    def request_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationResult: ...
