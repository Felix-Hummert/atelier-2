"""Serve-start convergence for effect intents no durable workflow will move.

A raised adapter exception -- a GitHub 500, a timeout -- ends `durable_effect`
or `durable_reconciliation` in a terminal DBOS error status, and recovery
replays only pending work, never a raised ending. Before this sweep the intent
stood PREPARED or RECONCILING forever, the operator door refused it, and the
run said STARTED until the store died (#628). An Action node workflow that ends
terminally between committing its intent and enqueuing the effect leaves the
same frozen intent under no effect-workflow row at all (#646). A workflow left
PENDING under a retired `application_version` is frozen the same way -- DBOS
will never resume it, so reading it as a live driver anyway leaves the run
frozen forever (#707). This file prepares those exact rows, models each ending
as the `workflow_status` row DBOS leaves behind, and asks the sweep to route
only what no live workflow owes: to WAITING_RECONCILIATION, onto the attention
feed, and through the operator door -- never to an invented absence (ADR 0010).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.effect_store import converge_driverless_effect_intents
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents, run_events, runs
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.uncontinuable_runs import LIVE_DRIVER_WORKFLOW_STATUSES
from atelier2.adapters.dbos.workflow_ids import (
    effect_workflow_id_for,
    node_workflow_id_for,
    reconcile_workflow_id_for,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.reconcile_effect import (
    ReconciliationAcceptedPending,
    ReconciliationExistingRejected,
    reconcile_effect_result,
)
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectResult,
    LogicalEffectKey,
    OperatorFoundEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.contracts.executions import NodeExecutionId, RunEventKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.run_events import AttentionEventPage
from tests.scenarios.agents import commit_configured_agent
from tests.scenarios.api import durable_queries
from tests.scenarios.runs import (
    prepare_and_launch_graph_action,
    start_published_v1_run,
    submit_reconcile_command,
)
from tests.scenarios.runtime import exact_output_runtime

WORKFLOW_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: waiting, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: waiting}
  - {id: agent, type: agent, job: job-17, output: request, next: action}
"""
ACTION_NODE_ID = "action"
"""The node of WORKFLOW_DOCUMENT the prepared intent belongs to."""


@pytest.fixture
def prepared(tmp_path: Path) -> Iterator[tuple[DbosRuntime, EffectIntent]]:
    runtime = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "executor-A"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    runtime.initialize_storage()
    revision = WorkflowRevision(WORKFLOW_DOCUMENT)
    start_published_v1_run(runtime.engine, runtime.settings, RunId("run-1"), revision)
    with canonical_write_transaction(runtime.engine) as connection:
        commit_configured_agent(
            connection, RunId("run-1"), revision.revision_hash, "agent"
        )
    intent = prepare_and_launch_graph_action(
        runtime.engine,
        runtime.settings,
        RunId("run-1"),
        revision.revision_hash,
        runtime.effect_adapter_binding,
    )
    try:
        yield runtime, intent
    finally:
        runtime.close()


def command(intent: EffectIntent, command_id: str = "command-1") -> ReconcileCommand:
    return ReconcileCommand(
        ReconcileCommandId(command_id),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator"),
        "inspected the exact destination and request",
        OperatorFoundEffect(EffectId("external-1"), EffectResult(b"result")),
    )


def end_workflow(engine: Engine, workflow_id: str, status: str = "ERROR") -> None:
    """Leave the row DBOS leaves when a workflow raises instead of crashing."""

    with engine.begin() as connection:
        ended = connection.execute(
            sa.text(
                "UPDATE workflow_status SET status=:status "
                "WHERE workflow_uuid=:workflow_id"
            ),
            {"status": status, "workflow_id": workflow_id},
        )
        assert ended.rowcount == 1


