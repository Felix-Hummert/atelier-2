from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.advancer import (
    DbosDurableRunAdvancer,
    effect_workflow_id_for,
)
from atelier2.adapters.dbos.reconciler import (
    DbosEffectReconcileCommander,
    reconcile_workflow_id_for,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents, effect_receipts, runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    bootstrap_workflow_id_for,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.advance_run import advance_run
from atelier2.application.reconcile_effect import reconcile_effect
from atelier2.application.start_run import start_run
from atelier2.contracts.effects import (
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectAdapterBinding,
    EffectBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectReadback,
    EffectResult,
    EffectUnknownOutcome,
    LogicalEffectKey,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandState,
)
from atelier2.contracts.runs import RunId, RunState, StartRunRequest, WorkflowRevision
from atelier2.ports.effects import EffectAdapter

TIMEOUT_SECONDS = 5.0


class UnknownReadbackAdapter:
    def __init__(self, owner: UnknownReadbackFactory, delegate: EffectAdapter) -> None:
        self._owner = owner
        self._delegate = delegate

    def readback(self, intent: EffectIntent) -> EffectReadback:
        if self._owner.unknown:
            return EffectUnknownOutcome(intent.reference)
        return self._delegate.readback(intent)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        return self._delegate.execute(intent)

    def close(self) -> None:
        self._delegate.close()


class UnknownReadbackFactory:
    """Test-only transient provider mode; it is deliberately not product config."""

    def __init__(self, delegate: LoopbackEffectAdapterFactory) -> None:
        self._delegate = delegate
        self.unknown = True
        self.opened: UnknownReadbackAdapter | None = None

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    def open(self) -> UnknownReadbackAdapter:
        self.opened = UnknownReadbackAdapter(self, self._delegate.open())
        return self.opened


@pytest.fixture
def prepared(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, EffectIntent, Path]]:
    external = tmp_path / "external.sqlite"
    runtime = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "executor-A"),
        LoopbackEffectAdapterFactory(
            external,
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
            runtime.effect_adapter_binding.adapter_revision,
            runtime.effect_adapter_binding.destination,
            runtime.effect_adapter_binding.operational_identity,
        ),
        CanonicalRequest(b"exact-request"),
    )
    advance_run(
        intent,
        DbosDurableRunAdvancer(
            runtime.engine, runtime.settings, runtime.effect_adapter_binding
        ),
    )
    try:
        yield runtime, intent, external
    finally:
        runtime.close()


def wait_for_workflow(runtime: DbosRuntime, workflow_id: str) -> str:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    status = "PENDING"
    while status != "SUCCESS" and time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            status = str(
                connection.scalar(
                    sa.text(
                        "SELECT status FROM workflow_status WHERE workflow_uuid=:id"
                    ),
                    {"id": workflow_id},
                )
            )
        if status != "SUCCESS":
            time.sleep(0.025)
    return status


def prepare_with_factory(
    tmp_path: Path, factory: UnknownReadbackFactory
) -> tuple[DbosRuntime, EffectIntent, Path]:
    external = tmp_path / "external.sqlite"
    runtime = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "executor-A"), factory
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
            factory.binding.adapter_revision,
            factory.binding.destination,
            factory.binding.operational_identity,
        ),
        CanonicalRequest(b"exact-request"),
    )
    advance_run(
        intent,
        DbosDurableRunAdvancer(runtime.engine, runtime.settings, factory.binding),
    )
    return runtime, intent, external


def reconcile_command(
    intent: EffectIntent,
    determination: OperatorAuthoritativeAbsence | OperatorFoundEffect,
) -> ReconcileCommand:
    return ReconcileCommand(
        ReconcileCommandId("command-1"),
        intent.reference,
        expected_intent_state_version=EffectIntentStateVersion(1),
        actor=ReconcileActor("operator"),
        evidence="inspected the exact external destination",
        determination=determination,
    )


def test_authoritative_absence_executes_once_and_atomically_confirms_the_run(
    prepared: tuple[DbosRuntime, EffectIntent, Path],
) -> None:
    runtime, intent, external = prepared

    runtime.launch()

    assert (
        wait_for_workflow(runtime, effect_workflow_id_for(intent.binding.logical_key))
        == "SUCCESS"
    )
    assert (
        wait_for_workflow(runtime, bootstrap_workflow_id_for(intent.binding.run_id))
        == "SUCCESS"
    )
    with runtime.engine.connect() as connection:
        assert connection.execute(
            sa.select(
                runs.c.state,
                effect_intents.c.state,
                effect_intents.c.state_version,
                effect_receipts.c.confirmation_source,
                effect_receipts.c.canonical_request,
                effect_receipts.c.adapter_operational_identity,
            )
            .select_from(runs)
            .join(effect_intents, effect_intents.c.run_id == runs.c.run_id)
            .join(
                effect_receipts,
                effect_receipts.c.logical_key == effect_intents.c.logical_key,
            )
        ).one() == (
            RunState.COMPLETED.value,
            EffectIntentState.CONFIRMED.value,
            1,
            ConfirmationSource.ADAPTER_EXECUTION.value,
            intent.request.payload,
            intent.binding.adapter_operational_identity.value,
        )
    with sqlite3.connect(external) as connection:
        assert connection.execute(
            "SELECT logical_key, calls FROM loopback_effect_calls"
        ).fetchall() == [(intent.binding.logical_key.value, 1)]


