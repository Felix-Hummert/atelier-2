from __future__ import annotations

from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptProcessPhase,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationResult,
    AgentAttemptStore,
    TransactionalAgentAttemptCanceller,
)
from atelier2.ports.agent_executions import (
    AgentProcessOwnerNotLocal,
    AgentProcessRunner,
)


def cancel_agent_attempt(
    request: CancelAgentAttemptRequest,
    canceller: TransactionalAgentAttemptCanceller,
) -> AgentAttemptCancellationResult:
    return canceller.request_cancellation(request)


def continue_agent_attempt_cancellation(
    request: CancelAgentAttemptRequest,
    store: AgentAttemptStore,
    supervisor: AgentProcessRunner,
) -> AgentAttemptCancellationAccepted | None:
    """Attest one exact cleanup, or leave a durable redrive marker."""

    attempt = store.load(request.attempt_id)
    if attempt.state in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED}:
        result = store.request_cancellation(request)
        if not isinstance(result, AgentAttemptCancellationAccepted):
            raise RuntimeError("terminal cancellation retry lost its exact command")
        supervisor.release(result.attempt)
        return result
    if (
        attempt.process_phase is AgentAttemptProcessPhase.NONE
        and attempt.process_owner_id is None
    ):
        terminal = store.attest_cancellation_cleanup(
            request,
            AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
            None,
            None,
        )
        supervisor.release(terminal.attempt)
        return terminal
    try:
        disposition, owner, generation = supervisor.cancel(attempt)
    except AgentProcessOwnerNotLocal:
        store.mark_cancellation_owner_not_local(request)
        attempt = store.load(request.attempt_id)
        try:
            disposition, owner, generation = supervisor.recover(attempt)
        except AgentProcessOwnerNotLocal:
            return None
    terminal = store.attest_cancellation_cleanup(
        request, disposition, owner, generation
    )
    supervisor.release(terminal.attempt)
    return terminal
