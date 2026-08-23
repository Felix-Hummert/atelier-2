from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import initialize_schema, queue_items
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
    CatalogLineageDisplayName,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    AdmitQueueItem,
    QueueAdmission,
    QueueAdmissionAlreadyCurrent,
    QueueAdmissionAlreadyDecided,
    QueueAdmissionRationale,
    QueueAdmissionRevisionConflict,
    QueueItemAdmitted,
    QueueItemState,
    QueueProjectionRevision,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.ports.published_revisions import CatalogLineageFounded


@dataclass(frozen=True)
class QueueHarness:
    store: DbosQueueProjectionStore
    engine: Engine


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[QueueHarness]:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    try:
        yield QueueHarness(DbosQueueProjectionStore(engine), engine)
    finally:
        engine.dispose()


_LINEAGE_NAME_COUNTER = iter(range(1_000_000))


def _founded_workflow_lineage(engine: Engine) -> CatalogLineage:
    catalog = DbosCatalogStore(engine)
    published = PublishedRevision(
        RevisionKind.WORKFLOW,
        f"queue-workflow-document-{next(_LINEAGE_NAME_COUNTER)}".encode(),
    )
    catalog.publish_revision(published)
    result = catalog.found_lineage(
        published,
        CatalogLineageDisplayName(
            f"queue-triage-workflow-{next(_LINEAGE_NAME_COUNTER)}"
        ),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-23T09:00:00Z"),
    )
    assert isinstance(result, CatalogLineageFounded)
    return result.lineage


def _reference() -> WorkItemReference:
    return WorkItemReference(ProjectId("project1"), TrackerItemReference("gh:79"))


def _row(engine: Engine, item_id: str) -> tuple[object, ...] | None:
    with engine.connect() as connection:
        record = connection.execute(
            sa.select(queue_items).where(queue_items.c.item_id == item_id)
        ).one_or_none()
        return None if record is None else tuple(record)


@pytest.mark.proves("a-work-item-is-admitted-into-the-queue")
def test_admitting_a_fresh_item_writes_its_durable_admission(
    harness: QueueHarness,
) -> None:
    lineage = _founded_workflow_lineage(harness.engine)
    reference = _reference()
    admission = QueueAdmission(
        lineage.lineage_id, QueueAdmissionRationale("matches the triage rule")
    )
    command = AdmitQueueItem(reference, admission, QueueProjectionRevision(0))

    outcome = harness.store.admit(command)

    assert outcome == QueueItemAdmitted(
        reference, admission, QueueProjectionRevision(1)
    )
    row = _row(harness.engine, reference.item_id.value)
    assert row is not None
    (item_id, project_id, tracker_item_reference, state, state_version, *_rest) = row
    assert item_id == reference.item_id.value
    assert project_id == "project1"
    assert tracker_item_reference == "gh:79"
    assert state == QueueItemState.ADMITTED.value
    assert state_version == 1


@pytest.mark.proves("a-work-item-is-admitted-into-the-queue")
def test_a_stale_revision_is_refused_leaving_the_row_byte_identical(
    harness: QueueHarness,
) -> None:
    lineage = _founded_workflow_lineage(harness.engine)
    reference = _reference()
    with harness.engine.connect() as connection:
        connection.execute(
            queue_items.insert().values(
                item_id=reference.item_id.value,
                project_id=reference.project.value,
                tracker_item_reference=reference.tracker_item.value,
                state=QueueItemState.OBSERVED.value,
                state_version=0,
                workflow_lineage_id=None,
                admission_rationale=None,
            )
        )
        connection.commit()
    before = _row(harness.engine, reference.item_id.value)
    admission = QueueAdmission(
        lineage.lineage_id, QueueAdmissionRationale("matches the triage rule")
    )
    stale_command = AdmitQueueItem(reference, admission, QueueProjectionRevision(7))

    outcome = harness.store.admit(stale_command)

    assert outcome == QueueAdmissionRevisionConflict(
        QueueProjectionRevision(7), QueueProjectionRevision(0)
    )
    assert _row(harness.engine, reference.item_id.value) == before


