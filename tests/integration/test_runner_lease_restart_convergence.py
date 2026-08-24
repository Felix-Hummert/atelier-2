"""A driverless Runner-lease Attempt converges over its retained fact (`#585`).

The launcher lays a Runner's own terminal record in the Attempt's handoff; on
its own restart Serve reads it back through `FileRunnerTerminalEvidenceSource`
and commits it over the real durable store. These tests drive that exact path
against real durable state: an Attempt left armed reaches the terminal the
Runner actually reported -- not `INTERRUPTED`, not a permanent hang -- exactly
once, and one whose fact never reached the handoff is left armed and honest.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from atelier2.adapters.dbos import runtime as dbos_runtime
from atelier2.adapters.dbos.workflow import (
    AgentExecutorMap,
    reconstruct_agent_attempt,
)
from atelier2.adapters.file_runner_terminal_evidence import (
    FileRunnerTerminalEvidenceSource,
)
from atelier2.application.converge_driverless_runner_lease_attempts import (
    RunnerLeaseConvergenceJob,
    RunnerLeaseConvergenceReport,
    converge_driverless_runner_lease_attempts,
)
from atelier2.application.converge_runner_terminal_evidence import (
    converge_runner_terminal_evidence,
)
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptState,
    ProcessExitSignature,
    RunnerEvidenceAcceptancePhase,
    RunnerInvocationId,
    RunnerProviderFailure,
    RunnerTerminalEvidenceEnvelope,
)
from atelier2.contracts.runner_terminal_evidence_codec import (
    RunnerTerminalEvidenceRecordMissing,
    encode_runner_terminal_evidence_record,
)
from tests.integration.test_runner_terminal_evidence_store import _bound, _v3_armed
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_execution_request_v2,
    prepared_agent_attempt,
)


def _retain(attempts_root: Path, attempt_id: str, record: bytes) -> None:
    handoff = attempts_root / attempt_id / "handoff"
    handoff.mkdir(mode=0o700, parents=True)
    (handoff / "retained-terminal-record").write_bytes(record)


def _executor_map_of(runtime: object) -> AgentExecutorMap:
    """The executor map the runtime composes, rebuilt for reconstruction.

    Reconstruction reads each key's operational identity and capabilities, not
    its opened executor, so the map a convergence needs is derivable from the
    registry alone -- the same map `runtime._agent_executor_map` builds at
    composition, with an unopened executor stub where the real one lives.
    """
    registry = runtime.agent_executor_registry  # type: ignore[attr-defined]
    return {
        entry.key: (
            None,
            entry.manifest_entry.operational_identity,
            entry.manifest_entry.declared_capabilities,
            entry.manifest_entry.carrier,
        )
        for entry in registry.entries
    }


def test_a_retained_fact_converges_an_armed_attempt_to_its_real_terminal(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding = _bound(tmp_path, "585/converges-to-terminal")
    try:
        invocation = RunnerInvocationId("runner-invocation-1")
        store.arm_runner_invocation(execution, binding, invocation)
        envelope = RunnerTerminalEvidenceEnvelope(
            binding,
            invocation,
            RunnerProviderFailure(ProcessExitSignature(9, b"provider died")),
        )
        attempts_root = tmp_path / "attempts-root"
        _retain(
            attempts_root,
            execution.attempt_id.value,
            encode_runner_terminal_evidence_record(envelope),
        )
        source = FileRunnerTerminalEvidenceSource(attempts_root)

        converged = converge_runner_terminal_evidence(execution, binding, source, store)

        assert isinstance(converged, AgentAttempt)
        durable = store.load(execution.attempt_id)
        assert durable.state is AgentAttemptState.FAILED
        assert durable.state is not AgentAttemptState.INTERRUPTED
        assert durable.state is not AgentAttemptState.LAUNCH_ARMED
        assert durable.runner_evidence_acceptance_phase is (
            RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
        )
    finally:
        runtime.close()


def test_converging_the_same_retained_fact_twice_ends_it_once(tmp_path: Path) -> None:
    runtime, store, execution, binding = _bound(tmp_path, "585/idempotent")
    try:
        invocation = RunnerInvocationId("runner-invocation-1")
        store.arm_runner_invocation(execution, binding, invocation)
        envelope = RunnerTerminalEvidenceEnvelope(
            binding,
            invocation,
            RunnerProviderFailure(ProcessExitSignature(9, b"provider died")),
        )
        attempts_root = tmp_path / "attempts-root"
        _retain(
            attempts_root,
            execution.attempt_id.value,
            encode_runner_terminal_evidence_record(envelope),
        )
        source = FileRunnerTerminalEvidenceSource(attempts_root)

        first = converge_runner_terminal_evidence(execution, binding, source, store)
        second = converge_runner_terminal_evidence(execution, binding, source, store)

        assert isinstance(first, AgentAttempt)
        assert isinstance(second, AgentAttempt)
        assert first.attempt_id == second.attempt_id
        assert store.load(execution.attempt_id).state is AgentAttemptState.FAILED
    finally:
        runtime.close()


def test_no_retained_fact_leaves_the_attempt_armed_and_non_terminal(
    tmp_path: Path,
) -> None:
    runtime, store, execution, binding = _bound(tmp_path, "585/record-missing")
    try:
        store.arm_runner_invocation(
            execution, binding, RunnerInvocationId("runner-invocation-1")
        )
        source = FileRunnerTerminalEvidenceSource(tmp_path / "attempts-root")

        result = converge_runner_terminal_evidence(execution, binding, source, store)

        assert isinstance(result, RunnerTerminalEvidenceRecordMissing)
        assert store.load(execution.attempt_id).state is AgentAttemptState.LAUNCH_ARMED
    finally:
        runtime.close()


def test_reconstruction_yields_the_durable_attempts_exact_request_hash(
    tmp_path: Path,
) -> None:
    """The one owner both the replacement workflow and this convergence reuse
    (`reconstruct_agent_attempt`) re-derives a request whose hash is
    byte-identical to the durable attempt's -- a drifting derivation would make
    the store refuse the very evidence #585 exists to commit."""
    runtime, store, execution, _binding, _invocation = _v3_armed(
        tmp_path, "585/reconstruct-request-hash"
    )
    try:
        durable = store.load(execution.attempt_id)

        reconstructed = reconstruct_agent_attempt(
            runtime.datasource,
            _executor_map_of(runtime),
            runtime.declared_project,
            durable,
        )

        assert reconstructed.execution.attempt_id == durable.attempt_id
        assert reconstructed.execution.request.request_hash == durable.request_hash
        assert (
            reconstructed.execution.request.node_execution_id
            == durable.node_execution_id
        )
    finally:
        runtime.close()


