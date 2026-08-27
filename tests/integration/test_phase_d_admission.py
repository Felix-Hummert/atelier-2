"""Phase D1: one inspected proposal binds one exact run across every restart."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

import atelier2.application.advance_queue as advance_queue_module
from atelier2.adapters.dbos import schema as schema_module
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
    create_canonical_engine,
)
from atelier2.adapters.dbos.schema import (
    PRODUCT_SCHEMA_HANDOFF,
    SCHEMA_VERSION,
    V43_SCHEMA_HANDOFF,
    MigrationRequired,
    StoreMigrationRefused,
    initialize_schema,
    migrate_store,
    queue_launch_bindings,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import (
    API_PREFIX,
    PROJECT_QUEUE_POLICY_PATH,
    QUEUE_ADMISSIONS_PATH,
    QUEUE_ITEMS_PATH,
    QUEUE_PROPOSALS_PATH,
)
from atelier2.api.references import encode_public_project_reference
from atelier2.application.advance_queue import QueueRunStarted
from atelier2.application.start_published_run import RunCreated
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    ConfirmQueueProposal,
    PlanQueueItem,
    QueueAdmissionAlreadyCurrent,
    QueueAdmissionAuthorityRefused,
    QueueAdmissionRationale,
    QueueAutomationDisposition,
    QueueBlockerKind,
    QueueDecisionAuthority,
    QueueItemAdmitted,
    QueueItemId,
    QueueItemProposed,
    QueueItemState,
    QueueLaunchBinding,
    QueuePriorityRank,
    QueueProjectionRevision,
    QueueProjectPolicyRevision,
    QueueProposal,
    QueueProposalAlreadyCurrent,
    QueueProposalRevisionConflict,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import (
    Run,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.durable_runs import DurablePublishedRunStarter
from atelier2.ports.published_revisions import CatalogLineageFounded
from atelier2.ports.queue_projection import (
    QueueItemsPage,
    QueueLaunchBlocked,
    QueueLaunchReserved,
    QueueProjectPolicyPublished,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.runs import publish_revision
from tests.scenarios.runtime import exact_output_runtime

PROJECT = ProjectId("project1")
BINDING_FREE_WORKFLOW = b"""format_version: 1
start: final
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
"""


def _runtime(database_path: Path) -> DbosRuntime:
    return exact_output_runtime(
        DbosRuntimeSettings(database_path, "phase-d-admission-test"),
        LoopbackEffectAdapterFactory(
            database_path.parent / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )


def _found_lineage(
    engine: Engine, document: bytes = BINDING_FREE_WORKFLOW
) -> tuple[CatalogLineageId, WorkflowRevisionHash]:
    revision = WorkflowRevision(document)
    publish_revision(engine, revision)
    catalog = DbosCatalogStore(engine)
    published = PublishedRevision(RevisionKind.WORKFLOW, document)
    catalog.publish_revision(published)
    founded = catalog.found_lineage(
        published,
        CatalogLineageDisplayName(f"phase-d-{revision.revision_hash.value[:8]}"),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-27T10:00:00Z"),
    )
    assert isinstance(founded, CatalogLineageFounded)
    return founded.lineage.lineage_id, revision.revision_hash


def _proposal(
    lineage_id: CatalogLineageId,
    prerequisites: tuple[QueueItemId, ...] = (),
    rank: int = 1,
) -> QueueProposal:
    return QueueProposal(
        QueuePriorityRank(rank),
        lineage_id,
        prerequisites,
        QueueAutomationDisposition.HUMAN_REQUIRED,
        1,
    )


def _prepare_admitted(
    store: DbosQueueProjectionStore,
    lineage_id: CatalogLineageId,
    tracker: str = "gh:79",
    prerequisites: tuple[QueueItemId, ...] = (),
    rank: int = 1,
) -> WorkItemReference:
    reference = WorkItemReference(PROJECT, TrackerItemReference(tracker))
    store.observe((reference,))
    proposed = store.plan(
        PlanQueueItem(
            reference,
            _proposal(lineage_id, prerequisites, rank),
            QueueProjectionRevision(0),
        )
    )
    assert isinstance(proposed, QueueItemProposed)
    admitted = store.confirm(
        ConfirmQueueProposal(
            reference,
            proposed.revision,
            QueueAdmissionRationale("operator approved the inspected proposal"),
        )
    )
    assert isinstance(admitted, QueueItemAdmitted)
    return reference


@pytest.fixture
def store(tmp_path: Path) -> Iterator[tuple[DbosQueueProjectionStore, Engine]]:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    try:
        yield DbosQueueProjectionStore(engine), engine
    finally:
        engine.dispose()


def test_proposal_and_manual_confirmation_are_separate_typed_transitions(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:79"))
    queue.observe((reference,))
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    command = PlanQueueItem(
        reference, _proposal(lineage_id), QueueProjectionRevision(0)
    )

    proposed = queue.plan(command)
    repeated_proposal = queue.plan(command)
    stale_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:80"))
    queue.observe((stale_reference,))
    stale = queue.plan(
        PlanQueueItem(
            stale_reference, _proposal(lineage_id), QueueProjectionRevision(9)
        )
    )

    assert isinstance(proposed, QueueItemProposed)
    assert repeated_proposal == QueueProposalAlreadyCurrent(
        reference, command.proposal, proposed.revision
    )
    assert isinstance(stale, QueueProposalRevisionConflict)
    admitted = queue.confirm(
        ConfirmQueueProposal(
            reference,
            proposed.revision,
            QueueAdmissionRationale("approved"),
        )
    )
    assert isinstance(admitted, QueueItemAdmitted)
    assert admitted.admission.authority is QueueDecisionAuthority.OPERATOR
    repeated_admission = queue.confirm(
        ConfirmQueueProposal(
            reference,
            proposed.revision,
            QueueAdmissionRationale("approved"),
        )
    )
    assert isinstance(repeated_admission, QueueAdmissionAlreadyCurrent)

    human_required_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:81"))
    queue.observe((human_required_reference,))
    human_required_proposal = queue.plan(
        PlanQueueItem(
            human_required_reference,
            _proposal(lineage_id),
            QueueProjectionRevision(0),
        )
    )
    assert isinstance(human_required_proposal, QueueItemProposed)
    refused_automation = queue.confirm(
        ConfirmQueueProposal(
            human_required_reference,
            human_required_proposal.revision,
            QueueAdmissionRationale("automation attempted confirmation"),
            QueueDecisionAuthority.AUTOMATION_RULE,
        )
    )
    assert refused_automation == QueueAdmissionAuthorityRefused(
        QueueDecisionAuthority.AUTOMATION_RULE,
        QueueAutomationDisposition.HUMAN_REQUIRED,
    )


def test_v44_fresh_shape_and_phase_d_vocabulary_are_exact(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()

    with sqlite3.connect(database_path) as connection:
        assert (
            schema_module._fingerprint_for_version(connection, 44)
            == PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256
        )
        table_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {table.name for table in schema_module._PHASE_D_QUEUE_TABLES} <= (
            table_names
        )
        trigger_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        assert set(schema_module._PHASE_D_QUEUE_IMMUTABILITY_TRIGGERS) <= trigger_names
        queue_shape = str(
            connection.execute(
                "SELECT sql FROM sqlite_master "
                "WHERE type = 'table' AND name = 'queue_items'"
            ).fetchone()[0]
        )
        assert {state.value for state in QueueItemState} <= set(queue_shape.split("'"))
    assert {blocker.value for blocker in QueueBlockerKind} == {
        "PRIORITY_UNSET",
        "HUMAN_REQUIRED",
        "PREREQUISITE_OPEN",
        "PREREQUISITE_FAILED",
        "CAP_REACHED",
        "BINDING_UNRESOLVED",
        "REQUIRED_ORDER_UNAVAILABLE",
        "START_REFUSED",
        "LEGACY_REVIEW_REQUIRED",
    }


def test_policy_and_launch_reservation_are_atomic_under_the_project_cap(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    policy = queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    assert isinstance(policy, QueueProjectPolicyPublished)
    references = tuple(
        _prepare_admitted(queue, lineage_id, f"gh:{number}") for number in (79, 80)
    )
    barrier = Barrier(2)

    def reserve(index: int) -> object:
        barrier.wait()
        reference = references[index]
        return queue.reserve_launch(
            QueueLaunchBinding(
                reference.item_id,
                QueueProjectionRevision(1),
                RunId(f"phase-d-run-{index}"),
                revision_hash,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reserve, range(2)))

    assert sum(isinstance(outcome, QueueLaunchReserved) for outcome in outcomes) == 1
    blocked = next(
        outcome for outcome in outcomes if isinstance(outcome, QueueLaunchBlocked)
    )
    assert QueueBlockerKind.CAP_REACHED in blocked.item.blockers


def test_dependencies_require_completed_and_ready_items_order_by_rank_then_id(
    store: tuple[DbosQueueProjectionStore, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 3, None), 0)
    prerequisite = _prepare_admitted(queue, lineage_id, "gh:prerequisite", rank=3)
    dependent = _prepare_admitted(
        queue,
        lineage_id,
        "gh:dependent",
        (prerequisite.item_id,),
        rank=1,
    )
    peer = _prepare_admitted(queue, lineage_id, "gh:peer", rank=1)
    prerequisite_run = RunId("phase-d-prerequisite")
    assert isinstance(
        queue.reserve_launch(
            QueueLaunchBinding(
                prerequisite.item_id,
                QueueProjectionRevision(1),
                prerequisite_run,
                revision_hash,
            )
        ),
        QueueLaunchReserved,
    )
    with engine.begin() as connection:
        connection.execute(
            schema_module.runs.insert().values(
                run_id=prerequisite_run.value,
                bootstrap_workflow_id="phase-d-prerequisite-bootstrap",
                revision_hash=revision_hash.value,
                workflow_format_version=1,
                current_node_id="final",
                current_round_ordinal=1,
                state=RunState.STARTED.value,
                state_version=0,
                last_event_sequence=0,
                terminal_hash=None,
            )
        )
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    open_dependent = next(
        item for item in page.items if item.item_reference == dependent
    )
    assert open_dependent.blockers == (QueueBlockerKind.PREREQUISITE_OPEN,)
    with engine.begin() as connection:
        connection.execute(
            schema_module.runs.update()
            .where(schema_module.runs.c.run_id == prerequisite_run.value)
            .values(
                state=RunState.COMPLETED.value,
                state_version=1,
                terminal_hash="ab" * 32,
            )
        )
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    completed_dependent = next(
        item for item in page.items if item.item_reference == dependent
    )
    assert completed_dependent.blockers == ()

    def started(
        run_id: RunId,
        workflow_revision_hash: WorkflowRevisionHash,
        _bindings: object,
        _starter: object,
        **_kwargs: object,
    ) -> RunCreated:
        return RunCreated(
            Run(run_id, workflow_revision_hash, RunState.STARTED, "final", 0, 0)
        )

    monkeypatch.setattr(advance_queue_module, "start_published_run", started)
    outcomes = advance_queue_module.advance_queue(
        queue,
        DbosCatalogStore(engine),
        cast(DurablePublishedRunStarter, object()),
    )
    started_items = [
        outcome.item_id for outcome in outcomes if isinstance(outcome, QueueRunStarted)
    ]
    assert started_items == [
        *sorted((dependent.item_id, peer.item_id), key=lambda item_id: item_id.value),
        prerequisite.item_id,
    ]


def _restore_v43(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        schema_module._rebuild_product_table(
            connection,
            schema_module.queue_items,
            "queue_items_v44",
            ("queue_items_identity_no_update", "queue_items_no_delete"),
            44,
            43,
        )
        for table in reversed(schema_module._PHASE_D_QUEUE_TABLES):
            connection.execute(f"DROP TABLE {table.name}")
        connection.execute("UPDATE atelier_schema_versions SET version = 43")
        connection.commit()
        schema_module._require_product_shape(connection, 43)


def _logical_dump(database_path: Path) -> tuple[str, ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(connection.iterdump())


@pytest.mark.proves("the-v43-hop-invents-no-queue-decision")
def test_v43_to_v44_preserves_populated_rows_and_invents_no_queue_decision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    lineage_id, _revision_hash = _found_lineage(engine)
    engine.dispose()
    _restore_v43(database_path)
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:legacy"))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO queue_items VALUES (?, ?, ?, 'ADMITTED', 1, ?, ?)",
            (
                reference.item_id.value,
                PROJECT.value,
                reference.tracker_item.value,
                lineage_id.value,
                "legacy approval",
            ),
        )
        connection.commit()

    report = migrate_store(database_path)

    assert report.source_version == V43_SCHEMA_HANDOFF.version
    assert report.target_version == SCHEMA_VERSION == 44
    assert report.fingerprint_sha256 == PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256
    reopened = create_canonical_engine(database_path)
    try:
        page = DbosQueueProjectionStore(reopened).list_items(None, 50)
        assert isinstance(page, QueueItemsPage)
        (legacy,) = page.items
        assert legacy.state is QueueItemState.ADMITTED
        assert legacy.admission is not None
        assert legacy.admission.rationale.value == "legacy approval"
        assert legacy.proposal is None
        assert legacy.launch_binding is None
        assert legacy.blockers == (QueueBlockerKind.LEGACY_REVIEW_REQUIRED,)
        with reopened.connect() as connection:
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(queue_launch_bindings)
                )
                == 0
            )
    finally:
        reopened.dispose()


def test_v44_migration_collision_and_failpoint_roll_back_exactly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collision_path = tmp_path / "collision.sqlite"
    engine = create_canonical_engine(collision_path)
    initialize_schema(engine)
    engine.dispose()
    _restore_v43(collision_path)
    with sqlite3.connect(collision_path) as connection:
        connection.execute("CREATE TABLE queue_items_before_phase_d (held INTEGER)")
        connection.commit()
    before_collision = _logical_dump(collision_path)
    with pytest.raises(StoreMigrationRefused, match="queue_items_before_phase_d"):
        migrate_store(collision_path)
    assert _logical_dump(collision_path) == before_collision

    failpoint_path = tmp_path / "failpoint.sqlite"
    engine = create_canonical_engine(failpoint_path)
    initialize_schema(engine)
    engine.dispose()
    _restore_v43(failpoint_path)
    before_failpoint = _logical_dump(failpoint_path)
    original = schema_module._SCHEMA_MIGRATION_BY_SOURCE[43]

    def fail_after_step(connection: sqlite3.Connection) -> None:
        original.apply(connection)
        raise sqlite3.OperationalError("v44-after-version-cas-failpoint")

    monkeypatch.setitem(
        schema_module._SCHEMA_MIGRATION_BY_SOURCE,
        43,
        replace(original, apply=fail_after_step),
    )
    with pytest.raises(StoreMigrationRefused, match="v44-after-version-cas-failpoint"):
        migrate_store(failpoint_path)
    assert _logical_dump(failpoint_path) == before_failpoint


def test_runtime_refuses_an_unmigrated_v43_store(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    _restore_v43(database_path)

    with pytest.raises(MigrationRequired):
        initialize_schema(create_canonical_engine(database_path))


@pytest.mark.parametrize("crash_after_start", [False, True])
@pytest.mark.proves("a-manually-approved-queue-item-starts-once")
def test_one_manually_approved_item_starts_once_across_a_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    crash_after_start: bool,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    first = _runtime(database_path)
    first.initialize_storage()
    lineage_id, _revision_hash = _found_lineage(first.engine)
    queue = DbosQueueProjectionStore(first.engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    original_start = advance_queue_module.start_published_run

    def crash(*args: Any, **kwargs: Any) -> object:
        if crash_after_start:
            original_start(*args, **kwargs)
        raise RuntimeError("simulated process crash")

    monkeypatch.setattr(advance_queue_module, "start_published_run", crash)
    with pytest.raises(RuntimeError, match="simulated process crash"):
        first.launch()
    first.close()
    monkeypatch.setattr(advance_queue_module, "start_published_run", original_start)

    reopened = _runtime(database_path)
    try:
        reopened.launch()
        with reopened.engine.connect() as connection:
            runs = connection.execute(
                sa.text("SELECT run_id, revision_hash FROM runs")
            ).all()
            bindings = (
                connection.execute(
                    sa.select(queue_launch_bindings).where(
                        queue_launch_bindings.c.item_id == reference.item_id.value
                    )
                )
                .mappings()
                .all()
            )
        assert len(runs) == len(bindings) == 1
        assert runs[0].run_id == bindings[0]["run_id"]
        assert runs[0].revision_hash == bindings[0]["workflow_revision_hash"]
    finally:
        reopened.close()


@pytest.mark.proves("a-manually-approved-queue-item-starts-once")
def test_a_moved_lineage_head_does_not_launch_the_item_again(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    first = _runtime(database_path)
    lineage_id, original_revision = _found_lineage(first.engine)
    queue = DbosQueueProjectionStore(first.engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    _prepare_admitted(queue, lineage_id)
    first.launch()
    first.close()

    second = _runtime(database_path)
    changed = BINDING_FREE_WORKFLOW.replace(b"[2, 3]", b"[3, 4]")
    revision = WorkflowRevision(changed)
    publish_revision(second.engine, revision)
    catalog = DbosCatalogStore(second.engine)
    published = PublishedRevision(RevisionKind.WORKFLOW, changed)
    catalog.publish_revision(published)
    admitted = catalog.admit_member(
        lineage_id,
        published,
        CatalogLineageDisplayName(f"phase-d-{original_revision.value[:8]}"),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-27T11:00:00Z"),
    )
    assert admitted is not None
    try:
        second.launch()
        with second.engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT COUNT(*) FROM runs")) == 1
            assert (
                connection.scalar(
                    sa.select(queue_launch_bindings.c.workflow_revision_hash)
                )
                == original_revision.value
            )
    finally:
        second.close()


def test_queue_api_exposes_one_typed_projection_and_confirmation_matrix(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path / "atelier.sqlite")
    runtime.initialize_storage()
    api: TestClient = durable_api_client(runtime)
    lineage_id, _revision_hash = _found_lineage(runtime.engine)
    public_project = encode_public_project_reference(PROJECT)
    policy_path = PROJECT_QUEUE_POLICY_PATH.replace(
        "{public_project_reference}", public_project
    )
    try:
        policy = api.put(
            policy_path,
            json={
                "revision_number": 1,
                "expected_revision": 0,
                "maximum_active_runs": 1,
                "automation_label": None,
            },
        )
        assert policy.status_code == 201, policy.text
        proposal = api.put(
            QUEUE_PROPOSALS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": "gh:79",
                "expected_revision": 0,
                "priority": {"rank": 1},
                "workflow_lineage_id": lineage_id.value,
                "prerequisite_item_ids": [],
                "automation_disposition": "HUMAN_REQUIRED",
                "policy_revision": 1,
            },
        )
        assert proposal.status_code == 201, proposal.text
        stale = api.post(
            QUEUE_ADMISSIONS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": "gh:79",
                "expected_revision": 0,
                "rationale": "approved",
            },
        )
        assert stale.status_code == 409
        admitted = api.post(
            QUEUE_ADMISSIONS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": "gh:79",
                "expected_revision": 1,
                "rationale": "approved",
            },
        )
        assert admitted.status_code == 201, admitted.text
        assert admitted.json()["admission"]["authority"] == "OPERATOR"
        projection = api.get(QUEUE_ITEMS_PATH)
        assert projection.status_code == 200, projection.text
        (row,) = projection.json()["items"]
        assert row["state"] == "ADMITTED"
        assert row["proposal"]["priority"] == {"rank": 1}
        assert row["tracker_enrichment"] == "ENRICHMENT_UNAVAILABLE"
        assert row["title"] is None
        assert api.get(API_PREFIX + "/observed-queue-items").status_code == 404
    finally:
        runtime.close()
