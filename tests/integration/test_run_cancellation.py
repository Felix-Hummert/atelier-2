"""#439 P2/P3: the durable run-cancel command, and the run it ends.

The command a route never gets to send yet (#439 P4) already has a full
answer at the store: `DbosAgentAttemptStore.request_run_cancellation` resolves
one operator idempotency key against the run's live attempt, recomputing the
node execution the operator's confirmation named rather than trusting it (D2).
The P2 heads pin the ordering the bauplan requires -- a known command answers
before any cancellability gate. The P3 heads pin what happens once the
attempt this command targets actually ends: the cleanup attestation that
closes it, on either carrier, also lifts the run terminal under the same
command identity and writes the one `cancelled` receipt that names it -- never
a second cancellation engine, and never for a command this store did not mint
as the operator's own (`is_operator_run_cancel`).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.run_transitions import (
    RunTransitionConflict,
    load_run,
    run_from_record_with_bindings,
)
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import node_receipts_v3, run_events, runs
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.dbos.workflow_ids import node_workflow_id_for
from atelier2.api.projection.runs import run_resource
from atelier2.api.wire.resources import RunResourceV3
from atelier2.application.cancel_run import (
    CancelAccepted,
    CancelTerminalRetry,
    cancel_run_result,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptReplacement,
    AgentAttemptState,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    WatchdogGenerationId,
)
from atelier2.contracts.agents import (
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    NodeExecutionId,
    RunEventKind,
)
from atelier2.contracts.node_records_v3 import PersistedReceiptDisposition
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_cancellations import (
    CancelRunRequest,
    is_operator_run_cancel,
)
from atelier2.contracts.runs import RunId, RunState
from atelier2.contracts.stored_node_receipt_reasons import (
    read_stored_node_receipt_reason,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    RunCancellationAccepted,
    RunCancellationCommandConflict,
    RunCancellationNotCancellable,
    RunCancellationRefusal,
    RunCancellationRunMissing,
    RunCancellationTerminalRetry,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.run_queries import RunFound
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.integration.test_v3_agent_start import publish as publish_v3_workflow
from tests.integration.test_v3_bounded_loop_run import RUN as LOOP_RUN
from tests.integration.test_v3_bounded_loop_run import (
    finish_gated_node,
    gate_execution,
    start_loop,
)
from tests.integration.test_v3_bounded_loop_run import (
    runtime as _loop_runtime,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_attempt_execution,
    agent_scratch_root,
    failing_agent_executor_factory,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.run_waiting import wait_for_workflow_completion
from tests.scenarios.workflows import LOOPED_LINE_MAXIMUM_ROUNDS

runtime = _loop_runtime

_LEGACY_OWNER = AgentProcessOwnerId("run-cancel-test-owner")
_LEGACY_GENERATION = WatchdogGenerationId("run-cancel-test-generation")


def _v3_prepared(
    root: Path, run_name: str
) -> tuple[DbosRuntime, DbosAgentAttemptStore, AgentAttemptExecution]:
    """A single-node V3 run with its `implement` attempt legacy-armed.

    `bind_watchdog` + `claim` reach `LAUNCH_ARMED` under a real owner
    generation -- every cleanup disposition but `NEVER_LAUNCHED` requires one
    (`AgentAttempt.__post_init__`), and cancellation itself never requires an
    armed attempt, so this shape serves every #439 P3 store head below.
    """
    root_runtime = DbosRuntime(
        canonical_runtime_settings(
            root, "run-cancel-v3-test", agent_scratch_root(root)
        ),
        canonical_loopback_effects(root),
        (failing_agent_executor_factory("exact", []),),
    )
    root_runtime.initialize_storage()
    workflow, bindings = publish_v3_workflow(root_runtime)
    run_id = RunId(run_name)
    started = DbosDurableRunStarter(
        root_runtime.engine,
        root_runtime.settings,
        root_runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated)
    with root_runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    assert isinstance(run, RunV3)
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, workflow.revision_hash, "implement"),
        run_id,
        workflow.revision_hash,
        "implement",
        run.agent_bindings[0],
        AgentExecutorOperationalIdentity("exact-operation"),
        b"Do the one thing this chain is for.",
    )
    execution = AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )
    store = DbosAgentAttemptStore(
        root_runtime.engine, root_runtime.settings.application_version
    )
    store.prepare(execution)
    store.bind_watchdog(execution, _LEGACY_OWNER, _LEGACY_GENERATION)
    store.claim(execution)
    return root_runtime, store, execution


def _node_receipt(
    engine: sa.Engine, node_execution_id: NodeExecutionId
) -> tuple[str, str] | None:
    with engine.connect() as connection:
        stored = (
            connection.execute(
                sa.select(
                    node_receipts_v3.c.disposition, node_receipts_v3.c.reason
                ).where(node_receipts_v3.c.node_execution_id == node_execution_id.value)
            )
            .mappings()
            .one_or_none()
        )
    if stored is None:
        return None
    reason, _schema, _value = read_stored_node_receipt_reason(str(stored["reason"]))
    return str(stored["disposition"]), reason


def _cancel_event_kinds(engine: sa.Engine) -> list[str]:
    with engine.connect() as connection:
        return [
            str(kind)
            for kind in connection.execute(
                sa.select(run_events.c.event_kind).order_by(run_events.c.event_sequence)
            ).scalars()
        ]


def test_duplicate_command_writes_exactly_one_requested_event(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "run-cancel/duplicate")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        store.prepare(execution)
        request = CancelRunRequest(
            execution.request.run_id,
            "operator-duplicate-1",
            execution.request.node_execution_id,
        )

        first = store.request_run_cancellation(request)
        second = store.request_run_cancellation(request)

        assert isinstance(first, RunCancellationAccepted)
        assert first.attempt.state is AgentAttemptState.CANCEL_REQUESTED
        assert second == first
        assert _cancel_event_kinds(runtime.engine) == [
            RunEventKind.AGENT_CANCEL_REQUESTED.value
        ]
    finally:
        runtime.close()


def test_a_foreign_command_on_an_already_busy_attempt_is_a_command_conflict(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "run-cancel/foreign-key")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        prepared = store.prepare(execution)
        already_busy = store.request_cancellation(
            CancelAgentAttemptRequest(
                execution.request.run_id,
                execution.attempt_id,
                "attempt-route-command",
                prepared.state_version,
                AgentAttemptReplacement.NONE,
            )
        )
        assert isinstance(already_busy, AgentAttemptCancellationAccepted)
        assert already_busy.attempt.state is AgentAttemptState.CANCEL_REQUESTED

        conflict = store.request_run_cancellation(
            CancelRunRequest(
                execution.request.run_id,
                "operator-foreign-key-1",
                execution.request.node_execution_id,
            )
        )

        assert isinstance(conflict, RunCancellationCommandConflict)
        assert _cancel_event_kinds(runtime.engine) == [
            RunEventKind.AGENT_CANCEL_REQUESTED.value
        ]
    finally:
        runtime.close()


def test_a_stale_node_execution_id_is_refused_between_nodes(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "run-cancel/stale-fence")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        store.prepare(execution)
        stale_fence = NodeExecutionId.for_node(
            execution.request.run_id,
            execution.request.workflow_revision_hash,
            "done",
        )

        result = store.request_run_cancellation(
            CancelRunRequest(execution.request.run_id, "operator-stale-1", stale_fence)
        )

        assert result == RunCancellationNotCancellable(
            RunCancellationRefusal.BETWEEN_NODES
        )
        assert _cancel_event_kinds(runtime.engine) == []
    finally:
        runtime.close()


def test_a_run_that_never_existed_is_named_missing(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )

        result = store.request_run_cancellation(
            CancelRunRequest(
                RunId("run-cancel/never-existed"),
                "operator-missing-1",
                NodeExecutionId("a" * 64),
            )
        )

        assert result == RunCancellationRunMissing()
    finally:
        runtime.close()


def test_a_stale_round_fence_is_refused_without_stopping_the_live_round(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """D2's fence binds the round: a command minted before a loop jump is stale.

    The confirmation the operator read in round one names round one's
    `implement`. After the loop turns to round two, that exact fence is no
    longer the run's live execution -- refused as `BETWEEN_NODES`, not
    accepted against the wrong round's attempt. Releasing the gate afterward
    and watching the loop run to its declared end is the proof that the
    refused command never touched round two's attempt at all.
    """
    started_runtime, recording = runtime
    entered, release, command = gate_execution(LOOP_RUN, "implement", 2)
    assert recording.opened is not None
    recording.opened.command = command
    workflow = start_loop(started_runtime)
    started_runtime.launch()
    assert entered.wait(timeout=10), "the loop never entered round two"

    store = DbosAgentAttemptStore(
        started_runtime.engine, started_runtime.settings.application_version
    )
    stale_round_fence = NodeExecutionId.for_node(
        LOOP_RUN, workflow.revision_hash, "implement", 1
    )
    result = store.request_run_cancellation(
        CancelRunRequest(LOOP_RUN, "operator-round-fence-1", stale_round_fence)
    )
    finish_gated_node(LOOP_RUN, workflow, "implement", 2, release)

    assert result == RunCancellationNotCancellable(RunCancellationRefusal.BETWEEN_NODES)
    assert (
        wait_for_workflow_completion(
            node_workflow_id_for(
                NodeExecutionId.for_node(
                    LOOP_RUN,
                    workflow.revision_hash,
                    "review",
                    LOOPED_LINE_MAXIMUM_ROUNDS,
                )
            ),
            "the loop's final review node to complete",
        )
        == RunState.COMPLETED.value
    )


def test_terminal_retry_is_canonical_and_writes_no_new_event(tmp_path: Path) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "run-cancel/terminal-retry")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        store.prepare(execution)
        request = CancelRunRequest(
            execution.request.run_id,
            "operator-terminal-retry-1",
            execution.request.node_execution_id,
        )
        accepted = store.request_run_cancellation(request)
        assert isinstance(accepted, RunCancellationAccepted)
        cancellation = accepted.attempt.cancellation
        assert cancellation is not None
        cleanup_request = CancelAgentAttemptRequest(
            execution.request.run_id,
            accepted.attempt.attempt_id,
            cancellation.command_id,
            cancellation.expected_attempt_state_version,
            cancellation.replacement,
        )
        terminal = store.attest_cancellation_cleanup(
            cleanup_request,
            AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
            None,
            None,
        )
        assert terminal.attempt.state is AgentAttemptState.CANCELLED

        retry = store.request_run_cancellation(request)

        with runtime.engine.connect() as connection:
            canonical_run = load_run(connection, execution.request.run_id)
        assert retry == RunCancellationTerminalRetry(canonical_run)
        # #439 P3: the attestation that ended the attempt already lifted the
        # run, in the same transaction -- the retry names that same ending.
        assert canonical_run.state is RunState.CANCELLED
        assert _cancel_event_kinds(runtime.engine) == [
            RunEventKind.AGENT_CANCEL_REQUESTED.value,
            RunEventKind.AGENT_CANCELLED.value,
        ]
    finally:
        runtime.close()


@pytest.mark.proves("a-v3-run-is-cancelled-once")
def test_a_v3_run_is_cancelled_once_through_the_route_application_path(
    tmp_path: Path,
) -> None:
    """#439 P4: the run the operator cancel route ends, ended exactly once.

    Driven through `cancel_run_result` -- the exact application call the HTTP
    route wires, with the real store -- so this proves the durable slice the
    route delivers rather than the thin shell. The route hands only the
    operator's opaque idempotency key; the durable command id is minted inside
    the reserved namespace, the attempt's cleanup lifts the run to `CANCELLED`,
    and a retry of the same key resolves to the same command and the same
    canonical run without minting a second cancel or writing a second event.
    """
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(attempt_request(runtime, "run-cancel/once"))
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        store.prepare(execution)

        accepted = cancel_run_result(
            execution.request.run_id,
            "operator-once-1",
            execution.request.node_execution_id,
            store,
        )
        assert isinstance(accepted, CancelAccepted)
        cancellation = accepted.attempt.cancellation
        assert cancellation is not None
        assert is_operator_run_cancel(cancellation.command_id)

        cleanup_request = CancelAgentAttemptRequest(
            execution.request.run_id,
            accepted.attempt.attempt_id,
            cancellation.command_id,
            cancellation.expected_attempt_state_version,
            cancellation.replacement,
        )
        terminal = store.attest_cancellation_cleanup(
            cleanup_request,
            AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
            None,
            None,
        )
        assert terminal.attempt.state is AgentAttemptState.CANCELLED

        retry = cancel_run_result(
            execution.request.run_id,
            "operator-once-1",
            execution.request.node_execution_id,
            store,
        )

        assert isinstance(retry, CancelTerminalRetry)
        assert retry.run.state is RunState.CANCELLED
        assert _cancel_event_kinds(runtime.engine) == [
            RunEventKind.AGENT_CANCEL_REQUESTED.value,
            RunEventKind.AGENT_CANCELLED.value,
        ]
    finally:
        runtime.close()


def test_the_legacy_carrier_lets_an_accepted_cancel_win_over_a_late_success(
    tmp_path: Path,
) -> None:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    try:
        execution = agent_attempt_execution(
            attempt_request(runtime, "run-cancel/legacy-cancel-wins")
        )
        store = DbosAgentAttemptStore(
            runtime.engine, runtime.settings.application_version
        )
        store.prepare(execution)
        request = CancelRunRequest(
            execution.request.run_id,
            "operator-legacy-wins-1",
            execution.request.node_execution_id,
        )

        accepted = store.request_run_cancellation(request)
        assert isinstance(accepted, RunCancellationAccepted)
        assert accepted.attempt.state is AgentAttemptState.CANCEL_REQUESTED

        with pytest.raises(RunTransitionConflict, match="armed current attempt"):
            store.complete_success(execution, AgentExecutionResult(b"too late"))
    finally:
        runtime.close()


def test_the_legacy_carrier_lifts_the_run_cancelled_with_its_operator_receipt(
    tmp_path: Path,
) -> None:
    """#439 P3: the cleanup attestation that ends the attempt also ends the run.

    Both write in the one transaction `attest_cancellation_cleanup` already
    holds -- the `cancelled` receipt names the process disposition in words,
    and the run's own terminal word is `CANCELLED`, not `FAILED`: it stood
    still because the operator asked, not because anything failed.
    """
    runtime, store, execution = _v3_prepared(tmp_path, "run-cancel/legacy-receipt")
    try:
        request = CancelRunRequest(
            execution.request.run_id,
            "operator-legacy-receipt-1",
            execution.request.node_execution_id,
        )
        accepted = store.request_run_cancellation(request)
        assert isinstance(accepted, RunCancellationAccepted)
        cancellation = accepted.attempt.cancellation
        assert cancellation is not None

        terminal = store.attest_cancellation_cleanup(
            CancelAgentAttemptRequest(
                execution.request.run_id,
                accepted.attempt.attempt_id,
                cancellation.command_id,
                cancellation.expected_attempt_state_version,
                cancellation.replacement,
            ),
            AgentAttemptCancellationDisposition.EXITED_BEFORE_SIGNAL,
            _LEGACY_OWNER,
            _LEGACY_GENERATION,
        )
        assert terminal.attempt.state is AgentAttemptState.CANCELLED

        with runtime.engine.connect() as connection:
            canonical_run = load_run(connection, execution.request.run_id)
        assert canonical_run.state is RunState.CANCELLED
        assert canonical_run.terminal_hash is not None
        assert _node_receipt(runtime.engine, execution.request.node_execution_id) == (
            PersistedReceiptDisposition.CANCELLED.value,
            "cancelled-by-operator: EXITED_BEFORE_SIGNAL",
        )
    finally:
        runtime.close()


def test_a_parent_death_disposition_still_lifts_the_run_cancelled_not_failed(
    tmp_path: Path,
) -> None:
    """Fenster (i): the attempt ends `INTERRUPTED`, the run still says `CANCELLED`.

    `OWNER_LOST_AFTER_PARENT_DEATH` is the one disposition that leaves the
    attempt `INTERRUPTED` rather than `CANCELLED` -- the two-axis doctrine
    (#439 Bauplan P3) keeps the run's own word bound to the operator's command
    identity regardless, so it never reads `FAILED` for a run the operator
    itself stopped.
    """
    runtime, store, execution = _v3_prepared(tmp_path, "run-cancel/parent-death")
    try:
        request = CancelRunRequest(
            execution.request.run_id,
            "operator-parent-death-1",
            execution.request.node_execution_id,
        )
        accepted = store.request_run_cancellation(request)
        assert isinstance(accepted, RunCancellationAccepted)
        cancellation = accepted.attempt.cancellation
        assert cancellation is not None

        terminal = store.attest_cancellation_cleanup(
            CancelAgentAttemptRequest(
                execution.request.run_id,
                accepted.attempt.attempt_id,
                cancellation.command_id,
                cancellation.expected_attempt_state_version,
                cancellation.replacement,
            ),
            AgentAttemptCancellationDisposition.OWNER_LOST_AFTER_PARENT_DEATH,
            _LEGACY_OWNER,
            _LEGACY_GENERATION,
        )
        assert terminal.attempt.state is AgentAttemptState.INTERRUPTED

        with runtime.engine.connect() as connection:
            canonical_run = load_run(connection, execution.request.run_id)
        assert canonical_run.state is RunState.CANCELLED
        assert _node_receipt(runtime.engine, execution.request.node_execution_id) == (
            PersistedReceiptDisposition.CANCELLED.value,
            "cancelled-by-operator: OWNER_LOST_AFTER_PARENT_DEATH",
        )
    finally:
        runtime.close()


def test_an_attempt_route_cancel_never_lifts_the_run(tmp_path: Path) -> None:
    """The command's *identity*, not the resulting attempt state, gates the lift.

    A cancellation submitted through the attempt route (#15) reaches the same
    `CANCELLED` attempt state an operator run-cancel does, but its command id
    was never minted by `RunCancelCommandId.for_key` -- `is_operator_run_cancel`
    refuses it, so #439 P3's lift leaves this run exactly `STARTED` and this
    node exactly receipt-less, unchanged from before P3 existed.
    """
    runtime, store, execution = _v3_prepared(tmp_path, "run-cancel/attempt-route")
    try:
        prepared = store.load(execution.attempt_id)
        command = CancelAgentAttemptRequest(
            execution.request.run_id,
            execution.attempt_id,
            "attempt-route-command",
            prepared.state_version,
            AgentAttemptReplacement.NONE,
        )
        accepted = store.request_cancellation(command)
        assert isinstance(accepted, AgentAttemptCancellationAccepted)

        terminal = store.attest_cancellation_cleanup(
            command,
            AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
            _LEGACY_OWNER,
            _LEGACY_GENERATION,
        )
        assert terminal.attempt.state is AgentAttemptState.CANCELLED

        with runtime.engine.connect() as connection:
            canonical_run = load_run(connection, execution.request.run_id)
        assert canonical_run.state is RunState.STARTED
        assert (
            _node_receipt(runtime.engine, execution.request.node_execution_id) is None
        )
    finally:
        runtime.close()


def test_a_started_run_with_a_terminal_current_attempt_reads_between_nodes_both_ways(
    tmp_path: Path,
) -> None:
    """#439 P6: one durable state, one honest sentence on both paths.

    An attempt-route cancel ends this node's attempt `CANCELLED` yet leaves the
    run `STARTED` (`is_operator_run_cancel` refuses that command's lift). A fresh
    operator run-cancel now meets a running run whose current attempt is already
    terminal. The projection the cockpit reads and the store's refusal to the
    submitted command must name the *same* reason -- the run is not ended, it is
    between nodes -- or the operator sees one sentence before pressing and a
    different one after.
    """
    runtime, store, execution = _v3_prepared(tmp_path, "run-cancel/between-both-ways")
    try:
        prepared = store.load(execution.attempt_id)
        attempt_route = CancelAgentAttemptRequest(
            execution.request.run_id,
            execution.attempt_id,
            "attempt-route-command",
            prepared.state_version,
            AgentAttemptReplacement.NONE,
        )
        assert isinstance(
            store.request_cancellation(attempt_route), AgentAttemptCancellationAccepted
        )
        terminal = store.attest_cancellation_cleanup(
            attempt_route,
            AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
            _LEGACY_OWNER,
            _LEGACY_GENERATION,
        )
        assert terminal.attempt.state is AgentAttemptState.CANCELLED
        with runtime.engine.connect() as connection:
            assert load_run(connection, execution.request.run_id).state is (
                RunState.STARTED
            )

        submitted = store.request_run_cancellation(
            CancelRunRequest(
                execution.request.run_id,
                "operator-between-both-ways-1",
                execution.request.node_execution_id,
            )
        )
        found = durable_queries(runtime.engine).get_run(execution.request.run_id)
        assert isinstance(found, RunFound)
        resource = run_resource(found.projection)
        assert isinstance(resource, RunResourceV3)

        assert submitted == RunCancellationNotCancellable(
            RunCancellationRefusal.BETWEEN_NODES
        )
        assert resource.cancellation.cancellable is False
        assert (
            resource.cancellation.reason == RunCancellationRefusal.BETWEEN_NODES.value
        )
    finally:
        runtime.close()


def test_a_late_completion_after_the_lift_stays_loud_and_does_not_revive_the_run(
    tmp_path: Path,
) -> None:
    """Absturzfenster (ii): the losing driver's late write ends loud, not silent.

    By the time a driver whose node the operator already cancelled reaches
    its own completion write, the run is no longer `STARTED` -- `complete_success`
    refuses with `RunTransitionConflict` rather than writing a second event or
    reopening the run #439 P3 already closed.
    """
    runtime, store, execution = _v3_prepared(tmp_path, "run-cancel/late-completion")
    try:
        request = CancelRunRequest(
            execution.request.run_id,
            "operator-late-completion-1",
            execution.request.node_execution_id,
        )
        accepted = store.request_run_cancellation(request)
        assert isinstance(accepted, RunCancellationAccepted)
        cancellation = accepted.attempt.cancellation
        assert cancellation is not None
        store.attest_cancellation_cleanup(
            CancelAgentAttemptRequest(
                execution.request.run_id,
                accepted.attempt.attempt_id,
                cancellation.command_id,
                cancellation.expected_attempt_state_version,
                cancellation.replacement,
            ),
            AgentAttemptCancellationDisposition.EXITED_BEFORE_SIGNAL,
            _LEGACY_OWNER,
            _LEGACY_GENERATION,
        )
        before_events = _cancel_event_kinds(runtime.engine)

        with pytest.raises(RunTransitionConflict, match="armed current attempt"):
            store.complete_success(execution, AgentExecutionResult(b"too late"))

        assert _cancel_event_kinds(runtime.engine) == before_events
        with runtime.engine.connect() as connection:
            canonical_run = load_run(connection, execution.request.run_id)
        assert canonical_run.state is RunState.CANCELLED
    finally:
        runtime.close()
