from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.adapter_operations_v3 import AdapterOperationName
from atelier2.contracts.effect_requests import HeadBranch
from atelier2.contracts.effects import (
    EffectAdapterBinding,
    EffectIntent,
    EffectReadback,
    EffectUnknownOutcome,
    PerformedEffect,
    ReadbackPhase,
    ReconcileCommand,
    ReconcileCommandSnapshot,
    UnknownOutcomeReason,
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


@dataclass(frozen=True, slots=True)
class PullRequestOpenOnHeadBranch:
    """A pull request on this head branch is still open, and this one names it."""

    number: int


@dataclass(frozen=True, slots=True)
class NoPullRequestOpenOnHeadBranch:
    """The tracker answered about this branch, and none of its pull requests is open."""


@dataclass(frozen=True, slots=True)
class HeadBranchPullRequestsUnreadable:
    """The tracker resolved nothing about this branch, and said why."""

    reason: UnknownOutcomeReason


type HeadBranchPullRequestState = (
    PullRequestOpenOnHeadBranch
    | NoPullRequestOpenOnHeadBranch
    | HeadBranchPullRequestsUnreadable
)


class HeadBranchPullRequests(Protocol):
    """Which pull requests still stand open on one head branch.

    A publishing adapter asks this about a branch it finds carrying a commit
    that is not the one it was asked to publish: work nobody reviews any more
    may be replaced, work someone is reviewing may not, and a tracker that
    cannot answer licenses nothing.
    """

    def open_pull_requests_on(
        self, head_branch: HeadBranch
    ) -> HeadBranchPullRequestState: ...


class EffectAdapter(Protocol):
    def readback(self, intent: EffectIntent, phase: ReadbackPhase) -> EffectReadback:
        """Read what the destination holds for one intent, in a named phase.

        `ReadbackPhase.AFTER_SEND` forbids `EffectAbsence`: an adapter reading
        nothing there reports `EffectUnknownOutcome` carrying what the
        destination said, because a send whose answer was lost looks from here
        exactly like one that never happened.
        """
        ...

    def execute(
        self, intent: EffectIntent
    ) -> PerformedEffect | EffectUnknownOutcome: ...

    def close(self) -> None: ...


class EffectAdapterFactory(Protocol):
    @property
    def binding(self) -> EffectAdapterBinding: ...

    @property
    def proves_absence(self) -> bool:
        """Whether a pre-send not-found from this adapter is an authoritative absence.

        A false answer means the effect lifecycle records UNKNOWN and waits for
        operator reconciliation before execution. Adapters retain this capability
        because the effect contract, rather than run admission, owns that distinction.
        Nothing here licenses anything after a send; `ReadbackPhase` decides that.
        """
        ...

    def open(self) -> EffectAdapter: ...


class EffectAdapterRegistryConflict(ValueError):
    """Registrations are ambiguous or disagree with the factories they name."""


class EffectAdapterRegistryMissing(LookupError):
    """No registered adapter performs a requested durable operation."""


@dataclass(frozen=True, slots=True)
class EffectAdapterRegistration:
    operation: AdapterOperationName
    factory: EffectAdapterFactory


class EffectAdapterRegistry:
    """The closed, immutable operation-to-factory registry for one runtime."""

    def __init__(self, registrations: Iterable[EffectAdapterRegistration]) -> None:
        entries = tuple(registrations)
        by_operation: dict[AdapterOperationName, EffectAdapterFactory] = {}
        seen_factories: set[int] = set()
        seen_bindings: set[tuple[object, object, object]] = set()
        for registration in entries:
            operation = registration.operation
            factory = registration.factory
            if not isinstance(operation, AdapterOperationName):
                raise EffectAdapterRegistryConflict(
                    "an effect registration uses the typed operation vocabulary"
                )
            if factory.binding.operation_name is not operation:
                raise EffectAdapterRegistryConflict(
                    f"registration operation {operation.value!r} differs from its "
                    f"factory binding {factory.binding.operation_name.value!r}"
                )
            if operation in by_operation:
                raise EffectAdapterRegistryConflict(
                    f"operation {operation.value!r} is registered more than once"
                )
            factory_identity = id(factory)
            if factory_identity in seen_factories:
                raise EffectAdapterRegistryConflict(
                    "one effect adapter factory is registered more than once"
                )
            binding_identity = (
                factory.binding.adapter_revision,
                factory.binding.destination,
                factory.binding.operational_identity,
            )
            if binding_identity in seen_bindings:
                raise EffectAdapterRegistryConflict(
                    "one effect adapter binding is registered more than once"
                )
            by_operation[operation] = factory
            seen_factories.add(factory_identity)
            seen_bindings.add(binding_identity)
        if not entries:
            raise EffectAdapterRegistryConflict("an effect registry is nonempty")
        self._entries = entries

    @property
    def bindings(self) -> tuple[EffectAdapterBinding, ...]:
        return tuple(entry.factory.binding for entry in self._entries)

    def open(self) -> OpenEffectAdapterRegistry:
        opened: list[tuple[EffectAdapterRegistration, EffectAdapter]] = []
        try:
            for entry in self._entries:
                opened.append((entry, entry.factory.open()))
        except BaseException as original:
            cleanup_errors: list[BaseException] = []
            for _entry, adapter in reversed(opened):
                try:
                    adapter.close()
                except RuntimeError as cleanup_error:
                    cleanup_errors.append(cleanup_error)
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "effect registry open and cleanup both failed",
                    [original, *cleanup_errors],
                ) from None
            raise
        return OpenEffectAdapterRegistry(tuple(opened))


class OpenEffectAdapterRegistry:
    """Every adapter opened together, with exact persisted-binding selection."""

    def __init__(
        self,
        entries: tuple[tuple[EffectAdapterRegistration, EffectAdapter], ...],
    ) -> None:
        self._entries = entries
        self._by_operation = {
            entry.operation: (entry.factory, adapter) for entry, adapter in entries
        }
        self._closed = False

    def adapter_for(
        self, operation: AdapterOperationName, binding: EffectAdapterBinding
    ) -> EffectAdapter:
        if self._closed:
            raise RuntimeError("effect adapter registry is closed")
        try:
            factory, adapter = self._by_operation[operation]
        except KeyError as error:
            raise EffectAdapterRegistryMissing(
                f"no open effect adapter performs {operation.value!r}"
            ) from error
        if factory.binding != binding:
            raise EffectAdapterRegistryConflict(
                f"durable {operation.value!r} binding differs from its registration"
            )
        return adapter

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[BaseException] = []
        for _entry, adapter in reversed(self._entries):
            try:
                adapter.close()
            except RuntimeError as error:
                errors.append(error)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("effect adapter registry close failed", errors)


class TransactionalEffectReconcileCommander(Protocol):
    def submit_result(
        self, command: ReconcileCommand
    ) -> DurableReconciliationResult: ...
