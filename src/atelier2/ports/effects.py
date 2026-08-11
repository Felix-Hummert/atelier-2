from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.effects import (
    EffectAdapterBinding,
    EffectIntent,
    EffectIntentSnapshot,
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
class DurableReconciliationStale:
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
    | DurableReconciliationStale
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

    def open(self) -> EffectAdapter: ...


class DurableRunAdvancer(Protocol):
    def advance(self, intent: EffectIntent) -> EffectIntentSnapshot: ...


class EffectReconcileCommander(Protocol):
    def submit(self, command: ReconcileCommand) -> ReconcileCommandSnapshot: ...


class TransactionalEffectReconcileCommander(Protocol):
    def submit_result(
        self, command: ReconcileCommand
    ) -> DurableReconciliationResult: ...
