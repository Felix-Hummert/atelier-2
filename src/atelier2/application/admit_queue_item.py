"""Admit an observed queue item, and read back what the queue has admitted.

**Why this exists.** `ports.queue_projection.QueueProjection.admit` has proved
the durable admission CAS since Slice 1 (#569, ADR 0016), and nothing in
production ever called it: the port had a store and a contract but no
application-layer caller, exactly the gap `admit_catalog_member` closed for
the catalog's own admission door. This module is that caller for the queue,
plus the read its store now answers: without it the queue was a table only
its own tests could fill or see.

**Why here and not the route.** The caller already holds an inspected
revision and a resolved workflow lineage above this seam (ADR 0016); this
layer owns only that the port's answer becomes this layer's own vocabulary,
exactly as `read_workflow_revisions` translates `WorkflowRevisionFound` into
`WorkflowRevisionRead` -- a caller above matches this layer's outcomes without
ever importing the port's or the store's word for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.queue_projection import (
    AdmitQueueItem,
    QueueAdmissionOutcome,
    QueueItemId,
    QueueItemSnapshot,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.queue_projection import (
    AdmittedQueueItemsPage,
    QueueProjection,
    QueueReadUnavailable,
)

type AdmitQueueItemOutcome = (
    QueueAdmissionOutcome | WriteUnavailable | DurableStateCorrupt
)


def admit_queue_item(
    command: AdmitQueueItem, queue: QueueProjection
) -> AdmitQueueItemOutcome:
    """Admit one item, or return the port's own outcome in this layer's word.

    `QueueAdmissionOutcome`'s members -- `QueueItemAdmitted`,
    `QueueAdmissionAlreadyCurrent`, `QueueAdmissionRevisionConflict`,
    `QueueAdmissionAlreadyDecided` -- already are this layer's vocabulary:
    they are contract types the caller above matches directly, exactly as
    `admit_catalog_member` hands its own contract outcomes on untouched. Only
    the store's two durable-failure words are translated, so a caller above
    never has to name the port to read either of them.
    """

    outcome = queue.admit(command)
    if isinstance(outcome, DurableWriteUnavailable):
        return WriteUnavailable()
    if isinstance(outcome, PortDurableStateCorrupt):
        return DurableStateCorrupt()
    return outcome


@dataclass(frozen=True)
class AdmittedQueueItemsListed:
    """One page of admitted items, each with its workflow binding and rationale."""

    items: tuple[QueueItemSnapshot, ...]
    next_after: QueueItemId | None


type ListAdmittedQueueItemsOutcome = (
    AdmittedQueueItemsListed | ReadUnavailable | DurableStateCorrupt
)


def list_admitted_queue_items(
    after: QueueItemId | None, limit: int, queue: QueueProjection
) -> ListAdmittedQueueItemsOutcome:
    """Read one page of admitted items, in this layer's own vocabulary."""

    match queue.list_admitted_items(after, limit):
        case AdmittedQueueItemsPage(items, next_after):
            return AdmittedQueueItemsListed(items, next_after)
        case QueueReadUnavailable():
            return ReadUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
