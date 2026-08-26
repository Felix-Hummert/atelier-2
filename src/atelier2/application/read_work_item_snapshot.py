"""Read one work item of the connected tracker as the revision a run can pin.

**Why this exists.** The queue knows a work item by reference alone
(REQ-QUEUE-14), which is enough to admit it and not enough to run against it: a
run that reads the item again later reads a moving object, so it can never say
which text it worked from. This layer is where a reference becomes the observed
revision of ADR 0010 §5 -- the served bytes, their digest, and the read's
provenance -- pinned once and unchanged afterwards.

**Why here and not in the adapter.** Which project the reference belongs to is
core knowledge: the adapter answers about the tracker it is composed for, and
this layer pairs that answer with the served project into the same
`WorkItemReference` identity the queue derives its rows from. Every port answer
becomes this layer's own vocabulary here, exactly as the import use case
translates the same source's words.

**One identity, derived.** A read names the item once: the answer's identity is
derived from the revision the source actually returned, never carried beside it
where the two could disagree, and a source answering about a different item than
the one asked for is refused rather than recorded under the asked-for name.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import assert_never

from atelier2.application.refusals import (
    ProjectSourceNotConnected,
    ReadUnavailable,
    SourcePayloadMalformed,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import TrackerItemReference, WorkItemReference
from atelier2.contracts.work_items import ObservedWorkItemRevision
from atelier2.ports.issue_observation import (
    TrackerItemSource,
    TrackerItemUnknown,
    TrackerPayloadMalformed,
    TrackerSourceUnavailable,
    WorkItemRevisionObserved,
)


@dataclass(frozen=True)
class WorkItemSnapshotRead:
    """The item stood at these bytes, under the served project's identity.

    `item_reference` is derived from the revision itself, so this record cannot
    name one item while carrying another's bytes.
    """

    project: ProjectId
    revision: ObservedWorkItemRevision
    item_reference: WorkItemReference = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "item_reference",
            WorkItemReference(self.project, self.revision.item),
        )


@dataclass(frozen=True)
class WorkItemNotInTracker:
    """The connected tracker holds no item at this reference."""

    item_reference: WorkItemReference


type ReadWorkItemSnapshotOutcome = (
    WorkItemSnapshotRead
    | WorkItemNotInTracker
    | ProjectSourceNotConnected
    | SourcePayloadMalformed
    | ReadUnavailable
)


def read_work_item_snapshot(
    project: ProjectId | None,
    source: TrackerItemSource | None,
    reference: TrackerItemReference,
) -> ReadWorkItemSnapshotOutcome:
    """Snapshot one tracker item, named under the served project's identity."""

    if project is None or source is None:
        return ProjectSourceNotConnected()
    match source.snapshot(reference):
        case WorkItemRevisionObserved(revision):
            if revision.item != reference:
                return SourcePayloadMalformed(
                    f"the tracker answered about {revision.item.value!r} for a "
                    f"read of {reference.value!r}"
                )
            return WorkItemSnapshotRead(project, revision)
        case TrackerItemUnknown():
            return WorkItemNotInTracker(WorkItemReference(project, reference))
        case TrackerSourceUnavailable(detail):
            return ReadUnavailable(detail)
        case TrackerPayloadMalformed(detail):
            return SourcePayloadMalformed(detail)
        case _ as unreachable:
            assert_never(unreachable)
