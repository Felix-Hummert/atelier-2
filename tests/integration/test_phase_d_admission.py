"""Phase D1: one inspected proposal binds one exact run across every restart."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any, cast

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.sql.functions import Function

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
from atelier2.api.app import create_app
from atelier2.api.openapi import (
    API_PREFIX,
    PROJECT_QUEUE_POLICY_PATH,
    QUEUE_ADMISSIONS_PATH,
    QUEUE_ITEMS_PATH,
    QUEUE_PROPOSALS_PATH,
)
from atelier2.api.references import encode_public_project_reference
from atelier2.application.advance_queue import (
    QueueAdvanceCorrupt,
    QueueAdvanceUnavailable,
    QueueItemBlocked,
    QueueRunStarted,
)
from atelier2.application.refusals import (
    DurableStateCorrupt as StartDurableStateCorrupt,
)
from atelier2.application.refusals import WriteUnavailable
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
    QueueItemSnapshot,
    QueueItemState,
    QueueLaunchBinding,
    QueuePriorityRank,
    QueueProjectionRevision,
    QueueProjectPolicyRevision,
    QueueProposal,
    QueueProposalAlreadyCurrent,
    QueueProposalRefusal,
    QueueProposalRefused,
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
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    DurableStateCorrupt,
    DurableWriteUnavailable,
)
from atelier2.ports.published_revisions import (
    CatalogLineageFounded,
    PublishedRevisionsUnavailable,
)
from atelier2.ports.queue_projection import (
    QueueItemsPage,
    QueueLaunchBlocked,
    QueueLaunchReserved,
    QueueProjectPolicyPublished,
    QueueReadUnavailable,
)
from tests.scenarios.api import (
    api_limits,
    api_ports,
    durable_api_client,
    event_poll_backoff,
)
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


def _insert_dependency_proposal(
    engine: Engine,
    item: WorkItemReference,
    lineage_id: CatalogLineageId,
    prerequisite: WorkItemReference,
    revision: int,
) -> None:
    with engine.begin() as connection:
        connection.execute(
            schema_module.queue_proposal_revisions.insert().values(
                item_id=item.item_id.value,
                proposal_revision=revision,
                project_id=item.project.value,
                priority_rank=1,
                workflow_lineage_id=lineage_id.value,
                automation_disposition=QueueAutomationDisposition.HUMAN_REQUIRED.value,
                policy_revision=1,
            )
        )
        connection.execute(
            schema_module.queue_dependency_edges.insert().values(
                item_id=item.item_id.value,
                proposal_revision=revision,
                project_id=item.project.value,
                prerequisite_item_id=prerequisite.item_id.value,
            )
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


def test_current_fresh_shape_keeps_phase_d_vocabulary_exact(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()

    with sqlite3.connect(database_path) as connection:
        assert (
            schema_module._fingerprint_for_version(connection, SCHEMA_VERSION)
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


def test_v44_check_rejects_a_sql_null_partial_admission(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:partial-check"))
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO queue_items "
            "(item_id, project_id, tracker_item_reference, state, state_version) "
            "VALUES (?, ?, ?, 'OBSERVED', 0)",
            (
                reference.item_id.value,
                reference.project.value,
                reference.tracker_item.value,
            ),
        )
        connection.execute("DROP TRIGGER queue_items_state_transition")
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "UPDATE queue_items SET state='ADMITTED', state_version=2, "
                "workflow_lineage_id=?, admission_rationale='approved', "
                "current_proposal_revision=1 WHERE item_id=?",
                ("a1" * 32, reference.item_id.value),
            )


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


def test_missing_policy_fails_launch_reservation_as_corrupt_without_a_binding(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:no-policy"))
    queue.observe((reference,))
    proposed = queue.plan(
        PlanQueueItem(
            reference,
            QueueProposal(
                QueuePriorityRank(1),
                lineage_id,
                (),
                QueueAutomationDisposition.HUMAN_REQUIRED,
                None,
            ),
            QueueProjectionRevision(0),
        )
    )
    assert isinstance(proposed, QueueItemProposed)
    admitted = queue.confirm(
        ConfirmQueueProposal(
            reference,
            proposed.revision,
            QueueAdmissionRationale("approved without an invented policy"),
        )
    )
    assert isinstance(admitted, QueueItemAdmitted)

    result = queue.reserve_launch(
        QueueLaunchBinding(
            reference.item_id,
            proposed.revision,
            RunId("missing-policy-run"),
            revision_hash,
        )
    )

    assert isinstance(result, DurableStateCorrupt)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(queue_launch_bindings)
            )
            == 0
        )


def test_unreadable_capacity_count_fails_the_reservation_loud(
    store: tuple[DbosQueueProjectionStore, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    scalar = cast(Any, sa.engine.Connection.scalar)

    def unreadable_count(
        connection: sa.engine.Connection, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if any(
            isinstance(element, Function) and element.name.lower() == "count"
            for element in sa.sql.visitors.iterate(statement)
        ):
            return None
        return scalar(connection, statement, *args, **kwargs)

    monkeypatch.setattr(sa.engine.Connection, "scalar", unreadable_count)

    result = queue.reserve_launch(
        QueueLaunchBinding(
            reference.item_id,
            QueueProjectionRevision(1),
            RunId("unreadable-count-run"),
            revision_hash,
        )
    )

    assert isinstance(result, DurableStateCorrupt)


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


@pytest.mark.parametrize("proposal_revision", [None, 1])
def test_phase_d_state_without_its_proposal_fails_loud(
    store: tuple[DbosQueueProjectionStore, Engine], proposal_revision: int | None
) -> None:
    queue, engine = store
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:corrupt-proposed"))
    queue.observe((reference,))
    with sqlite3.connect(str(engine.url.database)) as connection:
        connection.execute("DROP TRIGGER queue_items_state_transition")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE queue_items SET state='PROPOSED', state_version=1, "
            "current_proposal_revision=? WHERE item_id=?",
            (proposal_revision, reference.item_id.value),
        )
        connection.commit()

    assert isinstance(queue.list_items(None, 50), DurableStateCorrupt)


def test_unknown_prerequisite_run_state_fails_loud(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 2, None), 0)
    prerequisite = _prepare_admitted(queue, lineage_id, "gh:corrupt-run")
    _prepare_admitted(
        queue, lineage_id, "gh:dependent-on-corrupt-run", (prerequisite.item_id,)
    )
    run_id = RunId("corrupt-prerequisite-run")
    assert isinstance(
        queue.reserve_launch(
            QueueLaunchBinding(
                prerequisite.item_id,
                QueueProjectionRevision(1),
                run_id,
                revision_hash,
            )
        ),
        QueueLaunchReserved,
    )
    with engine.begin() as connection:
        connection.execute(
            schema_module.runs.insert().values(
                run_id=run_id.value,
                bootstrap_workflow_id="corrupt-prerequisite-bootstrap",
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
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            schema_module.runs.update()
            .where(schema_module.runs.c.run_id == run_id.value)
            .values(state="UNKNOWN_DURABLE_STATE")
        )

    assert isinstance(queue.list_items(None, 50), DurableStateCorrupt)


def test_plan_fails_loud_when_derived_identity_collides_with_another_reference(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:identity"))
    with engine.begin() as connection:
        connection.execute(
            schema_module.queue_items.insert().values(
                item_id=reference.item_id.value,
                project_id="different-project",
                tracker_item_reference="gh:different",
                state=QueueItemState.OBSERVED.value,
                state_version=0,
            )
        )

    result = queue.plan(
        PlanQueueItem(reference, _proposal(lineage_id), QueueProjectionRevision(0))
    )

    assert isinstance(result, DurableStateCorrupt)


def test_dependency_cycle_check_ignores_superseded_same_project_edges(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    first = WorkItemReference(PROJECT, TrackerItemReference("gh:first"))
    second = WorkItemReference(PROJECT, TrackerItemReference("gh:second"))
    queue.observe((first, second))
    assert isinstance(
        queue.plan(
            PlanQueueItem(
                first,
                QueueProposal(
                    QueuePriorityRank(1),
                    lineage_id,
                    (second.item_id,),
                    QueueAutomationDisposition.HUMAN_REQUIRED,
                    1,
                ),
                QueueProjectionRevision(0),
            )
        ),
        QueueItemProposed,
    )
    _insert_dependency_proposal(engine, second, lineage_id, first, 99)
    command_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:no-deps"))

    result = queue.plan(
        PlanQueueItem(
            command_reference,
            _proposal(lineage_id),
            QueueProjectionRevision(0),
        )
    )

    assert isinstance(result, QueueItemProposed)


def test_dependency_cycle_check_ignores_current_cross_project_edges(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    other_project = ProjectId("other-project")
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    queue.put_policy(QueueProjectPolicyRevision(other_project, 1, 1, None), 0)
    first = WorkItemReference(other_project, TrackerItemReference("gh:first"))
    second = WorkItemReference(other_project, TrackerItemReference("gh:second"))
    queue.observe((first, second))
    assert isinstance(
        queue.plan(
            PlanQueueItem(
                first,
                QueueProposal(
                    QueuePriorityRank(1),
                    lineage_id,
                    (second.item_id,),
                    QueueAutomationDisposition.HUMAN_REQUIRED,
                    1,
                ),
                QueueProjectionRevision(0),
            )
        ),
        QueueItemProposed,
    )
    _insert_dependency_proposal(engine, second, lineage_id, first, 1)
    with engine.begin() as connection:
        connection.execute(
            schema_module.queue_items.update()
            .where(schema_module.queue_items.c.item_id == second.item_id.value)
            .values(
                state=QueueItemState.PROPOSED.value,
                state_version=1,
                current_proposal_revision=1,
            )
        )
    command_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:no-deps"))

    result = queue.plan(
        PlanQueueItem(
            command_reference,
            _proposal(lineage_id),
            QueueProjectionRevision(0),
        )
    )

    assert isinstance(result, QueueItemProposed)


def test_queue_proposal_refusals_are_closed_typed_decisions(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)

    def plan(
        tracker: str,
        prerequisites: tuple[QueueItemId, ...],
        policy_revision: int = 1,
    ) -> object:
        reference = WorkItemReference(PROJECT, TrackerItemReference(tracker))
        return queue.plan(
            PlanQueueItem(
                reference,
                QueueProposal(
                    QueuePriorityRank(1),
                    lineage_id,
                    prerequisites,
                    QueueAutomationDisposition.HUMAN_REQUIRED,
                    policy_revision,
                ),
                QueueProjectionRevision(0),
            )
        )

    self_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:self"))
    self_refusal = queue.plan(
        PlanQueueItem(
            self_reference,
            QueueProposal(
                QueuePriorityRank(1),
                lineage_id,
                (self_reference.item_id,),
                QueueAutomationDisposition.HUMAN_REQUIRED,
                1,
            ),
            QueueProjectionRevision(0),
        )
    )
    missing_policy = plan("gh:policy-missing", (), 99)
    missing_prerequisite = plan("gh:prerequisite-missing", (QueueItemId("c3" * 32),))
    other_project_reference = WorkItemReference(
        ProjectId("other-project"), TrackerItemReference("gh:outside-project")
    )
    queue.observe((other_project_reference,))
    outside_project = plan("gh:outside-dependent", (other_project_reference.item_id,))
    first = WorkItemReference(PROJECT, TrackerItemReference("gh:cycle-first"))
    second = WorkItemReference(PROJECT, TrackerItemReference("gh:cycle-second"))
    queue.observe((first, second))
    assert isinstance(
        queue.plan(
            PlanQueueItem(
                first,
                _proposal(lineage_id, (second.item_id,)),
                QueueProjectionRevision(0),
            )
        ),
        QueueItemProposed,
    )
    cycle = queue.plan(
        PlanQueueItem(
            second,
            _proposal(lineage_id, (first.item_id,)),
            QueueProjectionRevision(0),
        )
    )

    assert self_refusal == QueueProposalRefused(QueueProposalRefusal.SELF_DEPENDENCY)
    assert missing_policy == QueueProposalRefused(
        QueueProposalRefusal.POLICY_REVISION_MISSING
    )
    assert missing_prerequisite == QueueProposalRefused(
        QueueProposalRefusal.PREREQUISITE_NOT_IN_PROJECT
    )
    assert outside_project == QueueProposalRefused(
        QueueProposalRefusal.PREREQUISITE_NOT_IN_PROJECT
    )
    assert cycle == QueueProposalRefused(QueueProposalRefusal.DEPENDENCY_CYCLE)
    api_reference = WorkItemReference(PROJECT, TrackerItemReference("gh:api-self"))
    with _queue_api(queue) as api:
        response = api.put(
            QUEUE_PROPOSALS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": api_reference.tracker_item.value,
                "expected_revision": 0,
                "priority": {"rank": 1},
                "workflow_lineage_id": lineage_id.value,
                "prerequisite_item_ids": [api_reference.item_id.value],
                "automation_disposition": "HUMAN_REQUIRED",
                "policy_revision": 1,
            },
        )
    assert response.status_code == 422
    assert response.json()["type"].endswith("queue-proposal-refused")


def _restore_v43(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for trigger in ("catalog_intakes_no_update", "catalog_intakes_no_delete"):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        connection.execute("DROP TABLE IF EXISTS catalog_intakes")
        schema_module._rebuild_product_table(
            connection,
            schema_module.run_events,
            "run_events_v46",
            schema_module._RUN_EVENTS_TRIGGERS,
            46,
            45,
        )
        schema_module._rebuild_product_table(
            connection,
            schema_module.wait_answers,
            "wait_answers_v46",
            schema_module._WAIT_ANSWERS_TRIGGERS,
            46,
            45,
            trigger_source=schema_module.PUBLISHED_WAIT_ANSWER_TRIGGERS[45],
        )
        schema_module._rebuild_product_table(
            connection,
            schema_module.host_project_source_connection_revisions,
            "project_source_connections_v45",
            (
                "host_project_source_connection_revisions_no_update",
                "host_project_source_connection_revisions_no_delete",
            ),
            45,
            44,
        )
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
    assert report.target_version == SCHEMA_VERSION == 47
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
        (outcome,) = advance_queue_module.advance_queue(
            DbosQueueProjectionStore(reopened),
            DbosCatalogStore(reopened),
            cast(DurablePublishedRunStarter, object()),
        )
        assert isinstance(outcome, QueueItemBlocked)
        assert outcome.blockers == (QueueBlockerKind.LEGACY_REVIEW_REQUIRED,)
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


def test_advance_replays_a_reserved_binding_before_projection_blockers(
    store: tuple[DbosQueueProjectionStore, Engine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    binding = QueueLaunchBinding(
        reference.item_id,
        QueueProjectionRevision(1),
        RunId("reserved-replay"),
        revision_hash,
    )
    assert isinstance(queue.reserve_launch(binding), QueueLaunchReserved)
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    (stored,) = page.items
    assert stored.blockers == ()

    class BoundQueue:
        def list_items(self, _after: object, _limit: int) -> QueueItemsPage:
            return QueueItemsPage(
                (replace(stored, blockers=(QueueBlockerKind.CAP_REACHED,)),), None
            )

        def reserve_launch(self, _binding: object) -> object:
            raise AssertionError("a stored binding must not be reserved again")

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
        cast(Any, BoundQueue()),
        DbosCatalogStore(engine),
        cast(DurablePublishedRunStarter, object()),
    )

    assert isinstance(outcomes[0], QueueRunStarted)
    assert outcomes[0].binding == binding


@pytest.mark.parametrize(
    ("read_answer", "expected_error"),
    [
        (QueueReadUnavailable(), QueueAdvanceUnavailable),
        (DurableStateCorrupt(), QueueAdvanceCorrupt),
        (object(), QueueAdvanceCorrupt),
    ],
)
def test_advance_classifies_queue_read_failures(
    read_answer: object,
    expected_error: type[RuntimeError],
) -> None:
    class ReadAnswerQueue:
        def list_items(self, _after: object, _limit: int) -> object:
            return read_answer

    with pytest.raises(expected_error):
        advance_queue_module.advance_queue(
            cast(Any, ReadAnswerQueue()),
            cast(Any, object()),
            cast(DurablePublishedRunStarter, object()),
        )


@pytest.mark.parametrize(
    "corruption",
    ["proposed-without-proposal", "authority-without-proposal-revision"],
)
def test_advance_refuses_incomplete_phase_d_projection_before_blockers(
    store: tuple[DbosQueueProjectionStore, Engine],
    corruption: str,
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id, f"gh:{corruption}")
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)
    stored = next(
        item for item in page.items if item.item_reference.item_id == reference.item_id
    )
    assert stored.admission is not None
    if corruption == "proposed-without-proposal":
        malformed = SimpleNamespace(
            item_reference=stored.item_reference,
            state=QueueItemState.PROPOSED,
            revision=QueueProjectionRevision(1),
            admission=None,
            proposal=None,
            launch_binding=None,
            blockers=(QueueBlockerKind.HUMAN_REQUIRED,),
        )
    else:
        malformed = SimpleNamespace(
            item_reference=stored.item_reference,
            state=stored.state,
            revision=stored.revision,
            admission=SimpleNamespace(
                workflow_lineage_id=lineage_id,
                rationale=stored.admission.rationale,
                authority=QueueDecisionAuthority.OPERATOR,
                proposal_revision=None,
            ),
            proposal=stored.proposal,
            launch_binding=None,
            blockers=(QueueBlockerKind.CAP_REACHED,),
        )

    class MalformedProjection:
        def list_items(self, _after: object, _limit: int) -> QueueItemsPage:
            return QueueItemsPage((cast(QueueItemSnapshot, malformed),), None)

        def reserve_launch(self, _binding: object) -> object:
            raise AssertionError("corrupt projection must fail before reservation")

    with pytest.raises(QueueAdvanceCorrupt, match="inconsistent item"):
        advance_queue_module.advance_queue(
            cast(Any, MalformedProjection()),
            DbosCatalogStore(engine),
            cast(DurablePublishedRunStarter, object()),
        )


@pytest.mark.parametrize(
    ("reservation_answer", "expected_error"),
    [
        (DurableWriteUnavailable(), QueueAdvanceUnavailable),
        (DurableStateCorrupt(), QueueAdvanceCorrupt),
        (object(), QueueAdvanceCorrupt),
    ],
)
def test_advance_classifies_launch_reservation_failures(
    store: tuple[DbosQueueProjectionStore, Engine],
    reservation_answer: object,
    expected_error: type[RuntimeError],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    _prepare_admitted(queue, lineage_id)
    page = queue.list_items(None, 50)
    assert isinstance(page, QueueItemsPage)

    class ReservationAnswerQueue:
        def list_items(self, _after: object, _limit: int) -> QueueItemsPage:
            return page

        def reserve_launch(self, _binding: object) -> object:
            return reservation_answer

    with pytest.raises(expected_error):
        advance_queue_module.advance_queue(
            cast(Any, ReservationAnswerQueue()),
            DbosCatalogStore(engine),
            cast(DurablePublishedRunStarter, object()),
        )


@pytest.mark.parametrize(
    ("catalog_answer", "expected_error"),
    [
        (PublishedRevisionsUnavailable(), QueueAdvanceUnavailable),
        (DurableStateCorrupt(), QueueAdvanceCorrupt),
        (object(), QueueAdvanceCorrupt),
    ],
)
def test_advance_classifies_catalog_resolution_failures(
    store: tuple[DbosQueueProjectionStore, Engine],
    catalog_answer: object,
    expected_error: type[RuntimeError],
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    _prepare_admitted(queue, lineage_id)

    class CatalogAnswer:
        def resolve_name(self, *_args: object) -> object:
            return catalog_answer

    with pytest.raises(expected_error):
        advance_queue_module.advance_queue(
            queue,
            cast(Any, CatalogAnswer()),
            cast(DurablePublishedRunStarter, object()),
        )


@pytest.mark.parametrize(
    ("start_failure", "expected_error"),
    [
        (WriteUnavailable(), QueueAdvanceUnavailable),
        (StartDurableStateCorrupt(), QueueAdvanceCorrupt),
        (object(), QueueAdvanceCorrupt),
    ],
)
def test_advance_classifies_reserved_run_start_failures(
    store: tuple[DbosQueueProjectionStore, Engine],
    monkeypatch: pytest.MonkeyPatch,
    start_failure: object,
    expected_error: type[RuntimeError],
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id)
    assert isinstance(
        queue.reserve_launch(
            QueueLaunchBinding(
                reference.item_id,
                QueueProjectionRevision(1),
                RunId("reserved-start-failure"),
                revision_hash,
            )
        ),
        QueueLaunchReserved,
    )
    monkeypatch.setattr(
        advance_queue_module,
        "start_published_run",
        lambda *_args, **_kwargs: start_failure,
    )

    with pytest.raises(expected_error):
        advance_queue_module.advance_queue(
            queue,
            DbosCatalogStore(engine),
            cast(DurablePublishedRunStarter, object()),
        )


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


def _queue_api(queue: object) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(queue_projection=queue),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


@pytest.mark.parametrize(
    "corruption",
    [
        "observed-revision",
        "observed-admission",
        "proposed-revision",
        "proposed-mismatched-revisions",
        "proposed-admission",
        "admitted-null-rationale",
    ],
)
def test_queue_api_fails_loud_for_illegal_raw_lifecycle_shapes(
    store: tuple[DbosQueueProjectionStore, Engine],
    corruption: str,
) -> None:
    queue, engine = store
    lineage_id, _revision_hash = _found_lineage(engine)
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = WorkItemReference(PROJECT, TrackerItemReference(f"gh:{corruption}"))
    if corruption.startswith("admitted"):
        _prepare_admitted(queue, lineage_id, reference.tracker_item.value)
    else:
        queue.observe((reference,))
        if corruption.startswith("proposed"):
            assert isinstance(
                queue.plan(
                    PlanQueueItem(
                        reference,
                        _proposal(lineage_id),
                        QueueProjectionRevision(0),
                    )
                ),
                QueueItemProposed,
            )
    corrupt_values_by_case: dict[str, dict[str, Any]] = {
        "observed-revision": {"state_version": 1},
        "observed-admission": {"admission_rationale": "ghost admission"},
        "proposed-revision": {"state_version": 0},
        "proposed-mismatched-revisions": {"state_version": 2},
        "proposed-admission": {"workflow_lineage_id": lineage_id.value},
        "admitted-null-rationale": {"admission_rationale": None},
    }
    corrupt_values = corrupt_values_by_case[corruption]
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER queue_items_state_transition")
        connection.exec_driver_sql("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            schema_module.queue_items.update()
            .where(schema_module.queue_items.c.item_id == reference.item_id.value)
            .values(**corrupt_values)
        )

    with _queue_api(queue) as api:
        response = api.get(QUEUE_ITEMS_PATH)

    assert response.status_code == 500
    assert response.json()["type"].endswith("durable-state-corrupt")


def test_queue_admission_authority_refusal_has_its_own_problem() -> None:
    class AuthorityRefusingQueue:
        def confirm(self, _command: object) -> QueueAdmissionAuthorityRefused:
            return QueueAdmissionAuthorityRefused(
                QueueDecisionAuthority.AUTOMATION_RULE,
                QueueAutomationDisposition.HUMAN_REQUIRED,
            )

    with _queue_api(AuthorityRefusingQueue()) as api:
        response = api.post(
            QUEUE_ADMISSIONS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": "gh:79",
                "expected_revision": 1,
                "rationale": "automation tried",
            },
        )

    assert response.status_code == 409
    assert response.json()["type"].endswith("queue-admission-authority-refused")


def test_queue_admission_api_requires_a_proposal_before_confirmation(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, _engine = store
    reference = WorkItemReference(PROJECT, TrackerItemReference("gh:unproposed"))
    queue.observe((reference,))

    with _queue_api(queue) as api:
        response = api.post(
            QUEUE_ADMISSIONS_PATH,
            json={
                "project_id": PROJECT.value,
                "tracker_item_reference": reference.tracker_item.value,
                "expected_revision": 0,
                "rationale": "cannot skip proposal",
            },
        )

    assert response.status_code == 409
    assert response.json()["type"].endswith("queue-admission-proposal-required")


def test_corrupt_admission_proposal_identity_fails_projection_api_and_start(
    store: tuple[DbosQueueProjectionStore, Engine],
) -> None:
    queue, engine = store
    lineage_id, revision_hash = _found_lineage(engine)
    other_lineage_id, _other_revision = _found_lineage(
        engine, BINDING_FREE_WORKFLOW.replace(b"[2, 3]", b"[4, 5]")
    )
    queue.put_policy(QueueProjectPolicyRevision(PROJECT, 1, 1, None), 0)
    reference = _prepare_admitted(queue, lineage_id, "gh:corrupt-admission")
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER queue_items_state_transition")
        connection.execute(
            schema_module.queue_items.update()
            .where(schema_module.queue_items.c.item_id == reference.item_id.value)
            .values(workflow_lineage_id=other_lineage_id.value)
        )

    reservation = queue.reserve_launch(
        QueueLaunchBinding(
            reference.item_id,
            QueueProjectionRevision(1),
            RunId("corrupt-admission-reservation"),
            revision_hash,
        )
    )
    assert isinstance(reservation, DurableStateCorrupt)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(queue_launch_bindings)
            )
            == 0
        )
    assert isinstance(queue.list_items(None, 50), DurableStateCorrupt)
    with _queue_api(queue) as api:
        response = api.get(QUEUE_ITEMS_PATH)
    assert response.status_code == 500
    assert response.json()["type"].endswith("durable-state-corrupt")
    with pytest.raises(QueueAdvanceCorrupt, match="queue is corrupt"):
        advance_queue_module.advance_queue(
            queue,
            DbosCatalogStore(engine),
            cast(DurablePublishedRunStarter, object()),
        )
