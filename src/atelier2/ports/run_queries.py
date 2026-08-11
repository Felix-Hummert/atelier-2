from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.effects import (
    EffectIntentSnapshot,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
)
from atelier2.contracts.runs import Run, RunId
from atelier2.contracts.workflows import WorkflowGraph
from atelier2.ports.workflow_revisions import QueryDurableStateCorrupt, ReadUnavailable


@dataclass(frozen=True)
class WaitingReconciliationProjection:
    intent: EffectIntentSnapshot
    pending_command: ReconcileCommandSnapshot | None


@dataclass(frozen=True)
class RunProjection:
    run: Run
    graph: WorkflowGraph
    reconciliation: WaitingReconciliationProjection | None


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
    def get_run(self, run_id: RunId) -> GetRunResult: ...

    def list_runs(self, after: RunId | None, limit: int) -> ListRunsResult: ...

    def get_reconciliation_retry_target(
        self, run_id: RunId, command_id: ReconcileCommandId
    ) -> GetReconciliationRetryTargetResult: ...
