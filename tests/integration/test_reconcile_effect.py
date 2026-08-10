from __future__ import annotations

import hashlib
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa
from dbos import DBOSClient
from sqlalchemy import exc
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.advancer import DbosDurableRunAdvancer
from atelier2.adapters.dbos.effect_store import commit_resolution, encode_found
from atelier2.adapters.dbos.reconciler import (
    DbosEffectReconcileCommander,
    ReconcileCommandIdentityConflict,
    reconcile_workflow_id_for,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    reconcile_commands,
    runs,
)
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.advance_run import advance_run
from atelier2.application.reconcile_effect import reconcile_effect
from atelier2.application.start_run import start_run
from atelier2.contracts.effects import (
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectResult,
    LogicalEffectKey,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.contracts.runs import RunId, RunState, StartRunRequest, WorkflowRevision


@pytest.fixture
def waiting(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, DbosEffectReconcileCommander, EffectIntent]]:
    runtime = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "executor-A"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    runtime.initialize_storage()
    revision = WorkflowRevision(b"workflow-v1")
    start_run(
        StartRunRequest(RunId("run-1"), revision),
        DbosDurableRunStarter(runtime.engine, runtime.settings),
    )
    intent = EffectIntent(
        EffectBinding(
            LogicalEffectKey("run-1/action-1"),
            RunId("run-1"),
            revision.revision_hash,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
            runtime.effect_adapter_binding.operational_identity,
        ),
        CanonicalRequest(b"request"),
    )
    advance_run(
        intent,
        DbosDurableRunAdvancer(
            runtime.engine, runtime.settings, runtime.effect_adapter_binding
        ),
    )
    with runtime.engine.begin() as connection:
        connection.execute(
            effect_intents.update().values(
                state=EffectIntentState.WAITING_RECONCILIATION.value,
                state_version=1,
            )
        )
        connection.execute(
            runs.update().values(state=RunState.WAITING_RECONCILIATION.value)
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


def test_reconcile_workflow_id_is_deterministic_from_the_exact_command_id() -> None:
    command_id = ReconcileCommandId(" command-1 ")
    assert reconcile_workflow_id_for(command_id) == (
        "atelier2-reconcile-" + hashlib.sha256(command_id.value.encode()).hexdigest()
    )


def test_command_atomically_owns_the_waiting_intent_and_enqueues_reconciliation(
    waiting: tuple[DbosRuntime, DbosEffectReconcileCommander, EffectIntent],
) -> None:
    runtime, commander, intent = waiting
    submitted = command(intent)

    assert reconcile_effect(submitted, commander) == ReconcileCommandSnapshot(
        submitted, ReconcileCommandState.PENDING
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
    reconcile_effect(submitted, commander)
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

    assert reconcile_effect(submitted, commander) == ReconcileCommandSnapshot(
        submitted, ReconcileCommandState.APPLIED
    )


def test_command_id_reuse_with_changed_payload_refuses_without_mutation(
    waiting: tuple[DbosRuntime, DbosEffectReconcileCommander, EffectIntent],
) -> None:
    runtime, commander, intent = waiting
    submitted = command(intent)
    reconcile_effect(submitted, commander)

    with pytest.raises(ReconcileCommandIdentityConflict):
        reconcile_effect(replace(submitted, evidence="different evidence"), commander)

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

    def submit(value: ReconcileCommand) -> ReconcileCommandSnapshot:
        barrier.wait(timeout=5)
        return reconcile_effect(value, commander)

    with ThreadPoolExecutor(max_workers=2) as pool:
        snapshots = list(pool.map(submit, commands))

    assert {snapshot.state for snapshot in snapshots} == {
        ReconcileCommandState.PENDING,
        ReconcileCommandState.REJECTED_CONFLICT,
    }
    with runtime.engine.connect() as connection:
        winner = next(
            snapshot
            for snapshot in snapshots
            if snapshot.state is ReconcileCommandState.PENDING
        )
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


def test_final_reconciliation_rolls_back_receipt_intent_run_and_command_together(
    waiting: tuple[DbosRuntime, DbosEffectReconcileCommander, EffectIntent],
) -> None:
    runtime, commander, intent = waiting
    submitted = command(intent)
    reconcile_effect(submitted, commander)
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
                CREATE TRIGGER fail_command_apply
                BEFORE UPDATE OF state ON reconcile_commands
                WHEN NEW.state = 'APPLIED'
                BEGIN
                  SELECT RAISE(ABORT, 'injected final commit failure');
                END
                """
            )
        )

    with (
        pytest.raises(exc.IntegrityError),
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