def strand_under_a_retired_application_version(
    engine: Engine, workflow_id: str, status: str
) -> None:
    """Leave the row DBOS leaves once a deploy retires the version driving it.

    `end_workflow` models a workflow that raised; this models the #707
    ending: still PENDING, ENQUEUED, or DELAYED -- DBOS would resume it -- but
    under an `application_version` this instance no longer runs, so DBOS
    itself never will.
    """

    with engine.begin() as connection:
        updated = connection.execute(
            sa.text(
                "UPDATE workflow_status SET status=:status, "
                "application_version=:application_version "
                "WHERE workflow_uuid=:workflow_id"
            ),
            {
                "status": status,
                "application_version": "dead-application-version",
                "workflow_id": workflow_id,
            },
        )
        assert updated.rowcount == 1


def leave_workflow(
    engine: Engine, workflow_id: str, status: str, application_version: str
) -> None:
    """Leave the row DBOS leaves for a workflow standing in that status.

    The Action node workflow the fixture's intent was prepared by never ran
    here -- the fixture calls that preparation step directly -- so its row is
    written rather than updated. A caller names `application_version`
    explicitly rather than relying on a default so a test that plants a
    workflow under a retired deploy (#707) reads as deliberate, not assumed.
    """

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO workflow_status "
                "(workflow_uuid,status,created_at,updated_at,priority,"
                "application_version) "
                "VALUES (:workflow_id,:status,0,0,0,:application_version)"
            ),
            {
                "workflow_id": workflow_id,
                "status": status,
                "application_version": application_version,
            },
        )


def action_node_workflow_id(intent: EffectIntent) -> str:
    return node_workflow_id_for(
        NodeExecutionId.for_node(
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            ACTION_NODE_ID,
        )
    )


def converge(engine: Engine, application_version: str) -> tuple[LogicalEffectKey, ...]:
    return converge_driverless_effect_intents(engine, application_version)


def intent_row(engine: Engine) -> tuple[object, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                sa.select(
                    effect_intents.c.state,
                    effect_intents.c.state_version,
                    effect_intents.c.reconciliation_owner_command_id,
                )
            ).one()
        )


def route_to_waiting(runtime: DbosRuntime, intent: EffectIntent) -> None:
    end_workflow(runtime.engine, effect_workflow_id_for(intent.binding.logical_key))
    assert converge(runtime.engine, runtime.settings.application_version) == (
        intent.binding.logical_key,
    )


def effect_workflow_ended(status: str) -> Callable[[DbosRuntime, EffectIntent], None]:
    def leave(runtime: DbosRuntime, intent: EffectIntent) -> None:
        end_workflow(
            runtime.engine, effect_workflow_id_for(intent.binding.logical_key), status
        )

    return leave


def node_workflow_ended_before_the_enqueue(
    runtime: DbosRuntime, intent: EffectIntent
) -> None:
    """The #646 window: the intent is committed, its enqueue never happened."""

    _drop_workflow(runtime.engine, effect_workflow_id_for(intent.binding.logical_key))
    leave_workflow(
        runtime.engine,
        action_node_workflow_id(intent),
        "ERROR",
        runtime.settings.application_version,
    )


def effect_workflow_pending_under_a_retired_application_version(
    status: str,
) -> Callable[[DbosRuntime, EffectIntent], None]:
    """#707, the direct driver: PREPARED reads `_workflow_is_dead` straight off
    the effect workflow (`effect_store.py`) whenever that row already exists."""

    def leave(runtime: DbosRuntime, intent: EffectIntent) -> None:
        strand_under_a_retired_application_version(
            runtime.engine, effect_workflow_id_for(intent.binding.logical_key), status
        )

    return leave


