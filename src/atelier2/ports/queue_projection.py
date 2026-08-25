"""The durable write and the durable read this slice proves.

A caller resolves an item's identity and its workflow binding above this seam;
this port owns only that the durable row advances under
`contracts.queue_projection.QueueItemSnapshot.admit`'s own rule, or is refused
unaltered, and that an admitted item can be read back with its binding and
rationale. That resolution -- reading the named workflow's catalog lineage
before handing this port an admission -- has no production caller in this
slice; it lands with the platform door in a later slice.

The `CatalogLineageId` an `AdmitQueueItem` command's admission names must
already be a founded lineage. Nothing here validates that: the store's
foreign key refuses an unfounded id, and today that refusal surfaces as
`DurableStateCorrupt` -- the honest word for durable state a sequence of writes
could not have produced, not for a caller's bad reference. A caller-error
refusal of its own is future work; until then, handing this port a lineage id
no admission ever founded is a caller error read as if it were store
corruption.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.queue_projection import (
    AdmitQueueItem,
    QueueAdmissionOutcome,
    QueueItemId,
    QueueItemSnapshot,
    WorkItemReference,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable

type AdmitQueueItemResult = (
    QueueAdmissionOutcome | DurableWriteUnavailable | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueReadUnavailable:
    """The store could not answer this read, and a later attempt may succeed."""


@dataclass(frozen=True)
class AdmittedQueueItemsPage:
    """One page of admitted items, each carrying its workflow binding and rationale.

    Ordered by `QueueItemId` rather than admission order, exactly as
    `list_workflow_revisions` orders by revision hash: the cursor is a durable
    identity a caller can resume from, not an insertion sequence this store
    does not track.
    """

    items: tuple[QueueItemSnapshot, ...]
    next_after: QueueItemId | None


type ListAdmittedQueueItemsResult = (
    AdmittedQueueItemsPage | QueueReadUnavailable | DurableStateCorrupt
)


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
class ObservedQueueItemsPage:
    """One page of items still OBSERVED, awaiting the operator's admission.

    Ordered by `QueueItemId` exactly as the admitted page is: the cursor is a
    durable identity a caller can resume from, not an insertion sequence.
    """

    items: tuple[QueueItemSnapshot, ...]
    next_after: QueueItemId | None


type ListObservedQueueItemsResult = (
    ObservedQueueItemsPage | QueueReadUnavailable | DurableStateCorrupt
)


class QueueProjection(Protocol):
    """The durable home of one item's admission lifecycle."""

    def observe(
        self, references: tuple[WorkItemReference, ...]
    ) -> ObserveQueueItemsResult: ...

    def admit(self, command: AdmitQueueItem) -> AdmitQueueItemResult: ...

    def list_observed_items(
        self, after: QueueItemId | None, limit: int
    ) -> ListObservedQueueItemsResult: ...

    def list_admitted_items(
        self, after: QueueItemId | None, limit: int
    ) -> ListAdmittedQueueItemsResult: ...
