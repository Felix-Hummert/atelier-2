"""The queue's own durable identity: one tracker-referenced item, one admission.

Core owns orchestration state keyed by a reference into whichever tracker holds
the item (REQ-QUEUE-14): never the item's title, description, comments, or
parent -- those stay the tracker's. What lives here is the smallest fact this
slice proves: a work item is identified by its project and tracker reference,
and it may be admitted into the queue under one named workflow binding, once,
with a durable reason. Dependency edges, readiness, and priority are later
slices and name no type here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.host_configuration import ProjectId

MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS = 1_024
MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS = 4_096


@dataclass(frozen=True)
class TrackerItemReference:
    """The opaque address of one item inside whichever tracker holds it.

    Core reads no more of the tracker than this reference carries: what the
    string means -- an issue number, a GitLab path -- is the connected
    platform adapter's contract (ADR 0010), never reinterpreted here.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a tracker item reference must be text")
        if not 1 <= len(self.value) <= MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS:
            raise ValueError(
                "a tracker item reference must contain 1 to "
                f"{MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS} characters"
            )


class QueueItemId(Sha256Hash):
    """The store-stable identity derived from one item's project and reference."""


@dataclass(frozen=True)
class WorkItemReference:
    """Which connected project, and which item inside its tracker.

    The pair is the whole identity: two references naming the same project and
    the same tracker item resolve to the same queue row, by derivation rather
    than by an id a caller could hand in and have accepted.
    """

    project: ProjectId
    tracker_item: TrackerItemReference
    item_id: QueueItemId = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project, ProjectId):
            raise TypeError(
                "a work item reference names its project through the contract"
            )
        if not isinstance(self.tracker_item, TrackerItemReference):
            raise TypeError(
                "a work item reference names its tracker item through the contract"
            )
        object.__setattr__(
            self,
            "item_id",
            QueueItemId.of(
                frame(
                    "queue-item/v1",
                    self.project.value.encode("utf-8"),
                    self.tracker_item.value.encode("utf-8"),
                )
            ),
        )


@dataclass(frozen=True)
class QueueProjectionRevision:
    """How many durable admission transitions one queue item has advanced."""

    value: int

    def __post_init__(self) -> None:
        if type(self.value) is not int or self.value < 0:
            raise ValueError(
                "QueueProjectionRevision must be a nonnegative advance count"
            )


QUEUE_PROJECTION_REVISION_OBSERVED: Final = QueueProjectionRevision(0)
QUEUE_PROJECTION_REVISION_ADMITTED: Final = QueueProjectionRevision(1)


class QueueItemState(StrEnum):
    """The closed lifecycle this slice proves. Readiness states are later work."""

    OBSERVED = "OBSERVED"
    ADMITTED = "ADMITTED"


@dataclass(frozen=True)
class QueueAdmissionRationale:
    """The durable reason recorded for one admission decision."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a queue admission rationale must be text")
        if not 1 <= len(self.value) <= MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS:
            raise ValueError(
                "a queue admission rationale must contain 1 to "
                f"{MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS} characters"
            )


@dataclass(frozen=True)
class QueueAdmission:
    """The one named workflow binding an admitted item carries, and why.

    The binding names a catalog lineage rather than a revision: which workflow
    this item runs under is a named decision that survives the lineage
    publishing later members, exactly as a catalog name resolves to `head`.
    """

    workflow_lineage_id: CatalogLineageId
    rationale: QueueAdmissionRationale

    def __post_init__(self) -> None:
        if not isinstance(self.workflow_lineage_id, CatalogLineageId):
            raise TypeError(
                "a queue admission names its workflow through the catalog lineage id"
            )
        if not isinstance(self.rationale, QueueAdmissionRationale):
            raise TypeError(
                "a queue admission carries its rationale through the contract"
            )


@dataclass(frozen=True)
class AdmitQueueItem:
    """One caller's request to admit one item, against the revision it inspected."""

    item_reference: WorkItemReference
    admission: QueueAdmission
    expected_revision: QueueProjectionRevision


@dataclass(frozen=True)
class QueueItemAdmitted:
    """The item advanced from OBSERVED to ADMITTED under this admission."""

    item_reference: WorkItemReference
    admission: QueueAdmission
    revision: QueueProjectionRevision


@dataclass(frozen=True)
class QueueAdmissionAlreadyCurrent:
    """A repeated request for exactly the admission already recorded: no mutation."""

    item_reference: WorkItemReference
    admission: QueueAdmission
    revision: QueueProjectionRevision


@dataclass(frozen=True)
class QueueAdmissionRevisionConflict:
    """The caller inspected a revision this item has since moved past."""

    expected: QueueProjectionRevision
    actual: QueueProjectionRevision


@dataclass(frozen=True)
class QueueAdmissionAlreadyDecided:
    """The item is already admitted under a different workflow binding or reason."""

    item_reference: WorkItemReference
    existing_admission: QueueAdmission


type QueueAdmissionOutcome = (
    QueueItemAdmitted
    | QueueAdmissionAlreadyCurrent
    | QueueAdmissionRevisionConflict
    | QueueAdmissionAlreadyDecided
)


class QueueItemReferenceMismatch(RuntimeError):
    """A command named a different item than the snapshot it was resolved against."""


@dataclass(frozen=True)
class QueueItemSnapshot:
    """One durable point-in-time view of a queue item's admission lifecycle."""

    item_reference: WorkItemReference
    state: QueueItemState
    revision: QueueProjectionRevision
    admission: QueueAdmission | None

    def __post_init__(self) -> None:
        admitted = self.state is QueueItemState.ADMITTED
        if admitted != (self.admission is not None):
            raise ValueError(
                "a queue item snapshot carries an admission if and only if it is ADMITTED"
            )

    def admit(self, command: AdmitQueueItem) -> QueueAdmissionOutcome:
        """The one legal transition this projection owns: OBSERVED to ADMITTED.

        Revision-checked so a caller that inspected a stale row is refused
        rather than silently overwriting a decision it never saw, and
        idempotent for its own exact admission so a retried command lands as
        success without a second write.
        """

        if command.item_reference != self.item_reference:
            raise QueueItemReferenceMismatch(
                "an admission command must name the item its snapshot was resolved for"
            )
        if self.state is QueueItemState.ADMITTED:
            current = self.admission
            if current is None:
                raise QueueItemReferenceMismatch(
                    "an ADMITTED snapshot must carry its admission"
                )
            if current == command.admission:
                return QueueAdmissionAlreadyCurrent(
                    self.item_reference, current, self.revision
                )
            return QueueAdmissionAlreadyDecided(self.item_reference, current)
        if command.expected_revision != self.revision:
            return QueueAdmissionRevisionConflict(
                command.expected_revision, self.revision
            )
        return QueueItemAdmitted(
            self.item_reference,
            command.admission,
            QueueProjectionRevision(self.revision.value + 1),
        )
