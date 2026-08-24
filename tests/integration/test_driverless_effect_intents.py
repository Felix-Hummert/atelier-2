"""Serve-start convergence for effect intents whose durable workflow raised.

A raised adapter exception -- a GitHub 500, a timeout -- ends `durable_effect`
or `durable_reconciliation` in a terminal DBOS error status, and recovery
replays only pending work, never a raised ending. Before this sweep the intent
stood PREPARED or RECONCILING forever, the operator door refused it, and the
run said STARTED until the store died (#628). This file prepares those exact
rows, models the raised ending as the terminal `workflow_status` row DBOS
leaves behind, and asks the sweep to route only what no live workflow owes:
to WAITING_RECONCILIATION, onto the attention feed, and through the operator
door -- never to an invented absence (ADR 0010).
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
from atelier2.adapters.dbos.workflow_ids import (
    effect_workflow_id_for,
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
from atelier2.contracts.executions import RunEventKind
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


def converge(engine: Engine) -> tuple[LogicalEffectKey, ...]:
    return converge_driverless_effect_intents(engine)


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
    assert converge(runtime.engine) == (intent.binding.logical_key,)


@pytest.mark.parametrize(
    "status", ["ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED", "CANCELLED"]
)
def test_prepared_intent_whose_effect_workflow_raised_reaches_the_operator_door(
    prepared: tuple[DbosRuntime, EffectIntent], status: str
) -> None:
    runtime, intent = prepared
    end_workflow(
        runtime.engine, effect_workflow_id_for(intent.binding.logical_key), status
    )

    assert converge(runtime.engine) == (intent.binding.logical_key,)

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


@pytest.mark.parametrize(
    "leave_driver",
    [
        pytest.param(lambda engine, workflow_id: None, id="workflow-still-enqueued"),
        pytest.param(
            lambda engine, workflow_id: _drop_workflow(engine, workflow_id),
            id="workflow-row-absent-recovery-owes-the-enqueue",
        ),
    ],
)
def test_prepared_intent_still_owed_a_driver_is_left_alone(
    prepared: tuple[DbosRuntime, EffectIntent],
    leave_driver: Callable[[Engine, str], None],
) -> None:
    runtime, intent = prepared
    leave_driver(runtime.engine, effect_workflow_id_for(intent.binding.logical_key))

    assert converge(runtime.engine) == ()

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


def test_reconciling_intent_whose_reconcile_workflow_raised_reopens_the_door(
    prepared: tuple[DbosRuntime, EffectIntent],
) -> None:
    runtime, intent = prepared
    route_to_waiting(runtime, intent)
    dead = command(intent)
    submit_reconcile_command(runtime.engine, runtime.settings, dead)
    end_workflow(runtime.engine, reconcile_workflow_id_for(dead.command_id))

    assert converge(runtime.engine) == (intent.binding.logical_key,)

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

    assert converge(runtime.engine) == ()

    assert intent_row(runtime.engine) == (
        EffectIntentState.RECONCILING.value,
        2,
        pending.command_id.value,
    )