def node_workflow_pending_under_a_retired_application_version(
    status: str,
) -> Callable[[DbosRuntime, EffectIntent], None]:
    """#707, the #646 window's driver: no effect workflow was ever enqueued,
    so `_enqueueing_node_workflow_is_dead` reads the Action node workflow
    instead. Mirrors `test_converge_ends_a_silent_successor_whose_durable_
    workflow_is_the_dead_version` (#645, `test_interrupted_uncontinuable_
    inventory.py`): dropping the `application_version` match would read
    either workflow as a live driver and leave the run frozen forever.
    """

    def leave(runtime: DbosRuntime, intent: EffectIntent) -> None:
        _drop_workflow(
            runtime.engine, effect_workflow_id_for(intent.binding.logical_key)
        )
        leave_workflow(
            runtime.engine,
            action_node_workflow_id(intent),
            status,
            "dead-application-version",
        )

    return leave


@pytest.mark.parametrize(
    "leave_dead_driver",
    [
        pytest.param(effect_workflow_ended("ERROR"), id="effect-workflow-raised"),
        pytest.param(
            effect_workflow_ended("MAX_RECOVERY_ATTEMPTS_EXCEEDED"),
            id="effect-workflow-exhausted-recovery",
        ),
        pytest.param(
            effect_workflow_ended("CANCELLED"), id="effect-workflow-cancelled"
        ),
        pytest.param(
            node_workflow_ended_before_the_enqueue,
            id="node-workflow-ended-before-the-enqueue",
        ),
        *[
            pytest.param(
                effect_workflow_pending_under_a_retired_application_version(status),
                id=f"effect-workflow-{status.lower()}-under-a-retired-application-version",
            )
            for status in LIVE_DRIVER_WORKFLOW_STATUSES
        ],
        *[
            pytest.param(
                node_workflow_pending_under_a_retired_application_version(status),
                id=f"node-workflow-{status.lower()}-under-a-retired-application-version",
            )
            for status in LIVE_DRIVER_WORKFLOW_STATUSES
        ],
    ],
)
def test_prepared_intent_no_workflow_will_move_reaches_the_operator_door(
    prepared: tuple[DbosRuntime, EffectIntent],
    leave_dead_driver: Callable[[DbosRuntime, EffectIntent], None],
) -> None:
    runtime, intent = prepared
    leave_dead_driver(runtime, intent)

    assert converge(runtime.engine, runtime.settings.application_version) == (
        intent.binding.logical_key,
    )

    assert intent_row(runtime.engine) == (
        EffectIntentState.WAITING_RECONCILIATION.value,
        1,
        None,
    )
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(runs.c.state))
            == RunState.WAITING_RECONCILIATION.value
        )
        assert connection.execute(
            sa.select(run_events.c.node_id, run_events.c.payload).where(
                run_events.c.event_kind
                == RunEventKind.ACTION_RECONCILIATION_REQUIRED.value
            )
        ).one() == ("action", intent.request.payload)

    page = durable_queries(runtime.engine).read_attention_event_page(None, None, 10, ())
    assert isinstance(page, AttentionEventPage)
    assert [
        (item.event.event.run_id, item.event.event.event_kind) for item in page.events
    ] == [(RunId("run-1"), RunEventKind.ACTION_RECONCILIATION_REQUIRED)]

    accepted = reconcile_effect_result(
        command(intent), DbosEffectReconcileCommander(runtime.engine, runtime.settings)
    )
    assert isinstance(accepted, ReconciliationAcceptedPending)


def drop_effect_workflow(runtime: DbosRuntime, intent: EffectIntent) -> None:
    _drop_workflow(runtime.engine, effect_workflow_id_for(intent.binding.logical_key))


def node_workflow_still_owes_the_enqueue(
    runtime: DbosRuntime, intent: EffectIntent
) -> None:
    drop_effect_workflow(runtime, intent)
    leave_workflow(
        runtime.engine,
        action_node_workflow_id(intent),
        "PENDING",
        runtime.settings.application_version,
    )