def test_the_runtime_pipeline_converges_a_driverless_attempt_to_its_terminal(
    tmp_path: Path,
) -> None:
    """The exact assembly the Serve-restart sweep runs: reconstruct the armed
    Attempt, read its retained fact back off the handoff, and converge it once.
    Proves the pieces compose to a real terminal over a real store, without the
    launch's DBOS-recovery race."""
    runtime, store, execution, binding, invocation = _v3_armed(
        tmp_path, "585/runtime-pipeline"
    )
    try:
        envelope = RunnerTerminalEvidenceEnvelope(
            binding,
            invocation,
            RunnerProviderFailure(ProcessExitSignature(9, b"provider died")),
        )
        attempts_root = tmp_path / "leases" / "attempts"
        _retain(
            attempts_root,
            execution.attempt_id.value,
            encode_runner_terminal_evidence_record(envelope),
        )
        durable = store.load(execution.attempt_id)
        reconstructed = reconstruct_agent_attempt(
            runtime.datasource,
            _executor_map_of(runtime),
            runtime.declared_project,
            durable,
        )
        job = RunnerLeaseConvergenceJob(
            reconstructed.execution,
            binding,
            FileRunnerTerminalEvidenceSource(attempts_root),
        )

        report = converge_driverless_runner_lease_attempts([job], store)

        assert report.converged == (execution.attempt_id,)
        assert report.left_nonterminal == ()
        assert store.load(execution.attempt_id).state is AgentAttemptState.FAILED
    finally:
        runtime.close()


class _RaisesIfTouched:
    """A host authority that fails the test the moment the sweep reads it.

    The Serve-restart Runner-lease convergence must reach a Runner's own
    retained terminal fact and nothing else: it never starts a process, owns a
    workspace, or otherwise reaches process or host authority. Standing in for
    those authorities with an object that raises on any attribute access turns
    that structural property into a test that fails loudly if it is ever broken.
    """

    def __getattr__(self, name: str) -> object:
        raise AssertionError(
            f"the runner-lease convergence reached host authority .{name}"
        )


