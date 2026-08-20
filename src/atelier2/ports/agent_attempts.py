from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    ProcessExitSignature,
    WatchdogGenerationId,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.tool_grants_v3 import ToolRedemptionReceipt
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


class AgentAttemptReader(Protocol):
    """Read one durable attempt back: all that workspace reconciliation needs."""

    def load(self, attempt_id: AgentAttemptId) -> AgentAttempt: ...


class AgentAttemptStore(AgentAttemptReader, Protocol):
    def iter_driverless_attempts(self, page_limit: PageLimit) -> Iterator[AgentAttempt]:
        """Every nonterminal attempt no live workflow is driving any more.

        Answered by the durable runtime, because only it knows which of its
        workflows are still going to run. An attempt whose driver is merely
        waiting to be recovered is *not* driverless: recovery will move it, and
        stopping it would take work away from the machine that owns it.

        The iteration reads one bounded page at a time. Its cursor is local to
        this call, so a restart begins again from durable truth rather than from
        scan progress that never became product state.
        """
        ...

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt: ...

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimResult: ...

    def complete_success(
        self,
        execution: AgentAttemptExecution,
        result: AgentExecutionResult,
        redemption: ToolRedemptionReceipt | None = None,
    ) -> AgentAttemptSucceeded | AgentAttemptFailed:
        """Keep this attempt's terminal truth, and what its grant redeemed with it.

        `redemption` is absent for an attempt whose node pinned no tool grant. One
        that redeemed a grant hands its evidence in here rather than writing it
        beside this call, so a succeeded attempt and the proof of what its tool
        ran become durable together or not at all.

        A decoded result whose bytes the node's own pinned schema refuses is not
        an error of this call: the attempt ends `FAILED` under
        `OUTPUT_SCHEMA_REFUSED` with the refusal durably named, and the failed
        outcome is returned rather than raised. A granted project verification
        that exits nonzero is the same kind of named failure, under
        `PROJECT_VERIFICATION_FAILED`, with how the command ended in the receipt
        reason and without a `tool_redemptions` row.
        """
        ...

    def complete_known_failure(
        self, execution: AgentAttemptExecution, exit_signature: ProcessExitSignature
    ) -> AgentAttemptFailed:
        """End this attempt on the process that produced no usable answer.

        `exit_signature` is what the supervision saw -- how the child ended and
        the standard error it left -- and it is durably named in the node
        receipt this write keeps, because otherwise the only record of why a
        provider died is a log line nobody kept.
        """
        ...

    def complete_project_verification_failure(
        self, execution: AgentAttemptExecution, verdict: str
    ) -> AgentAttemptFailed:
        """End an armed attempt whose granted verification never produced an exit.

        `verdict` names why -- the declared timeout, not an invented exit code.
        The attempt ends on the same `PROJECT_VERIFICATION_FAILED` seam a
        nonzero exit uses, without a `tool_redemptions` row.
        """
        ...

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


class TransactionalAgentAttemptCanceller(Protocol):
    def request_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationResult: ...
