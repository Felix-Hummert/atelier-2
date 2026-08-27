"""Narrow ports for the durable Phase-D queue projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.queue_projection import (
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueAdmissionOutcome,
    QueueItemId,
    QueueItemSnapshot,
    QueueLaunchBinding,
    QueueProjectPolicyRevision,
    QueueProposalOutcome,
    WorkItemReference,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable

type ConfirmQueueProposalResult = (
    QueueAdmissionOutcome | DurableWriteUnavailable | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueProposalRefused:
    reason: str


type PlanQueueItemResult = (
    QueueProposalOutcome
    | QueueProposalRefused
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueProjectPolicyPublished:
    policy: QueueProjectPolicyRevision


@dataclass(frozen=True)
class QueueProjectPolicyUnchanged:
    policy: QueueProjectPolicyRevision


@dataclass(frozen=True)
class QueueProjectPolicyRevisionConflict:
    expected_revision: int
    actual_revision: int


type PutQueueProjectPolicyResult = (
    QueueProjectPolicyPublished
    | QueueProjectPolicyUnchanged
    | QueueProjectPolicyRevisionConflict
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueLaunchReserved:
    binding: QueueLaunchBinding


@dataclass(frozen=True)
class QueueLaunchAlreadyBound:
    binding: QueueLaunchBinding


@dataclass(frozen=True)
class QueueLaunchBlocked:
    item: QueueItemSnapshot


type ReserveQueueLaunchResult = (
    QueueLaunchReserved
    | QueueLaunchAlreadyBound
    | QueueLaunchBlocked
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueReadUnavailable:
    """The store could not answer this read, and a later attempt may succeed."""


@dataclass(frozen=True)
class QueueItemsObserved:
    """How one observation batch landed: what was new, against what was handed in.

    Identity does the deduplication (`QueueItemId` derives from project and
    tracker reference), so a repeated observation is counted, never rewritten:
    `newly_observed` is the rows this write created, and the difference to
    `observed` already existed -- observed earlier, or already admitted.
    """

    observed: int
    newly_observed: int


type ObserveQueueItemsResult = (
    QueueItemsObserved | DurableWriteUnavailable | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueItemsPage:
    """One typed projection across every queue lifecycle state."""

    items: tuple[QueueItemSnapshot, ...]
    next_after: QueueItemId | None


type ListQueueItemsResult = QueueItemsPage | QueueReadUnavailable | DurableStateCorrupt


class QueueObserver(Protocol):
    def observe(
        self, references: tuple[WorkItemReference, ...]
    ) -> ObserveQueueItemsResult: ...


class QueuePlanner(Protocol):
    def plan(self, command: PlanQueueItem) -> PlanQueueItemResult: ...


class QueueAdmissionConfirmer(Protocol):
    def confirm(self, command: ConfirmQueueProposal) -> ConfirmQueueProposalResult: ...


class QueuePolicyWriter(Protocol):
    def put_policy(
        self, policy: QueueProjectPolicyRevision, expected_revision: int
    ) -> PutQueueProjectPolicyResult: ...


class QueueLaunchReserver(Protocol):
    def reserve_launch(
        self, binding: QueueLaunchBinding
    ) -> ReserveQueueLaunchResult: ...


class QueueItemsReader(Protocol):
    def list_items(
        self, after: QueueItemId | None, limit: int
    ) -> ListQueueItemsResult: ...


class QueueProjection(
    QueueObserver,
    QueuePlanner,
    QueueAdmissionConfirmer,
    QueuePolicyWriter,
    QueueLaunchReserver,
    QueueItemsReader,
    Protocol,
):
    """The complete durable home of the Phase-D queue lifecycle."""
