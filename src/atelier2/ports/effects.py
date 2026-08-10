from __future__ import annotations

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
