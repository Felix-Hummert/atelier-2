"""The one durable write this slice proves: admitting an observed queue item.

A caller resolves an item's identity and its workflow binding above this seam;
this port owns only that the durable row advances under
`contracts.queue_projection.QueueItemSnapshot.admit`'s own rule, or is refused
unaltered. That resolution -- reading the named workflow's catalog lineage
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

from typing import Protocol

from atelier2.contracts.queue_projection import AdmitQueueItem, QueueAdmissionOutcome
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable

type AdmitQueueItemResult = (
    QueueAdmissionOutcome | DurableWriteUnavailable | DurableStateCorrupt
)


class QueueProjection(Protocol):
    """The durable home of one item's admission lifecycle."""

    def admit(self, command: AdmitQueueItem) -> AdmitQueueItemResult: ...
