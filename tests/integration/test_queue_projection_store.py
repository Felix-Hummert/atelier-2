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
    QueueItemId,
    QueueItemSnapshot,
    QueueItemState,
    QueueProjectionRevision,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.ports.published_revisions import CatalogLineageFounded
from atelier2.ports.queue_projection import (
    AdmittedQueueItemsPage,
    ObservedQueueItemsPage,
    QueueItemsObserved,
)


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


def _founded_workflow_lineage(engine: Engine, name: str) -> CatalogLineage:
    catalog = DbosCatalogStore(engine)
    published = PublishedRevision(
        RevisionKind.WORKFLOW, f"queue-workflow-document-{name}".encode()
    )
    catalog.publish_revision(published)
    result = catalog.found_lineage(
        published,
        CatalogLineageDisplayName(f"queue-workflow-{name}"),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-23T09:00:00Z"),
    )
    assert isinstance(result, CatalogLineageFounded)
    return result.lineage


def _reference(tracker_item: str = "gh:79") -> WorkItemReference:
    return WorkItemReference(ProjectId("project1"), TrackerItemReference(tracker_item))


def _row(engine: Engine, item_id: str) -> tuple[object, ...] | None:
    with engine.connect() as connection:
        record = connection.execute(
            sa.select(queue_items).where(queue_items.c.item_id == item_id)
        ).one_or_none()
        return None if record is None else tuple(record)


def _row_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(
            connection.scalar(sa.select(sa.func.count()).select_from(queue_items)) or 0
        )


@pytest.mark.proves("a-work-item-is-admitted-into-the-queue")
def test_admitting_a_fresh_item_writes_its_durable_admission(
    harness: QueueHarness,
) -> None:
    lineage = _founded_workflow_lineage(harness.engine, "triage")
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
    lineage = _founded_workflow_lineage(harness.engine, "triage")
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
def test_a_refused_first_admission_still_durably_establishes_the_observed_row(
    harness: QueueHarness,
) -> None:
    """The one path where a refusal writes: it establishes identity, not admission.

    Nothing durable named this item before this call. The store's
    insert-or-ignore establishes its `OBSERVED` row within the same
    transaction the CAS decision refuses in -- ADR 0016 names this design;
    this test pins it. A second refusal against the same wrong revision adds
    nothing further.
    """

    lineage = _founded_workflow_lineage(harness.engine, "triage")
    reference = _reference()
    admission = QueueAdmission(
        lineage.lineage_id, QueueAdmissionRationale("matches the triage rule")
    )
    wrong_command = AdmitQueueItem(reference, admission, QueueProjectionRevision(5))

    outcome = harness.store.admit(wrong_command)

    assert outcome == QueueAdmissionRevisionConflict(
        QueueProjectionRevision(5), QueueProjectionRevision(0)
    )
    established_row = _row(harness.engine, reference.item_id.value)
    assert established_row is not None
    (
        item_id,
        project_id,
        tracker_item_reference,
        state,
        state_version,
        workflow_lineage_id,
        admission_rationale,
    ) = established_row
    assert item_id == reference.item_id.value
    assert project_id == "project1"
    assert tracker_item_reference == "gh:79"
    assert state == QueueItemState.OBSERVED.value
    assert state_version == 0
    assert workflow_lineage_id is None
    assert admission_rationale is None
    assert _row_count(harness.engine) == 1

    second_outcome = harness.store.admit(wrong_command)

    assert second_outcome == QueueAdmissionRevisionConflict(
        QueueProjectionRevision(5), QueueProjectionRevision(0)
    )
    assert _row(harness.engine, reference.item_id.value) == established_row
    assert _row_count(harness.engine) == 1


@pytest.mark.proves("a-work-item-is-admitted-into-the-queue")
def test_repeating_the_same_admission_succeeds_without_mutating_the_row(
    harness: QueueHarness,
) -> None:
    lineage = _founded_workflow_lineage(harness.engine, "triage")
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
    lineage = _founded_workflow_lineage(harness.engine, "triage")
    other_lineage = _founded_workflow_lineage(harness.engine, "escalation")
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
    lineage = _founded_workflow_lineage(engine, "triage")
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


def test_listing_an_empty_queue_answers_an_empty_page(harness: QueueHarness) -> None:
    page = harness.store.list_admitted_items(None, 50)

    assert page == AdmittedQueueItemsPage((), None)


def test_listing_admitted_items_carries_each_ones_binding_and_rationale(
    harness: QueueHarness,
) -> None:
    lineage = _founded_workflow_lineage(harness.engine, "triage")
    reference = _reference()
    admission = QueueAdmission(
        lineage.lineage_id, QueueAdmissionRationale("matches the triage rule")
    )
    outcome = harness.store.admit(
        AdmitQueueItem(reference, admission, QueueProjectionRevision(0))
    )
    assert isinstance(outcome, QueueItemAdmitted)

    page = harness.store.list_admitted_items(None, 50)

    assert page == AdmittedQueueItemsPage(
        (
            QueueItemSnapshot(
                reference,
                QueueItemState.ADMITTED,
                QueueProjectionRevision(1),
                admission,
            ),
        ),
        None,
    )


