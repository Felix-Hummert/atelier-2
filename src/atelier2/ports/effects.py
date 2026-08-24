from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.effects import (
    EffectAdapterBinding,
    EffectIntent,
    EffectReadback,
    PerformedEffect,
    ReconcileCommand,
    ReconcileCommandSnapshot,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


@dataclass(frozen=True)
class DurableReconciliationCreated:
    snapshot: ReconcileCommandSnapshot


@dataclass(frozen=True)
class DurableReconciliationExisting:
    snapshot: ReconcileCommandSnapshot


@dataclass(frozen=True)
class DurableReconciliationTargetMissing:
    pass


@dataclass(frozen=True)
class DurableReconciliationCommandConflict:
    pass


@dataclass(frozen=True)
class DurableReconciliationDeterminationConflict:
    pass


type DurableReconciliationResult = (
    DurableReconciliationCreated
    | DurableReconciliationExisting
    | DurableReconciliationTargetMissing
    | DurableReconciliationCommandConflict
    | DurableReconciliationDeterminationConflict
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class EffectAdapter(Protocol):
    def readback(self, intent: EffectIntent) -> EffectReadback: ...

    def execute(self, intent: EffectIntent) -> PerformedEffect: ...

    def close(self) -> None: ...


class EffectAdapterFactory(Protocol):
    @property
    def binding(self) -> EffectAdapterBinding: ...

    @property
    def proves_absence(self) -> bool:
        """Whether a not-found readback from this adapter is an authoritative absence.

        Admission refuses an agent-authored `open-pr` grant against a destination
        that cannot prove absence, because its redemption has no safe reconciliation
        path (`#430`/`#431`). Making this a protocol member forces every composition
        to carry the answer to the run starter, so no start path can silently assume
        it.
        """
        ...

    def open(self) -> EffectAdapter: ...


class TransactionalEffectReconcileCommander(Protocol):
    def submit_result(
        self, command: ReconcileCommand
    ) -> DurableReconciliationResult: ...
