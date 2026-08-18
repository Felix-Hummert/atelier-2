from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.agents import AgentReceiptV2
from atelier2.contracts.effects import (
    EffectIntentSnapshot,
    ReconcileCommandId,
)
from atelier2.contracts.run_projections import NodeDetail, RunPage, RunProjection
from atelier2.contracts.runs import RunId, RunState
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

    def get_node_detail(self, run_id: RunId, node_id: str) -> GetNodeDetailResult: ...

    def list_run_receipts(self, run_id: RunId) -> ListRunReceiptsResult: ...

    def list_runs(
        self,
        after: RunId | None,
        limit: int,
        state: RunState | None = None,
    ) -> ListRunsResult: ...

    def get_reconciliation_retry_target(
        self,
        run_id: RunId,
        command_id: ReconcileCommandId,
    ) -> GetReconciliationRetryTargetResult: ...


@dataclass(frozen=True)
class NodeDetailFound:
    detail: NodeDetail


@dataclass(frozen=True)
class NodeQueryMissing:
    """The run exists and declares no node by that name."""


type GetNodeDetailResult = (
    NodeDetailFound
    | NodeQueryMissing
    | RunQueryMissing
    | ReadUnavailable
    | ProjectionTooLarge
    | QueryDurableStateCorrupt
)


@dataclass(frozen=True)
class RunReceiptsFound:
    items: tuple[AgentReceiptV2, ...]


type ListRunReceiptsResult = (
    RunReceiptsFound | RunQueryMissing | ReadUnavailable | QueryDurableStateCorrupt
)
