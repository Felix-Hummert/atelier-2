"""How the Serve-restart convergence classifies each Attempt (`#585`).

The orchestration drives exactly one convergence per Attempt and sorts the
outcome: an Attempt whose retained fact commits reaches a terminal and is
reported converged; one whose fact is missing, corrupt, or refused is left
exactly as durable as it was and reported non-terminal. These fakes stand in
for the store and the retained-evidence source so the classification is what is
under test, not the store's own commit rules.
"""

from __future__ import annotations

from typing import cast

from atelier2.application.converge_driverless_runner_lease_attempts import (
    RunnerLeaseConvergenceJob,
    converge_driverless_runner_lease_attempts,
)
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
    RunnerProviderResult,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.runner_terminal_evidence_codec import (
    RunnerTerminalEvidenceRecordMissing,
)
from atelier2.ports.agent_attempts import (
    RunnerTerminalEvidenceCommitted,
    RunnerTerminalEvidenceSource,
    RunnerTerminalEvidenceSourceReadback,
    RunnerTerminalEvidenceStore,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_execution_request_v2,
    prepared_agent_attempt,
)


class _Source:
    def __init__(
        self,
        binding: RunnerGenerationBinding,
        readback: RunnerTerminalEvidenceSourceReadback,
    ) -> None:
        self._binding = binding
        self._readback = readback
        self.readbacks = 0

    def readback(
        self, binding: RunnerGenerationBinding
    ) -> RunnerTerminalEvidenceSourceReadback:
        assert binding == self._binding
        self.readbacks += 1
        return self._readback

    def acknowledge(
        self,
        envelope: RunnerTerminalEvidenceEnvelope,
        accepted_hash: RunnerTerminalEvidenceHash,
    ) -> RunnerTerminalEvidenceAckTombstone:
        return RunnerTerminalEvidenceAckTombstone(
            envelope.binding, envelope.invocation_id, accepted_hash
        )


class _Store:
    def __init__(self, terminal_by_attempt: dict[str, AgentAttempt]) -> None:
        self._terminal_by_attempt = terminal_by_attempt
        self.commits = 0

    def commit_runner_terminal_evidence(
        self,
        execution: AgentAttemptExecution,
        envelope: RunnerTerminalEvidenceEnvelope,
    ) -> RunnerTerminalEvidenceCommitted:
        self.commits += 1
        return RunnerTerminalEvidenceCommitted(
            self._terminal_by_attempt[execution.attempt_id.value],
            RunnerTerminalEvidenceHash.for_envelope(envelope),
        )

    def mark_runner_evidence_acknowledged(
        self,
        execution: AgentAttemptExecution,
        tombstone: RunnerTerminalEvidenceAckTombstone,
    ) -> AgentAttempt:
        return self._terminal_by_attempt[execution.attempt_id.value]


def _binding(
    execution: AgentAttemptExecution, run_name: str
) -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        execution.attempt_id,
        execution.request.request_hash,
        RunnerGenerationId(f"generation-{run_name}"),
        RunnerManifestId("d" * 64),
    )


def _job_and_terminal(
    run_name: str, readback: RunnerTerminalEvidenceSourceReadback
) -> tuple[RunnerLeaseConvergenceJob, AgentAttempt, _Source]:
    execution = agent_attempt_execution(agent_execution_request_v2(run_name))
    binding = _binding(execution, run_name)
    source = _Source(binding, readback)
    terminal = prepared_agent_attempt(execution)
    job = RunnerLeaseConvergenceJob(
        execution, binding, cast(RunnerTerminalEvidenceSource, source)
    )
    return job, terminal, source


def _envelope(binding: RunnerGenerationBinding) -> RunnerTerminalEvidenceEnvelope:
    return RunnerTerminalEvidenceEnvelope(
        binding,
        RunnerInvocationId("runner-invocation-1"),
        RunnerProviderResult(AgentExecutionResult(b"{}")),
    )


def test_a_committable_fact_converges_its_attempt_to_a_terminal() -> None:
    execution = agent_attempt_execution(agent_execution_request_v2("585/converges"))
    binding = _binding(execution, "585/converges")
    source = _Source(binding, _envelope(binding))
    terminal = prepared_agent_attempt(execution)
    job = RunnerLeaseConvergenceJob(
        execution, binding, cast(RunnerTerminalEvidenceSource, source)
    )
    store = _Store({execution.attempt_id.value: terminal})

    report = converge_driverless_runner_lease_attempts(
        [job], cast(RunnerTerminalEvidenceStore, store)
    )

    assert report.converged == (terminal.attempt_id,)
    assert report.left_nonterminal == ()
    assert store.commits == 1
    assert source.readbacks == 1


def test_a_missing_fact_leaves_its_attempt_non_terminal_and_is_named() -> None:
    job, _terminal, _source = _job_and_terminal(
        "585/missing", RunnerTerminalEvidenceRecordMissing()
    )
    store = _Store({})

    report = converge_driverless_runner_lease_attempts(
        [job], cast(RunnerTerminalEvidenceStore, store)
    )

    assert report.converged == ()
    assert report.left_nonterminal == (
        (job.execution.attempt_id, "RunnerTerminalEvidenceRecordMissing"),
    )
    assert store.commits == 0


def test_each_attempt_is_read_back_exactly_once() -> None:
    jobs_sources = [
        _job_and_terminal(f"585/once-{index}", RunnerTerminalEvidenceRecordMissing())
        for index in range(3)
    ]
    jobs = [job for job, _terminal, _source in jobs_sources]

    converge_driverless_runner_lease_attempts(
        jobs, cast(RunnerTerminalEvidenceStore, _Store({}))
    )

    assert all(source.readbacks == 1 for _job, _terminal, source in jobs_sources)


def test_no_jobs_converge_nothing() -> None:
    report = converge_driverless_runner_lease_attempts(
        [], cast(RunnerTerminalEvidenceStore, _Store({}))
    )

    assert report.converged == ()
    assert report.left_nonterminal == ()
