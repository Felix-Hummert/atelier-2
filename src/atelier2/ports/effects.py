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

        A false answer means the effect lifecycle records UNKNOWN and waits for
        operator reconciliation before execution. Adapters retain this capability
        because the effect contract, rather than run admission, owns that distinction.
        """
        ...

    def open(self) -> EffectAdapter: ...


class TransactionalEffectReconcileCommander(Protocol):
    def submit_result(
        self, command: ReconcileCommand
    ) -> DurableReconciliationResult: ...
