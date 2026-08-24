"""Launch starts every admitted queue item's workflow, once, end to end.

Phase A and B proved the admission and its doors against the real store; this
proves the bridge against the real runtime: an admitted item, a founded
workflow lineage, and a launch -- and a live run appears with no further hand.
A relaunch on the same database is the restart: the sweep re-derives the same
identity and the starter answers with the run that already exists, so the run
is never started twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    AdmitQueueItem,
    QueueAdmission,
    QueueAdmissionRationale,
    QueueItemAdmitted,
    QueueProjectionRevision,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import WorkflowRevision
from atelier2.ports.published_revisions import CatalogLineageFounded
from tests.scenarios.runs import publish_revision
from tests.scenarios.runtime import exact_output_runtime

BINDING_FREE_WORKFLOW = b"""format_version: 1
start: final
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
"""


def _settings(database_path: Path) -> DbosRuntimeSettings:
    return DbosRuntimeSettings(database_path, "queue-auto-start-test")


def _runtime(database_path: Path) -> DbosRuntime:
    return exact_output_runtime(
        _settings(database_path),
        LoopbackEffectAdapterFactory(
            database_path.parent / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )


def _found_workflow_lineage(engine: Engine, document: bytes) -> CatalogLineageId:
    """Publish the workflow's bytes and take a catalog name for them at head."""
    publish_revision(engine, WorkflowRevision(document))
    catalog = DbosCatalogStore(engine)
    published = PublishedRevision(RevisionKind.WORKFLOW, document)
    catalog.publish_revision(published)
    founded = catalog.found_lineage(
        published,
        CatalogLineageDisplayName("triage-workflow"),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-24T09:00:00Z"),
    )
    assert isinstance(founded, CatalogLineageFounded)
    return founded.lineage.lineage_id


def _admit_item(engine: Engine, tracker: str, lineage_id: CatalogLineageId) -> None:
    reference = WorkItemReference(ProjectId("project1"), TrackerItemReference(tracker))
    admitted = DbosQueueProjectionStore(engine).admit(
        AdmitQueueItem(
            reference,
            QueueAdmission(
                lineage_id, QueueAdmissionRationale("the triage rule matched")
            ),
            QueueProjectionRevision(0),
        )
    )
    assert isinstance(admitted, QueueItemAdmitted)


def _run_count(engine: Engine) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(sa.text("SELECT COUNT(*) FROM runs")) or 0)


@pytest.fixture
def database_path(tmp_path: Path) -> Path:
    return tmp_path / "atelier.sqlite"


def test_launch_starts_an_admitted_items_workflow(database_path: Path) -> None:
    runtime = _runtime(database_path)
    try:
        lineage_id = _found_workflow_lineage(runtime.engine, BINDING_FREE_WORKFLOW)
        _admit_item(runtime.engine, "gh:79", lineage_id)

        runtime.launch()

        with runtime.engine.connect() as connection:
            rows = connection.execute(sa.text("SELECT run_id, state FROM runs")).all()
        assert len(rows) == 1
    finally:
        runtime.close()


def test_a_relaunch_after_restart_does_not_start_the_workflow_twice(
    database_path: Path,
) -> None:
    first = _runtime(database_path)
    try:
        lineage_id = _found_workflow_lineage(first.engine, BINDING_FREE_WORKFLOW)
        _admit_item(first.engine, "gh:79", lineage_id)
        first.launch()
        assert _run_count(first.engine) == 1
    finally:
        first.close()

    reopened = _runtime(database_path)
    try:
        reopened.launch()
        assert _run_count(reopened.engine) == 1
    finally:
        reopened.close()
