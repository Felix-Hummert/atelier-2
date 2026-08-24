"""Converge every driverless Runner-lease Attempt to its real terminal (`#585`).

A Runner-lease Attempt whose driving workflow is gone after a Serve restart
stands armed forever: `converge_driverless_attempts` cannot answer for it (it
would mark it `INTERRUPTED`, inventing a terminal the Runner never reported),
and only the Runner's own retained terminal fact can say what actually
happened. The launcher lays that fact where Serve can read it; this drives one
convergence per Attempt over the exact terminal evidence, committing it once.

Pure orchestration: each Attempt is reconstructed into an execution, a binding
and a retained-evidence source by its caller, and this loops
`converge_runner_terminal_evidence` over them, classifying each outcome. An
Attempt whose fact is missing, corrupt, oversized, refused, or unacknowledgeable
is named and left exactly as durable as it already was -- never forced to a
terminal it cannot prove.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from atelier2.application.converge_runner_terminal_evidence import (
    converge_runner_terminal_evidence,
)
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptId,
    RunnerGenerationBinding,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.ports.agent_attempts import (
    RunnerTerminalEvidenceSource,
    RunnerTerminalEvidenceStore,
)


@dataclass(frozen=True, slots=True)
class RunnerLeaseConvergenceJob:
    """One driverless Attempt, reconstructed into what one convergence needs."""

    execution: AgentAttemptExecution
    binding: RunnerGenerationBinding
    source: RunnerTerminalEvidenceSource


@dataclass(frozen=True, slots=True)
class RunnerLeaseConvergenceReport:
    """Which Attempts reached a terminal and which were left non-terminal."""

    converged: tuple[AgentAttemptId, ...]
    left_nonterminal: tuple[tuple[AgentAttemptId, str], ...]


def converge_driverless_runner_lease_attempts(
    jobs: Iterable[RunnerLeaseConvergenceJob],
    store: RunnerTerminalEvidenceStore,
) -> RunnerLeaseConvergenceReport:
    """Converge each job's Attempt over its retained terminal fact, once each.

    Exactly one convergence per Attempt: the store's commit is hash-idempotent,
    so a re-run over an already-converged Attempt returns its terminal attempt
    again with no second effect. An Attempt whose retained fact is missing,
    corrupt, oversized, refused, or whose acknowledgement could not complete is
    not converged; it is named in the report, left exactly as durable as it
    already was.
    """
    converged: list[AgentAttemptId] = []
    left_nonterminal: list[tuple[AgentAttemptId, str]] = []
    for job in jobs:
        result = converge_runner_terminal_evidence(
            job.execution, job.binding, job.source, store
        )
        if isinstance(result, AgentAttempt):
            converged.append(result.attempt_id)
        else:
            left_nonterminal.append((job.execution.attempt_id, type(result).__name__))
    return RunnerLeaseConvergenceReport(tuple(converged), tuple(left_nonterminal))
