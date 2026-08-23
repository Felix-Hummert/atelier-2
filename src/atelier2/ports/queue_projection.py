"""The one durable write this slice proves: admitting an observed queue item.

A caller resolves an item's identity and its workflow binding above this seam
(`atelier2.application.admit_queue_item`); this port owns only that the durable
row advances under `contracts.queue_projection.QueueItemSnapshot.admit`'s own
rule, or is refused unaltered.
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
