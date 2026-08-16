from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.effects import (
    EffectIntentSnapshot,
    ReconcileCommandId,
)
from atelier2.contracts.run_projections import RunPage, RunProjection
from atelier2.contracts.runs import RunId
from atelier2.ports.workflow_revisions import (
    ProjectionTooLarge,
    QueryDurableStateCorrupt,
    ReadUnavailable,
)


@dataclass(frozen=True)
class RunFound:
    projection: RunProjection


@dataclass(frozen=True)
class RunQueryMissing:
    pass


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
    RunFound
    | RunQueryMissing
    | ReadUnavailable
    | ProjectionTooLarge
    | QueryDurableStateCorrupt
)
type ListRunsResult = (
    RunPage | ReadUnavailable | ProjectionTooLarge | QueryDurableStateCorrupt
)
type GetReconciliationRetryTargetResult = (
    ReconciliationRetryTargetFound
    | ReconciliationRetryTargetMissing
    | ReconciliationRetryCommandConflict
    | RunQueryMissing
    | ReadUnavailable
    | ProjectionTooLarge
    | QueryDurableStateCorrupt
)


class RunQueries(Protocol):
    def get_run(
        self,
        run_id: RunId,
    ) -> GetRunResult: ...

    def list_runs(
        self,
        after: RunId | None,
        limit: int,
    ) -> ListRunsResult: ...

    def get_reconciliation_retry_target(
        self,
        run_id: RunId,
        command_id: ReconcileCommandId,
    ) -> GetReconciliationRetryTargetResult: ...
