"""Converge one runner-lease attempt's operator cancel, and only on real proof.

A runner-lease-bound attempt that an operator cancels while it was leased but
never launched (`runner_manifest_id` set, `runner_invocation_id` NULL) has one
honest terminal: `CANCELLED` under disposition `NEVER_LAUNCHED`. The single
fact that proves it never launched is a *won* lease withdraw -- renaming the
attempt's open lease document into `withdrawn/`, which no launcher that already
claimed it could lose. So this owner withdraws first and commits second: the
durable terminal is written only behind a `RunnerLeaseWithdrawn`.

A `RunnerLeaseAlreadyClaimed` means a launcher owns the lease -- the attempt
was, or is being, launched -- so this path must not credit it a cancel it did
not converge. It is named and handed to the launched path (`#540` C-3.4, else
Kind #585), never retried in a loop here: retrying could not turn a claimed
lease back into a withdrawn one.

Ordering is load-bearing and crash-safe. A crash after the withdraw but before
the commit re-runs both: the withdraw is idempotent (the document is already in
`withdrawn/`, answered `RunnerLeaseWithdrawn` again) and the commit is
idempotent on the durable terminal row, so the attempt ends `NEVER_LAUNCHED`
exactly once. A crash after the commit re-runs neither beyond reading the
terminal row back.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.contracts.runner_leases import RunnerLeaseId
from atelier2.ports.agent_attempts import AgentAttemptStore
from atelier2.ports.runner_leases import (
    RunnerLeaseAlreadyClaimed,
    RunnerLeasePublisher,
)


@dataclass(frozen=True)
class NeverLaunchedRunnerCancellationCommitted:
    """The attempt ended CANCELLED / NEVER_LAUNCHED behind a won withdraw."""

    attempt: AgentAttempt


@dataclass(frozen=True)
class RunnerCancellationDeferredToLaunchedPath:
    """The lease was already claimed, so a launcher owns this attempt's ending.

    Nothing is written: the attempt stays `CANCEL_REQUESTED` for the launched
    path (`#540` C-3.4, else Kind #585) to converge over the launcher's own
    retained journal, the only source that can prove what actually happened.
    """


type CancelRunnerAttemptResult = (
    NeverLaunchedRunnerCancellationCommitted | RunnerCancellationDeferredToLaunchedPath
)


def cancel_runner_attempt(
    request: CancelAgentAttemptRequest,
    store: AgentAttemptStore,
    leases: RunnerLeasePublisher,
) -> CancelRunnerAttemptResult:
    """Withdraw the never-claimed lease, then commit the NEVER_LAUNCHED ending."""

    attempt = store.load(request.attempt_id)
    if attempt.state in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED}:
        return NeverLaunchedRunnerCancellationCommitted(
            store.commit_never_launched_cancellation(request).attempt
        )
    withdrawal = leases.withdraw(RunnerLeaseId(request.attempt_id.value))
    if isinstance(withdrawal, RunnerLeaseAlreadyClaimed):
        return RunnerCancellationDeferredToLaunchedPath()
    return NeverLaunchedRunnerCancellationCommitted(
        store.commit_never_launched_cancellation(request).attempt
    )
