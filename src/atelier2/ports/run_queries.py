from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.effects import (
    EffectIntentSnapshot,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId
from atelier2.contracts.workflows import AnyWorkflowGraph
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
    QueryDurableStateCorrupt,
    ReadUnavailable,
)


@dataclass(frozen=True)
class WaitingReconciliationProjection:
    intent: EffectIntentSnapshot
    pending_command: ReconcileCommandSnapshot | None


@dataclass(frozen=True)
class AgentAttemptCancellationProjection:
    command_id: str
    replacement: AgentAttemptReplacement
    redrive_state: AgentAttemptRedriveState
    disposition: AgentAttemptCancellationDisposition | None


@dataclass(frozen=True)
class AgentAttemptProjection:
    attempt_id: AgentAttemptId
    node_execution_id: NodeExecutionId
    request_hash: AgentExecutionRequestHash
    attempt_ordinal: int
    state: str
    failure_code: AgentAttemptFailureCode | None
    cancellation: AgentAttemptCancellationProjection | None = None


@dataclass(frozen=True)
class RunProjection:
    run: AnyRun
    graph: AnyWorkflowGraph
    reconciliation: WaitingReconciliationProjection | None
    agent_attempts: tuple[AgentAttemptProjection, ...] = ()

    @property
    def current_agent_attempt(self) -> AgentAttemptProjection | None:
        return self.agent_attempts[-1] if self.agent_attempts else None


@dataclass(frozen=True)
class RunFound:
    projection: RunProjection


@dataclass(frozen=True)
class RunQueryMissing:
    pass


@dataclass(frozen=True)
class RunPage:
    runs: tuple[RunProjection, ...]
    next_after: RunId | None


@dataclass(frozen=True)
class ReconciliationRetryTargetFound:
    intent: EffectIntentSnapshot


@dataclass(frozen=True)
class ReconciliationRetryTargetMissing:
    pass


@dataclass(frozen=True)
class ReconciliationRetryCommandConflict:
    pass


type GetRunResult = (
    RunFound | RunQueryMissing | ReadUnavailable | QueryDurableStateCorrupt
)
type ListRunsResult = RunPage | ReadUnavailable | QueryDurableStateCorrupt
type GetReconciliationRetryTargetResult = (
    ReconciliationRetryTargetFound
    | ReconciliationRetryTargetMissing
    | ReconciliationRetryCommandConflict
    | RunQueryMissing
    | ReadUnavailable
    | QueryDurableStateCorrupt
)


class RunQueries(Protocol):
    def get_run(
        self,
        run_id: RunId,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetRunResult: ...

    def list_runs(
        self,
        after: RunId | None,
        limit: int,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ListRunsResult: ...

    def get_reconciliation_retry_target(
        self,
        run_id: RunId,
        command_id: ReconcileCommandId,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetReconciliationRetryTargetResult: ...
