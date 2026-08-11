from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.continuation import action_continuation_workflow_id_for
from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import (
    DbosWaitAnswerer,
    RunTransitionConflict,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.answer_wait import answer_wait
from atelier2.application.reconcile_effect import reconcile_effect
from atelier2.application.start_run import start_run
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    EffectUnknownOutcome,
    OperatorAuthoritativeAbsence,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    WaitAnswerSnapshot,
    answer_workflow_id_for,
    logical_effect_key_for,
    node_workflow_id_for,
    subworkflow_workflow_id_for,
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, RunState, StartRunRequest, WorkflowRevision
from atelier2.ports.effects import EffectAdapter

UNORDERED_WORKFLOW = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: waiting, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: waiting}
  - {id: agent, type: agent, job: job-17, output: draft-17, next: action}
"""


class UnknownAdapter:
    def __init__(self, delegate: EffectAdapter, marker: Path) -> None:
        self._delegate = delegate
        self._marker = marker

    def readback(self, intent: EffectIntent) -> EffectReadback:
        if self._marker.exists():
            return EffectUnknownOutcome(intent.reference)
        return self._delegate.readback(intent)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        return self._delegate.execute(intent)

    def close(self) -> None:
        self._delegate.close()


class UnknownAdapterFactory:
    def __init__(self, root: Path, marker: Path) -> None:
        self._delegate = LoopbackEffectAdapterFactory(
            root / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        )
        self._marker = marker

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    def open(self) -> UnknownAdapter:
        return UnknownAdapter(self._delegate.open(), self._marker)


def runtime(root: Path, *, unknown_marker: Path | None = None) -> DbosRuntime:
    factory = (
        LoopbackEffectAdapterFactory(
            root / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        )
        if unknown_marker is None
        else UnknownAdapterFactory(root, unknown_marker)
    )
    return DbosRuntime(
        DbosRuntimeSettings(root / "atelier.sqlite", "executor-A"), factory
    )


def start_and_launch(lease: DbosRuntime, run_id: str = "run-1") -> WorkflowRevision:
    revision = WorkflowRevision(UNORDERED_WORKFLOW)
    lease.initialize_storage()
    start_run(
        StartRunRequest(RunId(run_id), revision),
        DbosDurableRunStarter(lease.engine, lease.settings),
    )
    lease.launch()
    return revision


def wait_for_state(lease: DbosRuntime, state: RunState) -> None:
    deadline = time.monotonic() + 8
    observed = ""
    while time.monotonic() < deadline:
        with lease.engine.connect() as connection:
            observed = str(connection.scalar(sa.text("SELECT state FROM runs")))
        if observed == state.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed!r}, expected {state.value!r}")


def events(lease: DbosRuntime) -> list[tuple[int, str, bytes]]:
    with lease.engine.connect() as connection:
        return [
            (int(row.event_sequence), str(row.event_kind), bytes(row.payload))
            for row in connection.execute(
                sa.text(
                    "SELECT event_sequence,event_kind,payload "
                    "FROM run_events ORDER BY event_sequence"
                )
            )
        ]


def all_durable_rows(
    root: Path,
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        return tuple(
            (
                table,
                tuple(
                    sorted(
                        connection.execute(f'SELECT * FROM "{table}"').fetchall(),
                        key=repr,
                    )
                ),
            )
            for table in tables
        )


def answer_counts(root: Path) -> tuple[int, int, int]:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        return (
            int(connection.execute("SELECT COUNT(*) FROM wait_answers").fetchone()[0]),
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM workflow_status "
                    "WHERE name='atelier2_wait_answer'"
                ).fetchone()[0]
            ),
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM run_events WHERE event_kind='WAIT_ANSWERED'"
                ).fetchone()[0]
            ),
        )


def workflow_identity_rows(root: Path, workflow_id: str) -> tuple[tuple[str, str], ...]:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        return tuple(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT workflow_uuid,name FROM workflow_status WHERE workflow_uuid=?",
                (workflow_id,),
            )
        )


def submit_answer(lease: DbosRuntime, revision: WorkflowRevision) -> None:
    answer_wait(
        SubmitWaitAnswerRequest(
            RunId("run-1"), revision.revision_hash, "waiting", b"5"
        ),
        DbosWaitAnswerer(lease.engine, lease.settings.application_version),
    )


def test_unordered_yaml_alone_drives_agent_action_wait_subworkflow(
    tmp_path: Path,
) -> None:
    lease = runtime(tmp_path)
    try:
        revision = start_and_launch(lease)
        wait_for_state(lease, RunState.WAITING_INPUT)
        submit_answer(lease, revision)
        wait_for_state(lease, RunState.COMPLETED)

        assert events(lease) == [
            (1, "AGENT_COMPLETED", b"draft-17"),
            (2, "ACTION_COMPLETED", b"draft-17"),
            (3, "WAITING_INPUT", b""),
            (4, "WAIT_ANSWERED", b"5"),
            (5, "SUBWORKFLOW_COMPLETED", b"5"),
        ]
        with lease.engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT COUNT(*) FROM effect_receipts")) == 1
            )
            hashes = tuple(
                Sha256Hash(str(value))
                for value in connection.execute(
                    sa.text("SELECT event_hash FROM run_events ORDER BY event_sequence")
                ).scalars()
            )
            assert connection.scalar(sa.text("SELECT terminal_hash FROM runs")) == (
                terminal_hash_for(revision.revision_hash, hashes).value
            )
    finally:
        lease.close()


def test_each_confirmed_node_starts_only_its_configured_successor(
    tmp_path: Path,
) -> None:
    lease = runtime(tmp_path)
    try:
        start_and_launch(lease)
        wait_for_state(lease, RunState.WAITING_INPUT)

        with lease.engine.connect() as connection:
            assert connection.execute(
                sa.text(
                    "SELECT current_node_id,state_version,last_event_sequence FROM runs"
                )
            ).one() == ("waiting", 3, 3)
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM workflow_status "
                        "WHERE name='atelier2_graph_node'"
                    )
                )
                == 3
            )
    finally:
        lease.close()


def test_wait_and_unknown_effect_expose_durable_reasons(tmp_path: Path) -> None:
    marker = tmp_path / "unknown"
    marker.touch()
    lease = runtime(tmp_path, unknown_marker=marker)
    try:
        start_and_launch(lease)
        wait_for_state(lease, RunState.WAITING_RECONCILIATION)

        assert events(lease) == [
            (1, "AGENT_COMPLETED", b"draft-17"),
            (2, "ACTION_RECONCILIATION_REQUIRED", b"draft-17"),
        ]
        with lease.engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM workflow_status WHERE name='atelier2_action_continuation'"
                    )
                )
                == 0
            )
    finally:
        lease.close()


def test_initial_and_reconciled_action_share_one_continuation(tmp_path: Path) -> None:
    initial_root = tmp_path / "initial"
    initial = runtime(initial_root)
    try:
        revision = start_and_launch(initial)
        wait_for_state(initial, RunState.WAITING_INPUT)
        initial_identity = action_path_identity(initial, revision)
        assert events(initial) == [
            (1, "AGENT_COMPLETED", b"draft-17"),
            (2, "ACTION_COMPLETED", b"draft-17"),
            (3, "WAITING_INPUT", b""),
        ]
    finally:
        initial.close()

    reconciled_root = tmp_path / "reconciled"
    reconciled_root.mkdir()
    marker = reconciled_root / "unknown"
    marker.touch()
    reconciled = runtime(reconciled_root, unknown_marker=marker)
    try:
        reconciled_revision = start_and_launch(reconciled)
        assert reconciled_revision == revision
        wait_for_state(reconciled, RunState.WAITING_RECONCILIATION)
        marker.unlink()
        with reconciled.engine.connect() as connection:
            snapshot = intent_snapshot_from_record(
                connection.execute(sa.select(effect_intents)).mappings().one()
            )
        command = ReconcileCommand(
            ReconcileCommandId("resolve-1"),
            snapshot.intent.reference,
            snapshot.state_version,
            ReconcileActor("operator"),
            "destination inspected",
            OperatorAuthoritativeAbsence(),
        )
        reconcile_effect(
            command,
            DbosEffectReconcileCommander(reconciled.engine, reconciled.settings),
        )
        wait_for_state(reconciled, RunState.WAITING_INPUT)
        reconciled_identity = action_path_identity(reconciled, reconciled_revision)
        assert events(reconciled) == [
            (1, "AGENT_COMPLETED", b"draft-17"),
            (2, "ACTION_RECONCILIATION_REQUIRED", b"draft-17"),
            (3, "ACTION_RECONCILIATION_RESOLVED", b"draft-17"),
            (4, "ACTION_COMPLETED", b"draft-17"),
            (5, "WAITING_INPUT", b""),
        ]
        assert reconciled_revision.revision_hash == (
            snapshot.intent.binding.workflow_revision_hash
        )
    finally:
        reconciled.close()

    assert initial_identity == reconciled_identity


def action_path_identity(
    lease: DbosRuntime, revision: WorkflowRevision
) -> tuple[str, str, tuple[str, str, str, str], str]:
    action_execution = NodeExecutionId.for_node(
        RunId("run-1"), revision.revision_hash, "action"
    )
    logical_key = logical_effect_key_for(action_execution)
    continuation_id = action_continuation_workflow_id_for(logical_key)
    wait_id = node_workflow_id_for(
        NodeExecutionId.for_node(RunId("run-1"), revision.revision_hash, "waiting")
    )
    expected_receipt = (
        logical_key.value,
        "run-1",
        revision.revision_hash.value,
        Sha256Hash.of(b"draft-17").value,
    )
    with lease.engine.connect() as connection:
        assert connection.execute(
            sa.text(
                "SELECT workflow_uuid FROM workflow_status "
                "WHERE name='atelier2_action_continuation'"
            )
        ).scalars().all() == [continuation_id]
        assert connection.execute(
            sa.text(
                "SELECT workflow_uuid FROM workflow_status "
                "WHERE name='atelier2_graph_node' AND workflow_uuid=:wait_id"
            ),
            {"wait_id": wait_id},
        ).scalars().all() == [wait_id]
        event_binding = connection.execute(
            sa.text(
                "SELECT node_execution_id,receipt_logical_key,receipt_result_hash "
                "FROM run_events WHERE event_kind='ACTION_COMPLETED'"
            )
        ).one()
        receipt_binding = connection.execute(
            sa.text(
                "SELECT logical_key,run_id,workflow_revision_hash,result_hash "
                "FROM effect_receipts"
            )
        ).one()
    assert tuple(receipt_binding) == expected_receipt
    assert tuple(event_binding) == (
        action_execution.value,
        expected_receipt[0],
        expected_receipt[3],
    )
    return continuation_id, action_execution.value, expected_receipt, wait_id


@pytest.mark.parametrize(
    "answers",
    [pytest.param((b"5", b"5"), id="same"), pytest.param((b"5", b"6"), id="different")],
)
def test_answer_submission_is_exact_and_concurrent(
    tmp_path: Path, answers: tuple[bytes, bytes]
) -> None:
    lease = runtime(tmp_path)
    try:
        revision = start_and_launch(lease)
        wait_for_state(lease, RunState.WAITING_INPUT)
        barrier = Barrier(2)

        def submit(
            answer_bytes: bytes,
        ) -> WaitAnswerSnapshot | RunTransitionConflict:
            barrier.wait(timeout=5)
            try:
                return answer_wait(
                    SubmitWaitAnswerRequest(
                        RunId("run-1"),
                        revision.revision_hash,
                        "waiting",
                        answer_bytes,
                    ),
                    DbosWaitAnswerer(lease.engine, lease.settings.application_version),
                )
            except RunTransitionConflict as error:
                return error

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, answers))

        snapshots = [
            result for result in results if isinstance(result, WaitAnswerSnapshot)
        ]
        conflicts = [
            result for result in results if isinstance(result, RunTransitionConflict)
        ]
        if answers[0] == answers[1]:
            assert len(snapshots) == 2
            assert len(conflicts) == 0
        else:
            assert len(snapshots) == 1
            assert len(conflicts) == 1
        accepted_answers = {snapshot.answer.answer_bytes for snapshot in snapshots}
        assert len(accepted_answers) == 1
        assert accepted_answers.issubset(set(answers))
        wait_for_state(lease, RunState.COMPLETED)
        with sqlite3.connect(tmp_path / "atelier.sqlite") as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM wait_answers"
            ).fetchone() == (1,)
            assert connection.execute(
                "SELECT COUNT(*) FROM workflow_status WHERE name='atelier2_wait_answer'"
            ).fetchone() == (1,)
    finally:
        lease.close()


@pytest.mark.parametrize(
    "answer_bytes",
    [b"", b"\xff", b" 5", b"5 ", b"+5", b"05", b"-0"],
    ids=[
        "empty",
        "invalid-utf8",
        "leading-space",
        "trailing-space",
        "plus",
        "leading-zero",
        "negative-zero",
    ],
)
def test_invalid_integer_answer_writes_no_product_or_dbos_state(
    tmp_path: Path, answer_bytes: bytes
) -> None:
    running = runtime(tmp_path)
    try:
        revision = start_and_launch(running)
        wait_for_state(running, RunState.WAITING_INPUT)
    finally:
        running.close()

    idle = runtime(tmp_path)
    try:
        before = all_durable_rows(tmp_path)
        with pytest.raises(ValueError, match="canonical base-10"):
            answer_wait(
                SubmitWaitAnswerRequest(
                    RunId("run-1"), revision.revision_hash, "waiting", answer_bytes
                ),
                DbosWaitAnswerer(idle.engine, idle.settings.application_version),
            )
        assert all_durable_rows(tmp_path) == before
    finally:
        idle.close()


def test_pending_and_applied_answer_retries_are_exact_without_reenqueue(
    tmp_path: Path,
) -> None:
    running = runtime(tmp_path)
    try:
        revision = start_and_launch(running)
        wait_for_state(running, RunState.WAITING_INPUT)
    finally:
        running.close()

    pending = runtime(tmp_path)
    try:
        wait_execution = NodeExecutionId.for_node(
            RunId("run-1"), revision.revision_hash, "waiting"
        )
        final_execution = NodeExecutionId.for_node(
            RunId("run-1"), revision.revision_hash, "final"
        )
        answer_workflow_id = answer_workflow_id_for(wait_execution)
        final_node_workflow_id = node_workflow_id_for(final_execution)
        subworkflow_id = subworkflow_workflow_id_for(final_execution)
        answerer = DbosWaitAnswerer(
            pending.engine, pending.settings.application_version
        )
        request = SubmitWaitAnswerRequest(
            RunId("run-1"), revision.revision_hash, "waiting", b"5"
        )
        first = answer_wait(request, answerer)
        assert first.state.value == "PENDING"
        assert answer_counts(tmp_path) == (1, 1, 0)
        assert workflow_identity_rows(tmp_path, answer_workflow_id) == (
            (answer_workflow_id, "atelier2_wait_answer"),
        )
        assert workflow_identity_rows(tmp_path, final_node_workflow_id) == ()
        assert workflow_identity_rows(tmp_path, subworkflow_id) == ()
        pending_rows = all_durable_rows(tmp_path)

        assert answer_wait(request, answerer) == first
        with pytest.raises(RunTransitionConflict):
            answer_wait(
                SubmitWaitAnswerRequest(
                    RunId("run-1"), revision.revision_hash, "waiting", b"6"
                ),
                answerer,
            )
        assert all_durable_rows(tmp_path) == pending_rows
        assert answer_counts(tmp_path) == (1, 1, 0)
    finally:
        pending.close()

    completing = runtime(tmp_path)
    try:
        completing.launch()
        wait_for_state(completing, RunState.COMPLETED)
    finally:
        completing.close()

    applied = runtime(tmp_path)
    try:
        answerer = DbosWaitAnswerer(
            applied.engine, applied.settings.application_version
        )
        applied_rows = all_durable_rows(tmp_path)
        retried = answer_wait(request, answerer)
        assert retried.state.value == "APPLIED"
        with pytest.raises(RunTransitionConflict):
            answer_wait(
                SubmitWaitAnswerRequest(
                    RunId("run-1"), revision.revision_hash, "waiting", b"6"
                ),
                answerer,
            )
        assert all_durable_rows(tmp_path) == applied_rows
        assert answer_counts(tmp_path) == (1, 1, 1)
        assert workflow_identity_rows(tmp_path, answer_workflow_id) == (
            (answer_workflow_id, "atelier2_wait_answer"),
        )
        assert workflow_identity_rows(tmp_path, final_node_workflow_id) == (
            (final_node_workflow_id, "atelier2_graph_node"),
        )
        assert workflow_identity_rows(tmp_path, subworkflow_id) == (
            (subworkflow_id, "atelier2_add_subworkflow"),
        )
    finally:
        applied.close()
