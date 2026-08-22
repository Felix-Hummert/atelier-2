"""#15-B4: the arm boundary at carrier loss.

Core's own durable `arm()` runs synchronously while it processes the
runner's `READY` frame -- strictly before it even constructs the `LAUNCH`
frame it will try to send back
(`run_runner_session.CoreRunnerSession._advance`, the `_CorePhase.READY`
case). This file pins the two honest endings that follow once a runner is
gone before its own terminal evidence ever lands, no matter which side of
`LAUNCH` the loss actually happened on:

- **C1** -- the runner never even reads `LAUNCH` (it crashed between
  sending `READY` and reading the reply). Because arming happens before
  `LAUNCH` is sent, the attempt is already durably `LAUNCH_ARMED` by the time
  this crash matters -- this corrects an initial framing that expected the
  attempt to stay `PREPARED`. No provider start was physically possible
  either way (the runner only calls `start_runner_child` after it has
  already read and validated a real `LAUNCH` frame, which this cut, by
  construction, never delivers), but the store's own contract carves out no
  exception for that: `commit_runner_terminal_evidence`'s `NEVER_LAUNCHED`
  branch requires `state is PREPARED`, and `arm()` already left `PREPARED`
  behind. The one legal path forward is the same one every other post-arm
  loss takes -- `RunnerInvocationLost` -> `POSSIBLY_RAN` -- never the
  pre-arm rebind gate (`#15-A`'s
  `test_acked_never_launched_is_the_only_pre_arm_rebind`, reachable only for
  a crash strictly before Core ever processes `READY`).
- **C2** -- the runner did receive `LAUNCH` and reported `STARTED`, and is
  then gone with its journal destroyed (nothing to reread on any
  reconnect). The honest ending is identical to C1's for the identical
  reason: `RunnerInvocationLost` -> `POSSIBLY_RAN`, never a second
  placement. What changes the ending is a *retained* journal record, proven
  here as the exact boundary that makes replay -- not `POSSIBLY_RAN` -- the
  honest answer.

`#15-A` already proved every store primitive these cuts land on
(`arm_runner_invocation`, `commit_runner_terminal_evidence`'s
`RunnerInvocationLost` and `NEVER_LAUNCHED` branches,
`rebind_after_acknowledged_never_launched`) idempotent and CAS-guarded in
isolation; this file proves the two wire-level crash points actually reach
them the way the design intends, against a real store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.runner_journal import RunnerJournal
from atelier2.application.run_runner_session import RunnerSessionRefusal
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    AgentAttemptState,
    RunnerBindingConflict,
    RunnerCancellation,
    RunnerCancellationObservation,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerInvocationLost,
    RunnerManifestId,
    RunnerProviderResult,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import AgentExecutionRequestHash, AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.run_projections import PublicAgentAttemptState
from atelier2.contracts.runner_sessions import RunnerSessionMessage
from atelier2.ports.agent_attempts import RunnerTerminalEvidenceCommitted
from atelier2.ports.run_queries import RunFound
from atelier2.runner.session import retained_terminal_record
from tests.integration.test_runner_session_application import _ready_payload
from tests.integration.test_runner_session_resume import (
    _drive_through_started,
    _frame,
    _session_for,
    _store_fixture,
)
from tests.scenarios.api import durable_queries


def _never_launched_rebind_is_refused(
    store: DbosAgentAttemptStore,
    execution: AgentAttemptExecution,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    tombstone: RunnerTerminalEvidenceAckTombstone,
) -> None:
    fresh = RunnerGenerationBinding(
        execution.attempt_id,
        execution.request.request_hash,
        RunnerGenerationId(f"{binding.generation_id.value}-fresh"),
        RunnerManifestId.of(binding.manifest_id.value.encode("ascii") + b"-fresh"),
    )
    with pytest.raises(RunnerBindingConflict):
        store.rebind_after_acknowledged_never_launched(execution, tombstone, fresh)
    foreign_invocation = RunnerInvocationId(f"foreign-{invocation.value}")
    with pytest.raises(RunnerBindingConflict):
        store.arm_runner_invocation(execution, binding, foreign_invocation)


def test_c1_ready_processed_durably_arms_before_launch_is_ever_constructed(
    tmp_path: Path,
) -> None:
    fixture = _store_fixture(tmp_path, "runner/b4/c1")
    try:
        session = _session_for(fixture, fixture.invocation)
        session.accept(
            _frame(
                fixture,
                fixture.invocation,
                RunnerSessionMessage.INVOCATION_OFFER,
                1,
            )
        )

        launch = session.accept(
            _frame(
                fixture,
                fixture.invocation,
                RunnerSessionMessage.READY,
                2,
                _ready_payload(),
            )
        )
        # The runner os._exit()s right here -- `launch` is never delivered to
        # anyone; discarding it models that a real socket write never happens.

        assert launch is not None
        assert launch.message is RunnerSessionMessage.LAUNCH
        durable = fixture.store.load(fixture.execution.attempt_id)
        assert durable.state is AgentAttemptState.LAUNCH_ARMED
        assert durable.runner_invocation_id == fixture.invocation

        never_launched = RunnerTerminalEvidenceEnvelope(
            fixture.binding,
            fixture.invocation,
            RunnerCancellation(
                "c1-never-launched", RunnerCancellationObservation.NEVER_LAUNCHED
            ),
        )
        with pytest.raises(RunnerBindingConflict):
            fixture.store.commit_runner_terminal_evidence(
                fixture.execution, never_launched
            )

        lost = RunnerTerminalEvidenceEnvelope(
            fixture.binding, fixture.invocation, RunnerInvocationLost()
        )
        committed = fixture.store.commit_runner_terminal_evidence(
            fixture.execution, lost
        )
        assert isinstance(committed, RunnerTerminalEvidenceCommitted)
        assert committed.attempt.state is AgentAttemptState.LAUNCH_ARMED
        found = durable_queries(fixture.runtime.engine).get_run(
            fixture.execution.request.run_id
        )
        assert isinstance(found, RunFound)
        assert found.projection.current_agent_attempt is not None
        assert (
            found.projection.current_agent_attempt.state
            is PublicAgentAttemptState.POSSIBLY_RAN
        )

        lost_hash = RunnerTerminalEvidenceHash.for_envelope(lost)
        tombstone = RunnerTerminalEvidenceAckTombstone(
            fixture.binding, fixture.invocation, lost_hash
        )
        fixture.store.mark_runner_evidence_acknowledged(fixture.execution, tombstone)

        _never_launched_rebind_is_refused(
            fixture.store,
            fixture.execution,
            fixture.binding,
            fixture.invocation,
            tombstone,
        )
    finally:
        fixture.runtime.close()


def test_c2_post_arm_loss_with_a_destroyed_journal_converges_to_possibly_ran(
    tmp_path: Path,
) -> None:
    fixture = _store_fixture(tmp_path, "runner/b4/c2")
    try:
        session = _session_for(fixture, fixture.invocation)
        _drive_through_started(fixture, session, fixture.invocation)

        destroyed_journal = RunnerJournal(tmp_path / "journal-c2-destroyed")
        with pytest.raises(ValueError, match="runner-terminal-record-missing"):
            destroyed_journal.readback(fixture.binding)
        assert retained_terminal_record(destroyed_journal, fixture.binding) is None

        lost = RunnerTerminalEvidenceEnvelope(
            fixture.binding, fixture.invocation, RunnerInvocationLost()
        )
        committed = fixture.store.commit_runner_terminal_evidence(
            fixture.execution, lost
        )
        assert isinstance(committed, RunnerTerminalEvidenceCommitted)
        assert committed.attempt.state is AgentAttemptState.LAUNCH_ARMED
        found = durable_queries(fixture.runtime.engine).get_run(
            fixture.execution.request.run_id
        )
        assert isinstance(found, RunFound)
        assert found.projection.current_agent_attempt is not None
        assert (
            found.projection.current_agent_attempt.state
            is PublicAgentAttemptState.POSSIBLY_RAN
        )

        # No second placement: a fresh invocation for the same binding still
        # collides with the durable arm the moment it reaches READY.
        second_invocation = RunnerInvocationId("C" * 43)
        second_session = _session_for(fixture, second_invocation)
        second_session.accept(
            _frame(fixture, second_invocation, RunnerSessionMessage.INVOCATION_OFFER, 1)
        )
        with pytest.raises(RunnerSessionRefusal, match="runner-arm-conflict"):
            second_session.accept(
                _frame(
                    fixture,
                    second_invocation,
                    RunnerSessionMessage.READY,
                    2,
                    _ready_payload(),
                )
            )

        lost_hash = RunnerTerminalEvidenceHash.for_envelope(lost)
        tombstone = RunnerTerminalEvidenceAckTombstone(
            fixture.binding, fixture.invocation, lost_hash
        )
        fixture.store.mark_runner_evidence_acknowledged(fixture.execution, tombstone)

        _never_launched_rebind_is_refused(
            fixture.store,
            fixture.execution,
            fixture.binding,
            fixture.invocation,
            tombstone,
        )
    finally:
        fixture.runtime.close()


def _boundary_binding() -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        AgentAttemptId("d" * 64),
        AgentExecutionRequestHash("e" * 64),
        RunnerGenerationId("D" * 43),
        RunnerManifestId("f" * 64),
    )


def test_c2_counterpart_a_retained_journal_replaces_possibly_ran_with_real_evidence(
    tmp_path: Path,
) -> None:
    """The exact boundary: `POSSIBLY_RAN` is what an *absent* journal produces.

    Same shape of post-`STARTED` loss as C2 above -- but this time the
    runner's journal already holds the envelope it published before it went
    away. `retained_terminal_record` (the one function both the resumed
    candidate and this boundary check read) returns that envelope instead of
    `None`, which is exactly what routes a resumed session
    (`run_candidate_session`, proven end to end by `#15-B3`'s
    `test_resume_delivers_retained_evidence_without_a_second_child`) to
    replay real evidence instead of ever reaching for `RunnerInvocationLost`.
    """
    binding = _boundary_binding()
    envelope = RunnerTerminalEvidenceEnvelope(
        binding,
        RunnerInvocationId("E" * 43),
        RunnerProviderResult(AgentExecutionResult(b"b4-boundary-result")),
    )
    retained_journal = RunnerJournal(tmp_path / "journal-retained")
    retained_journal.publish(envelope)

    assert retained_terminal_record(retained_journal, binding) == envelope

    destroyed_journal = RunnerJournal(tmp_path / "journal-destroyed")
    assert retained_terminal_record(destroyed_journal, binding) is None
