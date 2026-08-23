from __future__ import annotations

import pytest

from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    AdmitQueueItem,
    QueueAdmission,
    QueueAdmissionAlreadyCurrent,
    QueueAdmissionAlreadyDecided,
    QueueAdmissionRationale,
    QueueAdmissionRevisionConflict,
    QueueItemAdmitted,
    QueueItemReferenceMismatch,
    QueueItemSnapshot,
    QueueItemState,
    QueueProjectionRevision,
    TrackerItemReference,
    WorkItemReference,
)

_WORKFLOW_LINEAGE_ID = CatalogLineageId(
    "d392f5891160350355c3cb56a70c66afc725c19a5a39c7616198fcd3cf1e731e"
)
_OTHER_WORKFLOW_LINEAGE_ID = CatalogLineageId(
    "3bbea9dce190030e6cec035c1202bb6b8fed1f54058c6e0ffb7835c7590cf18f"
)


def _reference(
    project: str = "project1", tracker_item: str = "gh:79"
) -> WorkItemReference:
    return WorkItemReference(ProjectId(project), TrackerItemReference(tracker_item))


def _admission(
    lineage_id: CatalogLineageId = _WORKFLOW_LINEAGE_ID,
    rationale: str = "matches label",
) -> QueueAdmission:
    return QueueAdmission(lineage_id, QueueAdmissionRationale(rationale))


def _observed_snapshot(reference: WorkItemReference | None = None) -> QueueItemSnapshot:
    return QueueItemSnapshot(
        reference or _reference(),
        QueueItemState.OBSERVED,
        QueueProjectionRevision(0),
        None,
    )


def _admitted_snapshot(
    reference: WorkItemReference | None = None,
    admission: QueueAdmission | None = None,
) -> QueueItemSnapshot:
    return QueueItemSnapshot(
        reference or _reference(),
        QueueItemState.ADMITTED,
        QueueProjectionRevision(1),
        admission or _admission(),
    )


IDENTITY_VECTORS: tuple[tuple[str, str, str, str], ...] = (
    ("project1", "gh:79", "project1", "gh:79"),
    ("project1", "gh:79", "project2", "gh:79"),
    ("project1", "gh:79", "project1", "gh:80"),
)


@pytest.mark.parametrize(
    ("project_a", "item_a", "project_b", "item_b"), IDENTITY_VECTORS
)
def test_item_identity_is_derived_from_project_and_tracker_reference(
    project_a: str, item_a: str, project_b: str, item_b: str
) -> None:
    left = WorkItemReference(ProjectId(project_a), TrackerItemReference(item_a))
    right = WorkItemReference(ProjectId(project_b), TrackerItemReference(item_b))

    same_pair = (project_a, item_a) == (project_b, item_b)

    assert (left.item_id == right.item_id) is same_pair


def test_item_identity_cannot_be_supplied_only_derived() -> None:
    with pytest.raises(TypeError):
        WorkItemReference(  # type: ignore[call-arg]
            ProjectId("project1"),
            TrackerItemReference("gh:79"),
            item_id="a" * 64,  # type: ignore[arg-type]
        )


def test_admit_on_a_fresh_observed_item_advances_to_admitted() -> None:
    snapshot = _observed_snapshot()
    command = AdmitQueueItem(snapshot.item_reference, _admission(), snapshot.revision)

    outcome = snapshot.admit(command)

    assert outcome == QueueItemAdmitted(
        snapshot.item_reference, _admission(), QueueProjectionRevision(1)
    )


def test_admit_against_a_stale_revision_is_a_revision_conflict() -> None:
    snapshot = _observed_snapshot()
    stale_expected = QueueProjectionRevision(snapshot.revision.value + 1)
    command = AdmitQueueItem(snapshot.item_reference, _admission(), stale_expected)

    outcome = snapshot.admit(command)

    assert outcome == QueueAdmissionRevisionConflict(stale_expected, snapshot.revision)


def test_repeating_the_exact_same_admission_is_idempotent_success() -> None:
    snapshot = _admitted_snapshot()
    command = AdmitQueueItem(
        snapshot.item_reference, _admission(), QueueProjectionRevision(0)
    )

    outcome = snapshot.admit(command)

    assert outcome == QueueAdmissionAlreadyCurrent(
        snapshot.item_reference, _admission(), snapshot.revision
    )


def test_repeating_admission_with_a_different_workflow_binding_is_an_illegal_transition() -> (
    None
):
    snapshot = _admitted_snapshot()
    different = _admission(_OTHER_WORKFLOW_LINEAGE_ID)
    command = AdmitQueueItem(
        snapshot.item_reference, different, QueueProjectionRevision(0)
    )

    outcome = snapshot.admit(command)

    assert outcome == QueueAdmissionAlreadyDecided(
        snapshot.item_reference, _admission()
    )


def test_repeating_admission_with_a_different_rationale_is_an_illegal_transition() -> (
    None
):
    snapshot = _admitted_snapshot()
    different = _admission(rationale="a different reason")
    command = AdmitQueueItem(
        snapshot.item_reference, different, QueueProjectionRevision(0)
    )

    outcome = snapshot.admit(command)

    assert outcome == QueueAdmissionAlreadyDecided(
        snapshot.item_reference, _admission()
    )


def test_admit_refuses_a_command_naming_another_item() -> None:
    snapshot = _observed_snapshot()
    other_reference = _reference(tracker_item="gh:80")
    command = AdmitQueueItem(other_reference, _admission(), snapshot.revision)

    with pytest.raises(QueueItemReferenceMismatch):
        snapshot.admit(command)


def test_snapshot_rejects_an_admitted_state_without_its_admission() -> None:
    with pytest.raises(ValueError, match="carries an admission if and only if"):
        QueueItemSnapshot(
            _reference(), QueueItemState.ADMITTED, QueueProjectionRevision(1), None
        )


def test_snapshot_rejects_an_observed_state_carrying_an_admission() -> None:
    with pytest.raises(ValueError, match="carries an admission if and only if"):
        QueueItemSnapshot(
            _reference(),
            QueueItemState.OBSERVED,
            QueueProjectionRevision(0),
            _admission(),
        )
