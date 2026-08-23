"""#439 P2: the durable run-cancel command in the store.

The command a route never gets to send yet (#439 P4) already has a full
answer at the store: `DbosAgentAttemptStore.request_run_cancellation` resolves
one operator idempotency key against the run's live attempt, recomputing the
node execution the operator's confirmation named rather than trusting it (D2).
These heads pin the ordering the bauplan requires -- a known command answers
before any cancellability gate, and the store's own truth never invents a
second cancellation engine, a run terminal lift, or a `cancelled` receipt:
that is #439 P3, and the P1 test that no writer produces `RunState.CANCELLED`
before it stays exactly as green as it was.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.run_transitions import RunTransitionConflict, load_run
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import run_events
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptReplacement,
    AgentAttemptState,
    CancelAgentAttemptRequest,
    RunnerProviderResult,
    RunnerTerminalEvidenceEnvelope,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import NodeExecutionId, RunEventKind
from atelier2.contracts.run_cancellations import CancelRunRequest
from atelier2.contracts.runs import RunId, RunState
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    RunCancellationAccepted,
    RunCancellationCommandConflict,
    RunCancellationNotCancellable,
    RunCancellationOvertakenBySuccess,
    RunCancellationRefusal,
    RunCancellationRunMissing,
    RunCancellationTerminalRetry,
    RunnerTerminalEvidenceCommitted,
)
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.integration.test_runner_terminal_evidence_store import _armed
from tests.integration.test_v3_bounded_loop_run import RUN as LOOP_RUN
from tests.integration.test_v3_bounded_loop_run import (
    finish_gated_node,
    gate_execution,
    start_loop,
    wait_for_state,
)
from tests.integration.test_v3_bounded_loop_run import (
    runtime as _loop_runtime,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_attempt_execution,
)

runtime = _loop_runtime


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
    wait_for_state(started_runtime, RunState.COMPLETED)


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
        # #439 P2 stops at CANCEL_REQUESTED/CANCELLED on the attempt; the run's
        # own terminal lift and its `cancelled` receipt are #439 P3's job.
        assert canonical_run.state is RunState.STARTED
        assert _cancel_event_kinds(runtime.engine) == [
            RunEventKind.AGENT_CANCEL_REQUESTED.value,
            RunEventKind.AGENT_CANCELLED.value,
        ]
    finally:
        runtime.close()


def test_runner_success_overtakes_an_accepted_run_cancel_command(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding, invocation = _armed(
        tmp_path, "run-cancel/runner-success-wins"
    )
    try:
        request = CancelRunRequest(
            execution.request.run_id,
            "operator-runner-success-1",
            execution.request.node_execution_id,
        )
        accepted = store.request_run_cancellation(request)
        assert isinstance(accepted, RunCancellationAccepted)
        assert accepted.attempt.state is AgentAttemptState.CANCEL_REQUESTED

        committed = store.commit_runner_terminal_evidence(
            execution,
            RunnerTerminalEvidenceEnvelope(
                binding,
                invocation,
                RunnerProviderResult(AgentExecutionResult(b"finished in hand")),
            ),
        )
        assert isinstance(committed, RunnerTerminalEvidenceCommitted)
        assert committed.attempt.state is AgentAttemptState.SUCCEEDED
        assert committed.attempt.cancellation is None

        retried = store.request_run_cancellation(request)

        assert isinstance(retried, RunCancellationOvertakenBySuccess)
        # The run kept going on the success; this command never ended it.
        assert retried.run.state is RunState.STARTED
        assert _cancel_event_kinds(runtime.engine) == [
            RunEventKind.AGENT_CANCEL_REQUESTED.value,
            RunEventKind.AGENT_COMPLETED.value,
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
