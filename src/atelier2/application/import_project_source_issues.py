"""Import the connected tracker's open items as OBSERVED queue rows.

**Why this exists.** Since Slice 1 the queue could hold OBSERVED rows and the
admission door could advance them, but nothing in production ever created one:
an operator had to invent tracker references by hand. This module is the
caller that turns the connected repository's open issues into observed rows
(#652), plus the read that shows the operator what is now waiting for an
admission decision.

**Why here and not the route.** The composition owns which tracker the served
project is connected to; this layer owns only that one observation of that
tracker becomes one idempotent durable write, and that every port answer
becomes this layer's own vocabulary -- exactly as `admit_queue_item` translates
the same store's words.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectSourceNotConnected,
    ReadUnavailable,
    SourcePayloadMalformed,
    WriteUnavailable,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import WorkItemReference
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.issue_observation import (
    OpenTrackerItemsObserved,
    TrackerItemSource,
    TrackerPayloadMalformed,
    TrackerSourceUnavailable,
)
from atelier2.ports.queue_projection import QueueItemsObserved, QueueObserver


@dataclass(frozen=True)
class ProjectSourceIssuesImported:
    """The observation landed: every open issue is a row, only the new ones write."""

    observed: int
    newly_observed: int


type ImportProjectSourceIssuesOutcome = (
    ProjectSourceIssuesImported
    | ProjectSourceNotConnected
    | SourcePayloadMalformed
    | ReadUnavailable
    | WriteUnavailable
    | DurableStateCorrupt
)


def import_project_source_issues(
    project: ProjectId | None,
    source: TrackerItemSource | None,
    queue: QueueObserver,
) -> ImportProjectSourceIssuesOutcome:
    """Observe the tracker's open items into the queue, idempotently.

    Idempotency needs no cursor: every reference derives the same durable
    `QueueItemId`, and the store's observe write ignores rows that already
    exist -- so a repeated import adds nothing and never touches an admission.
    """

    if project is None or source is None:
        return ProjectSourceNotConnected()
    match source.open_items():
        case OpenTrackerItemsObserved(references):
            observed = tuple(
                WorkItemReference(project, reference) for reference in references
            )
        case TrackerSourceUnavailable(detail):
            return ReadUnavailable(detail)
        case TrackerPayloadMalformed(detail):
            return SourcePayloadMalformed(detail)
        case _ as unreachable:
            assert_never(unreachable)
    match queue.observe(observed):
        case QueueItemsObserved(total, newly_observed):
            return ProjectSourceIssuesImported(total, newly_observed)
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
