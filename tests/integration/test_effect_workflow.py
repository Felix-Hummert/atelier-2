from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.effect_store import (
    commit_resolution,
    decode_found,
    encode_found,
)
from atelier2.adapters.dbos.run_transitions import RunTransitionConflict
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    reconcile_commands,
    run_events,
    runs,
)
from atelier2.adapters.dbos.workflow_ids import (
    effect_workflow_id_for,
    reconcile_workflow_id_for,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.effects import (
    AdapterRevision,
    ConfirmationSource,
    EffectAdapterBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectReadback,
    EffectReceiptReference,
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
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.effects import EffectAdapter
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.effect_requests import open_pull_request_request_for_output
from tests.scenarios.run_waiting import (
    wait_for_run_state,
    wait_for_workflow_completion,
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
    V3_EFFECT_LINE_AGENT_JOB,
    V3_EFFECT_LINE_AGENT_NODE_ID,
    V3_EFFECT_LINE_DOCUMENT,
)

RUN = RunId("run-1")
PROVIDER_OUTPUT = b'"exact-request"'
ACTION_REQUEST = open_pull_request_request_for_output(PROVIDER_OUTPUT)


class UnknownReadbackAdapter:
    def __init__(self, owner: UnknownReadbackFactory, delegate: EffectAdapter) -> None:
        self._owner = owner
        self._delegate = delegate

    def readback(self, intent: EffectIntent) -> EffectReadback:
        if self._owner.unknown:
            return EffectUnknownOutcome(intent.reference)
        return self._delegate.readback(intent)

    def execute(self, intent: EffectIntent) -> PerformedEffect | EffectUnknownOutcome:
        if self._owner.execute_unknown:
            return EffectUnknownOutcome(intent.reference)
        return self._delegate.execute(intent)

    def close(self) -> None:
        self._delegate.close()


class UnknownReadbackFactory:
    """Test-only transient provider mode; it is deliberately not product config."""

    def __init__(self, delegate: LoopbackEffectAdapterFactory) -> None:
        self._delegate = delegate
        self.unknown = True
        self.execute_unknown = False
        self.opened: UnknownReadbackAdapter | None = None

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    @property
    def proves_absence(self) -> bool:
        return self._delegate.proves_absence

    def open(self) -> UnknownReadbackAdapter:
        self.opened = UnknownReadbackAdapter(self, self._delegate.open())
        return self.opened


@pytest.fixture
def prepared(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, EffectIntent, Path]]:
    external = tmp_path / "external.sqlite"
    runtime = recording_exact_runtime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "executor-A",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            external,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        PROVIDER_OUTPUT,
    )
    runtime.initialize_storage()
    revision = WorkflowRevision(V3_EFFECT_LINE_DOCUMENT)
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    start_published_v3_run(
        runtime.engine,
        runtime.settings,
        RUN,
        revision,
        runtime.agent_executor_registry,
    )
    complete_v3_agent_node(
        runtime,
        RUN,
        V3_EFFECT_LINE_AGENT_NODE_ID,
        V3_EFFECT_LINE_AGENT_JOB,
        PROVIDER_OUTPUT,
    )
    intent = prepare_and_launch_graph_action(
        runtime.engine,
        runtime.settings,
        RUN,
        revision.revision_hash,
        runtime.effect_adapter_binding,
    )
    try:
        yield runtime, intent, external
    finally:
        runtime.close()