@pytest.mark.parametrize(
    "leave_live_driver",
    [
        pytest.param(lambda runtime, intent: None, id="effect-workflow-still-enqueued"),
        pytest.param(
            drop_effect_workflow, id="no-workflow-row-recovery-owes-the-enqueue"
        ),
        pytest.param(
            node_workflow_still_owes_the_enqueue,
            id="node-workflow-still-owes-the-enqueue",
        ),
    ],
)
def test_prepared_intent_still_owed_a_driver_is_left_alone(
    prepared: tuple[DbosRuntime, EffectIntent],
    leave_live_driver: Callable[[DbosRuntime, EffectIntent], None],
) -> None:
    runtime, intent = prepared
    leave_live_driver(runtime, intent)

    assert converge(runtime.engine, runtime.settings.application_version) == ()

    assert intent_row(runtime.engine) == (EffectIntentState.PREPARED.value, 0, None)
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(runs.c.state)) == RunState.STARTED.value


def _drop_workflow(engine: Engine, workflow_id: str) -> None:
    with engine.begin() as connection:
        dropped = connection.execute(
            sa.text("DELETE FROM workflow_status WHERE workflow_uuid=:workflow_id"),
            {"workflow_id": workflow_id},
        )
        assert dropped.rowcount == 1


def reconcile_workflow_ended(status: str) -> Callable[[Engine, str], None]:
    def leave(engine: Engine, workflow_id: str) -> None:
        end_workflow(engine, workflow_id, status)

    return leave


def reconcile_workflow_pending_under_a_retired_application_version(
    status: str,
) -> Callable[[Engine, str], None]:
    """#707, the RECONCILING driver: `_intent_is_driverless` reads
    `_workflow_is_dead` straight off the reconcile workflow."""

    def leave(engine: Engine, workflow_id: str) -> None:
        strand_under_a_retired_application_version(engine, workflow_id, status)

    return leave


@pytest.mark.parametrize(
    "leave_dead_reconcile_workflow",
    [
        pytest.param(reconcile_workflow_ended("ERROR"), id="reconcile-workflow-raised"),
        *[
            pytest.param(
                reconcile_workflow_pending_under_a_retired_application_version(status),
                id=f"reconcile-workflow-{status.lower()}-under-a-retired-application-version",
            )
            for status in LIVE_DRIVER_WORKFLOW_STATUSES
        ],
    ],
)
def test_reconciling_intent_whose_reconcile_workflow_is_dead_reopens_the_door(
    prepared: tuple[DbosRuntime, EffectIntent],
    leave_dead_reconcile_workflow: Callable[[Engine, str], None],
) -> None:
    runtime, intent = prepared
    route_to_waiting(runtime, intent)
    dead = command(intent)
    submit_reconcile_command(runtime.engine, runtime.settings, dead)
    leave_dead_reconcile_workflow(
        runtime.engine, reconcile_workflow_id_for(dead.command_id)
    )

    assert converge(runtime.engine, runtime.settings.application_version) == (
        intent.binding.logical_key,
    )

    assert intent_row(runtime.engine) == (
        EffectIntentState.WAITING_RECONCILIATION.value,
        1,
        None,
    )
    commander = DbosEffectReconcileCommander(runtime.engine, runtime.settings)
    assert reconcile_effect_result(dead, commander) == ReconciliationExistingRejected(
        ReconcileCommandSnapshot(dead, ReconcileCommandState.REJECTED_CONFLICT)
    )
    fresh = reconcile_effect_result(command(intent, "command-2"), commander)
    assert isinstance(fresh, ReconciliationAcceptedPending)


def test_reconciling_intent_whose_reconcile_workflow_is_enqueued_is_left_alone(
    prepared: tuple[DbosRuntime, EffectIntent],
) -> None:
    runtime, intent = prepared
    route_to_waiting(runtime, intent)
    pending = command(intent)
    submit_reconcile_command(runtime.engine, runtime.settings, pending)

    assert converge(runtime.engine, runtime.settings.application_version) == ()

    assert intent_row(runtime.engine) == (
        EffectIntentState.RECONCILING.value,
        2,
        pending.command_id.value,
    )