def test_one_unreconstructable_attempt_is_left_nonterminal_and_the_rest_converge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An Attempt whose executor left config between restarts raises deep in
    reconstruction; the sweep must leave exactly that one non-terminal and
    named, converge every healthy Attempt, and never fail Serve start over one
    config change."""
    runtime, store, execution, binding, invocation = _v3_armed(
        tmp_path, "585/one-unreconstructable-attempt"
    )
    try:
        envelope = RunnerTerminalEvidenceEnvelope(
            binding,
            invocation,
            RunnerProviderFailure(ProcessExitSignature(9, b"provider died")),
        )
        attempts_root = tmp_path / "attempts-root"
        _retain(
            attempts_root,
            execution.attempt_id.value,
            encode_runner_terminal_evidence_record(envelope),
        )
        healthy = store.load(execution.attempt_id)
        left_config = prepared_agent_attempt(
            agent_attempt_execution(agent_execution_request_v2("585/executor-removed"))
        )

        original = dbos_runtime.reconstruct_agent_attempt

        def reconstruct_but_one_executor_is_gone(
            datasource: object,
            executors: AgentExecutorMap,
            project: object,
            attempt: AgentAttempt,
        ) -> object:
            if attempt.attempt_id == left_config.attempt_id:
                raise KeyError("agent executor removed from config")
            return original(datasource, executors, project, attempt)  # type: ignore[arg-type]

        monkeypatch.setattr(
            dbos_runtime,
            "reconstruct_agent_attempt",
            reconstruct_but_one_executor_is_gone,
        )

        jobs, left_nonterminal = dbos_runtime._reconstructed_runner_lease_jobs(
            runtime._held(),
            _executor_map_of(runtime),
            FileRunnerTerminalEvidenceSource(attempts_root),
            (left_config, healthy),
        )

        assert [job.execution.attempt_id for job in jobs] == [execution.attempt_id]
        assert left_nonterminal == [(left_config.attempt_id, "KeyError")]

        report = converge_driverless_runner_lease_attempts(jobs, store)
        assert report.converged == (execution.attempt_id,)
        assert report.left_nonterminal == ()
        assert store.load(execution.attempt_id).state is AgentAttemptState.FAILED

        with caplog.at_level(logging.WARNING):
            dbos_runtime._log_runner_lease_convergence(
                RunnerLeaseConvergenceReport(
                    report.converged,
                    (*left_nonterminal, *report.left_nonterminal),
                )
            )
        assert any(
            getattr(record, "event", None) == "runner_lease_attempt_left_nonterminal"
            and getattr(record, "attempt_id", None) == left_config.attempt_id.value
            and getattr(record, "reason", None) == "KeyError"
            for record in caplog.records
        )
    finally:
        runtime.close()


def test_the_runner_lease_convergence_never_reaches_process_or_host_authority(
    tmp_path: Path,
) -> None:
    """The convergence reads a Runner's retained fact and commits it; it must
    never touch the process supervisor or the workspace owner. Standing those in
    with authorities that raise on any access proves the property structurally:
    a healthy Attempt still reaches its real terminal without either being
    reached."""
    runtime, store, execution, binding, invocation = _v3_armed(
        tmp_path, "585/no-host-authority"
    )
    bound = runtime._held()
    real_supervisor = bound.agent_process_supervisor
    real_workspace_owner = bound.agent_workspace_owner
    try:
        envelope = RunnerTerminalEvidenceEnvelope(
            binding,
            invocation,
            RunnerProviderFailure(ProcessExitSignature(9, b"provider died")),
        )
        attempts_root = tmp_path / "attempts-root"
        _retain(
            attempts_root,
            execution.attempt_id.value,
            encode_runner_terminal_evidence_record(envelope),
        )
        bound.agent_process_supervisor = _RaisesIfTouched()  # type: ignore[assignment]
        bound.agent_workspace_owner = _RaisesIfTouched()  # type: ignore[assignment]

        jobs, left_nonterminal = dbos_runtime._reconstructed_runner_lease_jobs(
            bound,
            _executor_map_of(runtime),
            FileRunnerTerminalEvidenceSource(attempts_root),
            (store.load(execution.attempt_id),),
        )
        report = converge_driverless_runner_lease_attempts(jobs, store)

        assert left_nonterminal == []
        assert report.converged == (execution.attempt_id,)
        assert store.load(execution.attempt_id).state is AgentAttemptState.FAILED
    finally:
        bound.agent_process_supervisor = real_supervisor
        bound.agent_workspace_owner = real_workspace_owner
        runtime.close()