def test_listing_admitted_items_omits_items_still_only_observed(
    harness: QueueHarness,
) -> None:
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

    page = harness.store.list_admitted_items(None, 50)

    assert page == AdmittedQueueItemsPage((), None)


def test_observing_fresh_references_creates_one_observed_row_each(
    harness: QueueHarness,
) -> None:
    references = (_reference("gh:1"), _reference("gh:2"))

    outcome = harness.store.observe(references)

    assert outcome == QueueItemsObserved(observed=2, newly_observed=2)
    page = harness.store.list_observed_items(None, 50)
    assert isinstance(page, ObservedQueueItemsPage)
    assert {item.item_reference for item in page.items} == set(references)
    assert all(
        item.state is QueueItemState.OBSERVED
        and item.revision == QueueProjectionRevision(0)
        and item.admission is None
        for item in page.items
    )


def test_observing_the_same_references_again_adds_nothing(
    harness: QueueHarness,
) -> None:
    references = (_reference("gh:1"), _reference("gh:2"))
    assert harness.store.observe(references) == QueueItemsObserved(2, 2)

    repeated = harness.store.observe((*references, _reference("gh:3")))

    assert repeated == QueueItemsObserved(observed=3, newly_observed=1)
    assert _row_count(harness.engine) == 3


def test_observing_an_already_admitted_item_never_rewinds_its_admission(
    harness: QueueHarness,
) -> None:
    lineage = _founded_workflow_lineage(harness.engine, "triage")
    reference = _reference()
    admission = QueueAdmission(
        lineage.lineage_id, QueueAdmissionRationale("matches the triage rule")
    )
    admitted = harness.store.admit(
        AdmitQueueItem(reference, admission, QueueProjectionRevision(0))
    )
    assert isinstance(admitted, QueueItemAdmitted)
    admitted_row = _row(harness.engine, reference.item_id.value)

    outcome = harness.store.observe((reference,))

    assert outcome == QueueItemsObserved(observed=1, newly_observed=0)
    assert _row(harness.engine, reference.item_id.value) == admitted_row


def test_observing_an_empty_batch_writes_nothing(harness: QueueHarness) -> None:
    assert harness.store.observe(()) == QueueItemsObserved(0, 0)
    assert _row_count(harness.engine) == 0


def test_listing_observed_items_omits_the_admitted_and_pages_by_item_id(
    harness: QueueHarness,
) -> None:
    lineage = _founded_workflow_lineage(harness.engine, "triage")
    references = [_reference(f"gh:{number}") for number in range(1, 5)]
    assert harness.store.observe(tuple(references)) == QueueItemsObserved(4, 4)
    admitted = harness.store.admit(
        AdmitQueueItem(
            references[0],
            QueueAdmission(
                lineage.lineage_id, QueueAdmissionRationale("matches the triage rule")
            ),
            QueueProjectionRevision(0),
        )
    )
    assert isinstance(admitted, QueueItemAdmitted)
    expected_order = sorted(
        references[1:], key=lambda reference: reference.item_id.value
    )

    first_page = harness.store.list_observed_items(None, 2)

    assert isinstance(first_page, ObservedQueueItemsPage)
    assert [item.item_reference for item in first_page.items] == expected_order[:2]
    assert first_page.next_after == QueueItemId(expected_order[1].item_id.value)

    second_page = harness.store.list_observed_items(first_page.next_after, 2)

    assert isinstance(second_page, ObservedQueueItemsPage)
    assert [item.item_reference for item in second_page.items] == expected_order[2:]
    assert second_page.next_after is None


def test_listing_admitted_items_pages_by_item_id_and_reports_where_to_resume(
    harness: QueueHarness,
) -> None:
    lineage = _founded_workflow_lineage(harness.engine, "triage")
    admission = QueueAdmission(
        lineage.lineage_id, QueueAdmissionRationale("matches the triage rule")
    )
    references = [_reference(f"gh:{number}") for number in range(3)]
    for reference in references:
        outcome = harness.store.admit(
            AdmitQueueItem(reference, admission, QueueProjectionRevision(0))
        )
        assert isinstance(outcome, QueueItemAdmitted)
    expected_order = sorted(references, key=lambda reference: reference.item_id.value)

    first_page = harness.store.list_admitted_items(None, 2)

    assert isinstance(first_page, AdmittedQueueItemsPage)
    assert len(first_page.items) == 2
    assert [item.item_reference for item in first_page.items] == expected_order[:2]
    assert first_page.next_after == QueueItemId(expected_order[1].item_id.value)

    second_page = harness.store.list_admitted_items(first_page.next_after, 2)

    assert isinstance(second_page, AdmittedQueueItemsPage)
    assert [item.item_reference for item in second_page.items] == expected_order[2:]
    assert second_page.next_after is None
