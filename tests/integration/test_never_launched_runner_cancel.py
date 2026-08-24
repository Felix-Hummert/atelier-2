"""Operator run-cancel on a runner-lease attempt leased but never launched.

The convergence owner (`atelier2.application.cancel_runner_attempt`) driving a
real `DbosAgentAttemptStore` and a real `FileRunnerLeasePublisher` directory:
the only proof the attempt never launched is a *won* lease withdraw, and the
durable terminal (`CANCELLED` / `NEVER_LAUNCHED`, runner binding preserved, no
evidence fabricated) is written only behind it. A claimed lease defers to the
launched path without writing. The transport socket is never reached -- a
never-launched attempt has no session -- so nothing here scripts one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.run_transitions import RunTransitionConflict, load_run
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import run_events
from atelier2.adapters.file_runner_leases import (
    FileRunnerLeasePublisher,
    RunnerLeaseUnknown,
)
from atelier2.application.cancel_runner_attempt import (
    NeverLaunchedRunnerCancellationCommitted,
    RunnerCancellationDeferredToLaunchedPath,
    cancel_runner_attempt,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptRedriveState,
    AgentAttemptState,
    CancelAgentAttemptRequest,
    RunnerEvidenceAcceptancePhase,
)
from atelier2.contracts.executions import NodeExecutionId, RunEventKind
from atelier2.contracts.run_cancellations import CancelRunRequest
from atelier2.contracts.runner_leases import RunnerLeaseId
from atelier2.contracts.runs import RunState
from atelier2.ports.agent_attempts import RunCancellationAccepted
from tests.integration.test_runner_terminal_evidence_store import _armed, _bound


def _event_kinds(engine: sa.Engine) -> list[str]:
    with engine.connect() as connection:
        return [
            str(kind)
            for kind in connection.execute(
                sa.select(run_events.c.event_kind).order_by(run_events.c.event_sequence)
            ).scalars()
        ]


def _publisher(root: Path) -> FileRunnerLeasePublisher:
    lease_directory = root / "leases"
    return FileRunnerLeasePublisher(lease_directory, root / "attempts")


def _place_lease(root: Path, attempt_id: str, state: str) -> None:
    """Script the launcher/serve filesystem contract: one lease in one state.

    The document bytes are opaque to `withdraw`, which routes only on which
    lifecycle directory holds the lease id -- `open` is serve's own, `claimed`
    is a launcher's. The publisher's constructor already created the lifecycle
    directories; writing the file directly stands a lease in exactly the state
    a real serve or launcher would have left it.
    """
    document = root / "leases" / state / f"{attempt_id}.json"
    document.write_text("{}", encoding="ascii")


def _cancel_requested_never_launched(
    root: Path, run_name: str
) -> tuple[
    DbosAgentAttemptStore,
    NodeExecutionId,
    CancelAgentAttemptRequest,
    sa.Engine,
    DbosRuntime,
    str,
]:
    runtime, store, execution, _binding = _bound(root, run_name)
    accepted = store.request_run_cancellation(
        CancelRunRequest(
            execution.request.run_id,
            f"operator-{run_name}",
            execution.request.node_execution_id,
        )
    )
    assert isinstance(accepted, RunCancellationAccepted)
    assert accepted.attempt.state is AgentAttemptState.CANCEL_REQUESTED
    cancellation = accepted.attempt.cancellation
    assert cancellation is not None
    request = CancelAgentAttemptRequest(
        execution.request.run_id,
        execution.attempt_id,
        cancellation.command_id,
        cancellation.expected_attempt_state_version,
        cancellation.replacement,
    )
    return (
        store,
        execution.request.node_execution_id,
        request,
        runtime.engine,
        runtime,
        execution.attempt_id.value,
    )


def test_a_won_withdraw_ends_the_attempt_never_launched_and_lifts_the_run(
    tmp_path: Path,
) -> None:
    store, _node_execution_id, request, engine, runtime, attempt_id = (
        _cancel_requested_never_launched(tmp_path, "never-launched/won")
    )
    try:
        durable = store.load(request.attempt_id)
        publisher = _publisher(tmp_path)
        _place_lease(tmp_path, attempt_id, "open")

        outcome = cancel_runner_attempt(request, store, publisher)

        assert isinstance(outcome, NeverLaunchedRunnerCancellationCommitted)
        terminal = outcome.attempt
        assert terminal.state is AgentAttemptState.CANCELLED
        assert terminal.process_phase.value == "NONE"
        assert terminal.runner_invocation_id is None
        assert terminal.runner_terminal_evidence_hash is None
        assert (
            terminal.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.NONE
        )
        # The runner binding is preserved -- erasing it would make this row
        # indistinguishable from a process cancel.
        assert terminal.runner_manifest_id == durable.runner_manifest_id
        assert terminal.runner_generation_id == durable.runner_generation_id
        cancellation = terminal.cancellation
        assert cancellation is not None
        assert (
            cancellation.disposition
            is AgentAttemptCancellationDisposition.NEVER_LAUNCHED
        )
        assert cancellation.redrive_state is AgentAttemptRedriveState.CLEANUP_ATTESTED

        with engine.connect() as connection:
            run = load_run(connection, request.run_id)
        assert run.state is RunState.CANCELLED
        assert run.terminal_hash is not None

        assert not (tmp_path / "leases" / "open" / f"{attempt_id}.json").exists()
        assert (tmp_path / "leases" / "withdrawn" / f"{attempt_id}.json").is_file()

        assert _event_kinds(engine) == [
            RunEventKind.AGENT_CANCEL_REQUESTED.value,
            RunEventKind.AGENT_CANCELLED.value,
        ]
    finally:
        runtime.close()


def test_a_cancel_of_a_lease_that_was_never_published_propagates_without_committing(
    tmp_path: Path,
) -> None:
    """`#584` gap (a)/(b): a crash between `bind_runner_generation` and
    `leases.publish` leaves an Attempt manifest-bound with no lease document. A
    later cancel finds nothing to withdraw and fails loud with
    `RunnerLeaseUnknown` -- and crucially commits no terminal, so the Attempt
    stays `CANCEL_REQUESTED` rather than being lied about as CANCELLED. This
    pins that fail-loud, lie-free propagation as a regression."""
    store, _node_execution_id, request, engine, runtime, _attempt_id = (
        _cancel_requested_never_launched(tmp_path, "never-launched/no-lease")
    )
    try:
        publisher = _publisher(tmp_path)

        with pytest.raises(RunnerLeaseUnknown):
            cancel_runner_attempt(request, store, publisher)

        durable = store.load(request.attempt_id)
        assert durable.state is AgentAttemptState.CANCEL_REQUESTED
        assert durable.cancellation is not None
        assert durable.cancellation.disposition is None
        assert _event_kinds(engine) == [RunEventKind.AGENT_CANCEL_REQUESTED.value]
    finally:
        runtime.close()


def test_a_claimed_lease_defers_without_writing_and_leaves_the_run_started(
    tmp_path: Path,
) -> None:
    store, _node_execution_id, request, engine, runtime, attempt_id = (
        _cancel_requested_never_launched(tmp_path, "never-launched/claimed")
    )
    try:
        publisher = _publisher(tmp_path)
        _place_lease(tmp_path, attempt_id, "claimed")

        outcome = cancel_runner_attempt(request, store, publisher)

        assert isinstance(outcome, RunnerCancellationDeferredToLaunchedPath)
        durable = store.load(request.attempt_id)
        assert durable.state is AgentAttemptState.CANCEL_REQUESTED
        assert durable.cancellation is not None
        assert durable.cancellation.disposition is None

        with engine.connect() as connection:
            run = load_run(connection, request.run_id)
        assert run.state is RunState.STARTED
        # A launcher owns the claimed lease; this path never touches it.
        assert (tmp_path / "leases" / "claimed" / f"{attempt_id}.json").is_file()
        assert _event_kinds(engine) == [RunEventKind.AGENT_CANCEL_REQUESTED.value]
    finally:
        runtime.close()


def test_a_repeated_convergence_ends_the_attempt_exactly_once(
    tmp_path: Path,
) -> None:
    store, _node_execution_id, request, engine, runtime, attempt_id = (
        _cancel_requested_never_launched(tmp_path, "never-launched/double")
    )
    try:
        publisher = _publisher(tmp_path)
        _place_lease(tmp_path, attempt_id, "open")

        first = cancel_runner_attempt(request, store, publisher)
        assert isinstance(first, NeverLaunchedRunnerCancellationCommitted)
        first_version = first.attempt.state_version

        second = cancel_runner_attempt(request, store, publisher)
        assert isinstance(second, NeverLaunchedRunnerCancellationCommitted)
        assert second.attempt.state is AgentAttemptState.CANCELLED
        # The idempotent short-circuit runs no second CAS.
        assert second.attempt.state_version == first_version

        assert _event_kinds(engine) == [
            RunEventKind.AGENT_CANCEL_REQUESTED.value,
            RunEventKind.AGENT_CANCELLED.value,
        ]
        with engine.connect() as connection:
            run = load_run(connection, request.run_id)
        assert run.state is RunState.CANCELLED
    finally:
        runtime.close()


def test_a_crash_between_the_withdraw_and_the_commit_still_ends_it_once(
    tmp_path: Path,
) -> None:
    store, _node_execution_id, request, engine, runtime, attempt_id = (
        _cancel_requested_never_launched(tmp_path, "never-launched/crash")
    )
    try:
        publisher = _publisher(tmp_path)
        _place_lease(tmp_path, attempt_id, "open")

        # The withdraw landed but the commit did not: a crash between the two.
        first_withdraw = publisher.withdraw(RunnerLeaseId(attempt_id))
        assert first_withdraw.lease_id.value == attempt_id
        assert (tmp_path / "leases" / "withdrawn" / f"{attempt_id}.json").is_file()
        with engine.connect() as connection:
            assert load_run(connection, request.run_id).state is RunState.STARTED

        # The durable workflow re-runs the owner: the re-withdraw is answered
        # from `withdrawn/` and the commit lands exactly once.
        outcome = cancel_runner_attempt(request, store, publisher)

        assert isinstance(outcome, NeverLaunchedRunnerCancellationCommitted)
        assert outcome.attempt.state is AgentAttemptState.CANCELLED
        cancellation = outcome.attempt.cancellation
        assert cancellation is not None
        assert (
            cancellation.disposition
            is AgentAttemptCancellationDisposition.NEVER_LAUNCHED
        )
        assert _event_kinds(engine) == [
            RunEventKind.AGENT_CANCEL_REQUESTED.value,
            RunEventKind.AGENT_CANCELLED.value,
        ]
        with engine.connect() as connection:
            assert load_run(connection, request.run_id).state is RunState.CANCELLED
    finally:
        runtime.close()


def test_the_store_commit_short_circuits_on_the_durable_terminal(
    tmp_path: Path,
) -> None:
    store, _node_execution_id, request, engine, runtime, attempt_id = (
        _cancel_requested_never_launched(tmp_path, "never-launched/short-circuit")
    )
    try:
        publisher = _publisher(tmp_path)
        _place_lease(tmp_path, attempt_id, "open")
        committed = cancel_runner_attempt(request, store, publisher)
        assert isinstance(committed, NeverLaunchedRunnerCancellationCommitted)

        # A bare second store call reads the terminal row back without a second
        # CAS, event or run lift.
        again = store.commit_never_launched_cancellation(request)
        assert again.attempt.state is AgentAttemptState.CANCELLED
        assert again.attempt.state_version == committed.attempt.state_version
        assert _event_kinds(engine) == [
            RunEventKind.AGENT_CANCEL_REQUESTED.value,
            RunEventKind.AGENT_CANCELLED.value,
        ]
    finally:
        runtime.close()


def test_a_launched_attempt_cannot_commit_never_launched(tmp_path: Path) -> None:
    runtime, store, execution, _binding, _invocation = _armed(
        tmp_path, "never-launched/launched-guard"
    )
    try:
        accepted = store.request_run_cancellation(
            CancelRunRequest(
                execution.request.run_id,
                "operator-launched-guard",
                execution.request.node_execution_id,
            )
        )
        assert isinstance(accepted, RunCancellationAccepted)
        cancellation = accepted.attempt.cancellation
        assert cancellation is not None
        request = CancelAgentAttemptRequest(
            execution.request.run_id,
            execution.attempt_id,
            cancellation.command_id,
            cancellation.expected_attempt_state_version,
            cancellation.replacement,
        )
        with pytest.raises(RunTransitionConflict, match="launched runner attempt"):
            store.commit_never_launched_cancellation(request)
    finally:
        runtime.close()