def prepare_with_factory(
    tmp_path: Path, factory: UnknownReadbackFactory
) -> tuple[DbosRuntime, EffectIntent, Path]:
    external = tmp_path / "external.sqlite"
    runtime = recording_exact_runtime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "executor-A",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        factory,
        PROVIDER_OUTPUT,
    )
    runtime.initialize_storage()
    revision = WorkflowRevision(V3_EFFECT_LINE_DOCUMENT)
    publish_pinned_revisions(runtime.engine, ANY_JSON_SCHEMA, OPEN_PR_OPERATION)
    start_published_v3_run(
        runtime.engine,
        runtime.settings,
        RUN,
        revision,
        runtime.agent_executor_registry,
    )
    complete_v3_agent_node(
        runtime,
        RUN,
        V3_EFFECT_LINE_AGENT_NODE_ID,
        V3_EFFECT_LINE_AGENT_JOB,
        PROVIDER_OUTPUT,
    )
    intent = prepare_and_launch_graph_action(
        runtime.engine,
        runtime.settings,
        RUN,
        revision.revision_hash,
        factory.binding,
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


def test_unknown_commits_waiting_state_and_required_event_together(
    prepared: tuple[DbosRuntime, EffectIntent, Path],
) -> None:
    runtime, intent, _external = prepared
    with runtime.engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                CREATE TRIGGER fail_required_event
                BEFORE INSERT ON run_events
                WHEN NEW.event_kind = 'ACTION_RECONCILIATION_REQUIRED'
                BEGIN
                  SELECT RAISE(ABORT, 'injected required-event failure');
                END
                """
            )
        )

    with (
        pytest.raises(RunTransitionConflict, match="injected required-event failure"),
        Session(runtime.engine) as session,
        session.begin(),
    ):
        commit_resolution(
            session,
            intent.binding.logical_key.value,
            intent.binding.workflow_revision_hash.value,
            {"outcome": "UNKNOWN"},
        )

    with runtime.engine.connect() as connection:
        assert connection.execute(
            sa.select(runs.c.state, effect_intents.c.state).join(
                effect_intents, effect_intents.c.run_id == runs.c.run_id
            )
        ).one() == (RunState.STARTED.value, EffectIntentState.PREPARED.value)
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(run_events)) == 1
        )

    with runtime.engine.begin() as connection:
        connection.execute(sa.text("DROP TRIGGER fail_required_event"))
    with Session(runtime.engine) as session, session.begin():
        assert (
            commit_resolution(
                session,
                intent.binding.logical_key.value,
                intent.binding.workflow_revision_hash.value,
                {"outcome": "UNKNOWN"},
            )
            is RunState.WAITING_RECONCILIATION
        )

    with runtime.engine.connect() as connection:
        assert connection.execute(
            sa.select(runs.c.state, effect_intents.c.state).join(
                effect_intents, effect_intents.c.run_id == runs.c.run_id
            )
        ).one() == (
            RunState.WAITING_RECONCILIATION.value,
            EffectIntentState.WAITING_RECONCILIATION.value,
        )
        assert connection.execute(
            sa.select(run_events.c.event_kind, run_events.c.payload).order_by(
                run_events.c.event_sequence
            )
        ).all() == [
            ("AGENT_COMPLETED", PROVIDER_OUTPUT),
            ("ACTION_RECONCILIATION_REQUIRED", ACTION_REQUEST),
        ]


@pytest.mark.parametrize(
    "missing_field",
    (
        "fork_source_logical_key",
        "fork_source_run_id",
        "fork_source_workflow_revision_hash",
        "fork_source_result_hash",
    ),
)
def test_encoded_fork_resolution_refuses_a_partial_source_identity(
    prepared: tuple[DbosRuntime, EffectIntent, Path], missing_field: str
) -> None:
    _runtime, intent, _external = prepared
    source_result_hash = Sha256Hash.of(b"source-result")
    encoded = encode_found(
        PerformedEffect(EffectId("source-effect"), EffectResult(b"source-result")),
        ConfirmationSource.FORK_REFERENCE,
        source_receipt=EffectReceiptReference(
            LogicalEffectKey("source-logical-key"),
            RunId("source-run"),
            WorkflowRevisionHash(intent.binding.workflow_revision_hash.value),
            source_result_hash,
        ),
    )
    encoded[missing_field] = None

    with pytest.raises(ValueError, match="fork receipt source identity is incomplete"):
        decode_found(intent, encoded)


def test_authoritative_absence_executes_once_and_atomically_confirms_the_run(
    prepared: tuple[DbosRuntime, EffectIntent, Path],
) -> None:
    runtime, intent, external = prepared

    runtime.launch()

    wait_for_workflow_completion(
        effect_workflow_id_for(intent.binding.logical_key),
        "the effect workflow after authoritative absence",
    )
    wait_for_run_state(runtime.engine, RUN, RunState.WAITING_INPUT)
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
            RunState.WAITING_INPUT.value,
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

    wait_for_workflow_completion(
        effect_workflow_id_for(intent.binding.logical_key),
        "the effect workflow after adapter readback",
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
        wait_for_workflow_completion(
            effect_workflow_id_for(intent.binding.logical_key),
            "the effect workflow before operator reconciliation",
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
        submit_reconcile_command(runtime.engine, runtime.settings, submitted)
        wait_for_workflow_completion(
            reconcile_workflow_id_for(submitted.command_id),
            "the operator reconciliation workflow",
        )
        wait_for_run_state(runtime.engine, RUN, RunState.WAITING_INPUT)

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
                RunState.WAITING_INPUT.value,
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
        wait_for_workflow_completion(
            effect_workflow_id_for(intent.binding.logical_key),
            "the effect workflow before authoritative reconciliation",
        )
        assert factory.opened is not None
        if later_outcome == "found":
            factory.opened.execute(intent)
        factory.unknown = False
        submitted = reconcile_command(intent, OperatorAuthoritativeAbsence())
        submit_reconcile_command(runtime.engine, runtime.settings, submitted)

        wait_for_workflow_completion(
            reconcile_workflow_id_for(submitted.command_id),
            "the authoritative reconciliation workflow",
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


def test_authorized_execution_unknown_reopens_the_reconciliation_door(
    tmp_path: Path,
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
        wait_for_workflow_completion(
            effect_workflow_id_for(intent.binding.logical_key),
            "the effect workflow before reopening reconciliation",
        )
        factory.execute_unknown = True
        submitted = reconcile_command(intent, OperatorAuthoritativeAbsence())
        submit_reconcile_command(runtime.engine, runtime.settings, submitted)

        wait_for_workflow_completion(
            reconcile_workflow_id_for(submitted.command_id),
            "the reconciliation workflow that reopens reconciliation",
        )
        with runtime.engine.connect() as connection:
            assert connection.execute(
                sa.select(
                    runs.c.state,
                    effect_intents.c.state,
                    effect_intents.c.state_version,
                    effect_intents.c.reconciliation_owner_command_id,
                    reconcile_commands.c.state,
                )
                .join(effect_intents, effect_intents.c.run_id == runs.c.run_id)
                .join(
                    reconcile_commands,
                    reconcile_commands.c.logical_key == effect_intents.c.logical_key,
                )
            ).one() == (
                RunState.WAITING_RECONCILIATION.value,
                EffectIntentState.WAITING_RECONCILIATION.value,
                1,
                None,
                ReconcileCommandState.REJECTED_CONFLICT.value,
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
    finally:
        runtime.close()
