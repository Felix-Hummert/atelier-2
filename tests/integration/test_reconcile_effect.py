from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa
from dbos import DBOSClient
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.effect_store import commit_resolution, encode_found
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    commit_reconciliation_required,
)
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    reconcile_commands,
    run_events,
    runs,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow_ids import reconcile_workflow_id_for
from atelier2.application.reconcile_effect import (
    ReconcileRunResult,
    ReconciliationAcceptedPending,
    ReconciliationCommandConflict,
    ReconciliationExistingApplied,
    ReconciliationStale,
    reconcile_effect_result,
)
from atelier2.contracts.effects import (
    ConfirmationSource,
    EffectId,
    EffectIntent,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectResult,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.runs import (
    complete_v3_agent_node,
    prepare_and_launch_graph_action,
    publish_pinned_revisions,
    start_published_v3_run,
    submit_reconcile_command,
)
from tests.scenarios.runtime import recording_exact_runtime
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    OPEN_PR_OPERATION,
    V3_EFFECT_LINE_ACTION_NODE_ID,
    V3_EFFECT_LINE_AGENT_JOB,
    V3_EFFECT_LINE_AGENT_NODE_ID,
    V3_EFFECT_LINE_DOCUMENT,
)

PROVIDER_OUTPUT = b'"request"'


@pytest.fixture
def waiting(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, DbosEffectReconcileCommander, EffectIntent]]:
    runtime = recording_exact_runtime(
        canonical_runtime_settings(
            tmp_path, "executor-A", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        PROVIDER_OUTPUT,
    )
    runtime.initialize_storage()
    revision = WorkflowRevision(V3_EFFECT_LINE_DOCUMENT)
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    start_published_v3_run(
        runtime.engine,
        runtime.settings,
        RunId("run-1"),
        revision,
        runtime.agent_executor_registry,
    )
    complete_v3_agent_node(
        runtime,
        RunId("run-1"),
        V3_EFFECT_LINE_AGENT_NODE_ID,
        V3_EFFECT_LINE_AGENT_JOB,
        PROVIDER_OUTPUT,
    )
    intent = prepare_and_launch_graph_action(
        runtime.engine,
        runtime.settings,
        RunId("run-1"),
        revision.revision_hash,
        runtime.effect_adapter_binding,
    )
    with canonical_write_transaction(runtime.engine) as connection:
        updated = connection.execute(
            effect_intents.update().values(
                state=EffectIntentState.WAITING_RECONCILIATION.value,
                state_version=1,
            )
        )
        assert updated.rowcount == 1
        commit_reconciliation_required(
            connection,
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            V3_EFFECT_LINE_ACTION_NODE_ID,
            intent.request.payload,
        )
    try:
        yield (
            runtime,
            DbosEffectReconcileCommander(runtime.engine, runtime.settings),
            intent,
        )
    finally:
        runtime.close()


def command(
    intent: EffectIntent,
    command_id: str = "command-1",
    found: bool = True,
) -> ReconcileCommand:
    determination = (
        OperatorFoundEffect(EffectId("external-1"), EffectResult(b"result"))
        if found
        else OperatorAuthoritativeAbsence()
    )
    return ReconcileCommand(
        ReconcileCommandId(command_id),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator"),
        "inspected the exact destination and request",
        determination,
    )


def test_command_atomically_owns_the_waiting_intent_and_enqueues_reconciliation(
    waiting: tuple[DbosRuntime, DbosEffectReconcileCommander, EffectIntent],
) -> None:
    runtime, commander, intent = waiting
    submitted = command(intent)

    assert reconcile_effect_result(submitted, commander) == (
        ReconciliationAcceptedPending(
            ReconcileCommandSnapshot(submitted, ReconcileCommandState.PENDING)
        )
    )
    with runtime.engine.connect() as connection:
        assert connection.execute(
            sa.select(
                effect_intents.c.state,
                effect_intents.c.state_version,
                effect_intents.c.reconciliation_owner_command_id,
            )
        ).one() == (EffectIntentState.RECONCILING.value, 2, "command-1")
        assert connection.scalar(sa.select(reconcile_commands.c.state)) == "PENDING"
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                ),
                {"workflow_id": reconcile_workflow_id_for(submitted.command_id)},
            )
            == 1
        )


def test_exact_command_retry_returns_its_current_state_without_enqueuing_again(
    waiting: tuple[DbosRuntime, DbosEffectReconcileCommander, EffectIntent],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime, commander, intent = waiting
    submitted = command(intent)
    submit_reconcile_command(runtime.engine, runtime.settings, submitted)
    with runtime.engine.begin() as connection:
        connection.execute(
            reconcile_commands.update().values(
                state=ReconcileCommandState.APPLIED.value
            )
        )
    monkeypatch.setattr(
        DBOSClient,
        "enqueue_in_transaction",
        lambda *args, **kwargs: pytest.fail("retry enqueued another workflow"),
    )

    assert reconcile_effect_result(submitted, commander) == (
        ReconciliationExistingApplied(
            ReconcileCommandSnapshot(submitted, ReconcileCommandState.APPLIED)
        )
    )


def test_command_id_reuse_with_changed_payload_refuses_without_mutation(
    waiting: tuple[DbosRuntime, DbosEffectReconcileCommander, EffectIntent],
) -> None:
    runtime, commander, intent = waiting
    submitted = command(intent)
    submit_reconcile_command(runtime.engine, runtime.settings, submitted)

    assert (
        reconcile_effect_result(
            replace(submitted, evidence="different evidence"), commander
        )
        == ReconciliationCommandConflict()
    )

    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(reconcile_commands)
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status "
                    "WHERE workflow_uuid LIKE 'atelier2-reconcile-%'"
                )
            )
            == 1
        )


