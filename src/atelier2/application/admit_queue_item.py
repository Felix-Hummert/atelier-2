"""Confirm an inspected queue proposal and read the one typed projection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.queue_projection import (
    ConfirmQueueProposal,
    QueueAdmissionOutcome,
    QueueItemId,
    QueueItemSnapshot,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.queue_projection import (
    QueueAdmissionConfirmer,
    QueueItemsPage,
    QueueItemsReader,
    QueueReadUnavailable,
)

type ConfirmQueueProposalOutcome = (
    QueueAdmissionOutcome | WriteUnavailable | DurableStateCorrupt
)


def confirm_queue_proposal(
    command: ConfirmQueueProposal, queue: QueueAdmissionConfirmer
) -> ConfirmQueueProposalOutcome:
    outcome = queue.confirm(command)
    if isinstance(outcome, DurableWriteUnavailable):
        return WriteUnavailable()
    if isinstance(outcome, PortDurableStateCorrupt):
        return DurableStateCorrupt()
    return outcome


@dataclass(frozen=True)
class QueueItemsListed:
    items: tuple[QueueItemSnapshot, ...]
    next_after: QueueItemId | None


type ListQueueItemsOutcome = QueueItemsListed | ReadUnavailable | DurableStateCorrupt


def list_queue_items(
    after: QueueItemId | None, limit: int, queue: QueueItemsReader
) -> ListQueueItemsOutcome:
    match queue.list_items(after, limit):
        case QueueItemsPage(items, next_after):
            return QueueItemsListed(items, next_after)
        case QueueReadUnavailable():
            return ReadUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