def test_existing_effect_is_confirmed_by_adapter_readback_without_reexecution(
    prepared: tuple[DbosRuntime, EffectIntent, Path],
) -> None:
    runtime, intent, external = prepared
    runtime.effect_adapter.execute(intent)

    runtime.launch()

    assert (
        wait_for_workflow(runtime, effect_workflow_id_for(intent.binding.logical_key))
        == "SUCCESS"
    )
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                effect_receipts.select().with_only_columns(
                    effect_receipts.c.confirmation_source
                )
            )
            == ConfirmationSource.ADAPTER_READBACK.value
        )
    with sqlite3.connect(external) as connection:
        assert connection.execute(
            "SELECT calls FROM loopback_effect_calls"
        ).fetchall() == [(1,)]


@pytest.mark.parametrize("resolution", ["authorized-absence", "operator-found"])
def test_unknown_waits_without_an_effect_then_an_operator_command_finishes(
    tmp_path: Path, resolution: str
) -> None:
    external = tmp_path / "external.sqlite"
    factory = UnknownReadbackFactory(
        LoopbackEffectAdapterFactory(
            external,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        )
    )
    runtime, intent, _external = prepare_with_factory(tmp_path, factory)
    try:
        runtime.launch()
        assert (
            wait_for_workflow(
                runtime, effect_workflow_id_for(intent.binding.logical_key)
            )
            == "SUCCESS"
        )
        with runtime.engine.connect() as connection:
            assert connection.execute(
                sa.select(
                    runs.c.state, effect_intents.c.state, effect_intents.c.state_version
                ).join(effect_intents, effect_intents.c.run_id == runs.c.run_id)
            ).one() == (
                RunState.WAITING_RECONCILIATION.value,
                EffectIntentState.WAITING_RECONCILIATION.value,
                1,
            )
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(effect_receipts)
                )
                == 0
            )
        with sqlite3.connect(external) as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM loopback_effect_calls"
                ).fetchone()[0]
                == 0
            )

        determination = (
            OperatorAuthoritativeAbsence()
            if resolution == "authorized-absence"
            else OperatorFoundEffect(
                EffectId("operator-effect"), EffectResult(b"found")
            )
        )
        submitted = reconcile_command(intent, determination)
        reconcile_effect(
            submitted,
            DbosEffectReconcileCommander(runtime.engine, runtime.settings),
        )
        assert (
            wait_for_workflow(runtime, reconcile_workflow_id_for(submitted.command_id))
            == "SUCCESS"
        )

        expected_source = (
            ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION
            if resolution == "authorized-absence"
            else ConfirmationSource.OPERATOR_FOUND
        )
        with runtime.engine.connect() as connection:
            assert connection.execute(
                sa.select(
                    runs.c.state,
                    effect_intents.c.state,
                    effect_intents.c.state_version,
                    effect_receipts.c.confirmation_source,
                    effect_receipts.c.reconcile_command_id,
                )
                .join(effect_intents, effect_intents.c.run_id == runs.c.run_id)
                .join(
                    effect_receipts,
                    effect_receipts.c.logical_key == effect_intents.c.logical_key,
                )
            ).one() == (
                RunState.COMPLETED.value,
                EffectIntentState.CONFIRMED.value,
                3,
                expected_source.value,
                submitted.command_id.value,
            )
            assert (
                connection.scalar(sa.text("SELECT state FROM reconcile_commands"))
                == ReconcileCommandState.APPLIED.value
            )
        with sqlite3.connect(external) as connection:
            expected_calls = 1 if resolution == "authorized-absence" else 0
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM loopback_effect_calls"
                ).fetchone()[0]
                == expected_calls
            )
    finally:
        runtime.close()


@pytest.mark.parametrize("later_outcome", ["found", "not-found"])
def test_authorized_absence_keeps_operator_provenance_after_later_readback(
    tmp_path: Path, later_outcome: str
) -> None:
    external = tmp_path / "external.sqlite"
    factory = UnknownReadbackFactory(
        LoopbackEffectAdapterFactory(
            external,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        )
    )
    runtime, intent, _external = prepare_with_factory(tmp_path, factory)
    try:
        runtime.launch()
        assert (
            wait_for_workflow(
                runtime, effect_workflow_id_for(intent.binding.logical_key)
            )
            == "SUCCESS"
        )
        assert factory.opened is not None
        if later_outcome == "found":
            factory.opened.execute(intent)
        factory.unknown = False
        submitted = reconcile_command(intent, OperatorAuthoritativeAbsence())
        reconcile_effect(
            submitted,
            DbosEffectReconcileCommander(runtime.engine, runtime.settings),
        )

        assert (
            wait_for_workflow(runtime, reconcile_workflow_id_for(submitted.command_id))
            == "SUCCESS"
        )
        with runtime.engine.connect() as connection:
            assert (
                connection.scalar(sa.select(effect_receipts.c.confirmation_source))
                == ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION.value
            )
        with sqlite3.connect(external) as connection:
            assert connection.execute(
                "SELECT calls FROM loopback_effect_calls"
            ).fetchall() == [(1,)]
    finally:
        runtime.close()