@pytest.mark.proves("a-work-item-is-admitted-into-the-queue")
def test_repeating_the_same_admission_succeeds_without_mutating_the_row(
    harness: QueueHarness,
) -> None:
    lineage = _founded_workflow_lineage(harness.engine)
    reference = _reference()
    admission = QueueAdmission(
        lineage.lineage_id, QueueAdmissionRationale("matches the triage rule")
    )
    first = harness.store.admit(
        AdmitQueueItem(reference, admission, QueueProjectionRevision(0))
    )
    assert isinstance(first, QueueItemAdmitted)
    admitted_row = _row(harness.engine, reference.item_id.value)

    outcome = harness.store.admit(
        AdmitQueueItem(reference, admission, QueueProjectionRevision(0))
    )

    assert outcome == QueueAdmissionAlreadyCurrent(
        reference, admission, QueueProjectionRevision(1)
    )
    assert _row(harness.engine, reference.item_id.value) == admitted_row


def test_admitting_under_a_different_workflow_binding_is_refused_unaltered(
    harness: QueueHarness,
) -> None:
    lineage = _founded_workflow_lineage(harness.engine)
    other_lineage = _founded_workflow_lineage(harness.engine)
    reference = _reference()
    original = QueueAdmission(
        lineage.lineage_id, QueueAdmissionRationale("matches the triage rule")
    )
    harness.store.admit(AdmitQueueItem(reference, original, QueueProjectionRevision(0)))
    admitted_row = _row(harness.engine, reference.item_id.value)
    conflicting = QueueAdmission(
        other_lineage.lineage_id, QueueAdmissionRationale("a different reason")
    )

    outcome = harness.store.admit(
        AdmitQueueItem(reference, conflicting, QueueProjectionRevision(0))
    )

    assert outcome == QueueAdmissionAlreadyDecided(reference, original)
    assert _row(harness.engine, reference.item_id.value) == admitted_row


def test_queue_item_identity_columns_are_immutable(harness: QueueHarness) -> None:
    reference = _reference()
    with harness.engine.connect() as connection:
        connection.execute(
            queue_items.insert().values(
                item_id=reference.item_id.value,
                project_id=reference.project.value,
                tracker_item_reference=reference.tracker_item.value,
                state=QueueItemState.OBSERVED.value,
                state_version=0,
                workflow_lineage_id=None,
                admission_rationale=None,
            )
        )
        connection.commit()

    with harness.engine.connect() as connection, pytest.raises(exc.IntegrityError):
        connection.execute(
            queue_items.update()
            .where(queue_items.c.item_id == reference.item_id.value)
            .values(project_id="another-project")
        )


def test_queue_items_cannot_be_deleted(harness: QueueHarness) -> None:
    reference = _reference()
    with harness.engine.connect() as connection:
        connection.execute(
            queue_items.insert().values(
                item_id=reference.item_id.value,
                project_id=reference.project.value,
                tracker_item_reference=reference.tracker_item.value,
                state=QueueItemState.OBSERVED.value,
                state_version=0,
                workflow_lineage_id=None,
                admission_rationale=None,
            )
        )
        connection.commit()

    with harness.engine.connect() as connection, pytest.raises(exc.IntegrityError):
        connection.execute(
            queue_items.delete().where(queue_items.c.item_id == reference.item_id.value)
        )


def test_an_admission_survives_a_process_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    lineage = _founded_workflow_lineage(engine)
    reference = _reference()
    admission = QueueAdmission(
        lineage.lineage_id, QueueAdmissionRationale("matches the triage rule")
    )
    outcome = DbosQueueProjectionStore(engine).admit(
        AdmitQueueItem(reference, admission, QueueProjectionRevision(0))
    )
    assert isinstance(outcome, QueueItemAdmitted)
    engine.dispose()

    reopened_engine = create_canonical_engine(database_path)
    initialize_schema(reopened_engine)
    try:
        row = _row(reopened_engine, reference.item_id.value)
        assert row is not None
        assert row[3] == QueueItemState.ADMITTED.value
        assert row[4] == 1
        assert row[5] == lineage.lineage_id.value
    finally:
        reopened_engine.dispose()