def test_concurrent_opposing_commands_commit_one_pending_and_one_rejected(
    waiting: tuple[DbosRuntime, DbosEffectReconcileCommander, EffectIntent],
) -> None:
    runtime, commander, intent = waiting
    commands = (command(intent, "found", True), command(intent, "absent", False))
    barrier = Barrier(2)

    def submit(value: ReconcileCommand) -> ReconcileRunResult:
        barrier.wait(timeout=5)
        return reconcile_effect_result(value, commander)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, commands))

    assert {type(result) for result in results} == {
        ReconciliationAcceptedPending,
        ReconciliationStale,
    }
    with runtime.engine.connect() as connection:
        (accepted,) = [
            result
            for result in results
            if isinstance(result, ReconciliationAcceptedPending)
        ]
        winner = accepted.snapshot
        assert winner.state is ReconcileCommandState.PENDING
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(reconcile_commands)
                .where(
                    reconcile_commands.c.state == ReconcileCommandState.PENDING.value
                )
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(reconcile_commands)
                .where(
                    reconcile_commands.c.state
                    == ReconcileCommandState.REJECTED_CONFLICT.value
                )
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.select(effect_intents.c.reconciliation_owner_command_id)
            )
            == winner.command.command_id.value
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status "
                    "WHERE workflow_uuid LIKE 'atelier2-reconcile-%'"
                )
            )
            == 1
        )


def test_reconcile_commits_exact_receipt_run_cursor_and_resolved_event_together(
    waiting: tuple[DbosRuntime, DbosEffectReconcileCommander, EffectIntent],
) -> None:
    runtime, _commander, intent = waiting
    submitted = command(intent)
    submit_reconcile_command(runtime.engine, runtime.settings, submitted)
    determination = submitted.determination
    assert isinstance(determination, OperatorFoundEffect)
    resolved = encode_found(
        PerformedEffect(determination.effect_id, determination.result),
        ConfirmationSource.OPERATOR_FOUND,
        submitted.command_id,
    )
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                CREATE TRIGGER fail_resolved_event
                BEFORE INSERT ON run_events
                WHEN NEW.event_kind = 'ACTION_RECONCILIATION_RESOLVED'
                BEGIN
                  SELECT RAISE(ABORT, 'injected resolved-event failure');
                END
                """
            )
        )

    with (
        pytest.raises(RunTransitionConflict, match="injected resolved-event failure"),
        Session(runtime.engine) as session,
        session.begin(),
    ):
        commit_resolution(
            session,
            intent.binding.logical_key.value,
            intent.binding.workflow_revision_hash.value,
            resolved,
            submitted.command_id,
        )

    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(effect_receipts))
            == 0
        )
        assert connection.execute(
            sa.select(
                effect_intents.c.state,
                effect_intents.c.state_version,
                runs.c.state,
                runs.c.state_version,
                runs.c.last_event_sequence,
                reconcile_commands.c.state,
            )
            .select_from(effect_intents)
            .join(runs, runs.c.run_id == effect_intents.c.run_id)
            .join(
                reconcile_commands,
                reconcile_commands.c.logical_key == effect_intents.c.logical_key,
            )
        ).one() == (
            EffectIntentState.RECONCILING.value,
            2,
            RunState.WAITING_RECONCILIATION.value,
            2,
            2,
            ReconcileCommandState.PENDING.value,
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status "
                    "WHERE workflow_uuid LIKE 'atelier2-reconcile-%'"
                )
            )
            == 1
        )

    with runtime.engine.begin() as connection:
        connection.execute(sa.text("DROP TRIGGER fail_resolved_event"))
    with Session(runtime.engine) as session, session.begin():
        assert (
            commit_resolution(
                session,
                intent.binding.logical_key.value,
                intent.binding.workflow_revision_hash.value,
                resolved,
                submitted.command_id,
            )
            is RunState.STARTED
        )

    with runtime.engine.connect() as connection:
        assert connection.execute(
            sa.select(
                effect_intents.c.state,
                effect_intents.c.state_version,
                runs.c.state,
                runs.c.state_version,
                runs.c.last_event_sequence,
                reconcile_commands.c.state,
                effect_receipts.c.result,
                effect_receipts.c.result_hash,
                run_events.c.event_sequence,
                run_events.c.event_kind,
                run_events.c.payload,
                run_events.c.payload_hash,
                run_events.c.receipt_logical_key,
                run_events.c.receipt_result_hash,
            )
            .select_from(effect_intents)
            .join(runs, runs.c.run_id == effect_intents.c.run_id)
            .join(
                reconcile_commands,
                reconcile_commands.c.logical_key == effect_intents.c.logical_key,
            )
            .join(
                effect_receipts,
                effect_receipts.c.logical_key == effect_intents.c.logical_key,
            )
            .join(
                run_events,
                sa.and_(
                    run_events.c.run_id == runs.c.run_id,
                    run_events.c.event_kind == "ACTION_RECONCILIATION_RESOLVED",
                ),
            )
        ).one() == (
            EffectIntentState.CONFIRMED.value,
            3,
            RunState.STARTED.value,
            3,
            3,
            ReconcileCommandState.APPLIED.value,
            b"result",
            determination.result.payload_hash.value,
            3,
            "ACTION_RECONCILIATION_RESOLVED",
            b"result",
            determination.result.payload_hash.value,
            intent.binding.logical_key.value,
            determination.result.payload_hash.value,
        )
