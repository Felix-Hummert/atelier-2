"""Ask for one attempt to stop, and answer in this layer's own words.

Until now this handed the port's union straight back to the route, which made
the application layer a corridor rather than a layer: the route read the store's
vocabulary and the record that binds use cases named a port type. It was the last
such pass-through in the tree, left standing only because rewriting it meant
rewriting a module #70 was already rewriting. #70 landed, so it is translated
here like every neighbour: the store says what happened, and this says what it
means for the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptState,
    CancelAgentAttemptRequest,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptStore,
    TransactionalAgentAttemptCanceller,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted as DurableCancellationAccepted,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationCommandConflict as DurableCommandConflict,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationNotCurrent as DurableNotCurrent,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationRunMissing as DurableRunMissing,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationStale as DurableStale,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationTargetMissing as DurableTargetMissing,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationTerminalConflict as DurableTerminalConflict,
)
from atelier2.ports.agent_attempts import (
    AgentAttemptReplacementNotAllowed as DurableReplacementNotAllowed,
)
from atelier2.ports.agent_attempts import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.agent_attempts import (
    DurableWriteUnavailable as PortDurableWriteUnavailable,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceOwner,
    AgentProcessOwnerNotLocal,
    AgentSession,
)


@dataclass(frozen=True)
class CancellationAccepted:
    """The attempt was asked to stop, and whether it is already over."""

    attempt: AgentAttempt
    terminal: bool
    replacement_attempt_id: AgentAttemptId | None = None


@dataclass(frozen=True)
class CancellationRunMissing:
    pass


@dataclass(frozen=True)
class AttemptMissing:
    pass


@dataclass(frozen=True)
class AttemptNotCurrent:
    pass


@dataclass(frozen=True)
class CancellationStale:
    pass


@dataclass(frozen=True)
class AttemptAlreadyTerminal:
    pass


@dataclass(frozen=True)
class CommandConflict:
    pass


@dataclass(frozen=True)
class ReplacementNotAllowed:
    pass


type CancelAgentAttemptResult = (
    CancellationAccepted
    | CancellationRunMissing
    | AttemptMissing
    | AttemptNotCurrent
    | CancellationStale
    | AttemptAlreadyTerminal
    | CommandConflict
    | ReplacementNotAllowed
    | WriteUnavailable
    | DurableStateCorrupt
)


def cancel_agent_attempt(
    request: CancelAgentAttemptRequest,
    canceller: TransactionalAgentAttemptCanceller,
) -> CancelAgentAttemptResult:
    """One cancellation asked for, answered in this layer's vocabulary."""
    match canceller.request_cancellation(request):
        case DurableCancellationAccepted(attempt, terminal, replacement_attempt_id):
            return CancellationAccepted(attempt, terminal, replacement_attempt_id)
        case DurableRunMissing():
            return CancellationRunMissing()
        case DurableTargetMissing():
            return AttemptMissing()
        case DurableNotCurrent():
            return AttemptNotCurrent()
        case DurableStale():
            return CancellationStale()
        case DurableTerminalConflict():
            return AttemptAlreadyTerminal()
        case DurableCommandConflict():
            return CommandConflict()
        case DurableReplacementNotAllowed():
            return ReplacementNotAllowed()
        case PortDurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def continue_agent_attempt_cancellation(
    request: CancelAgentAttemptRequest,
    store: AgentAttemptStore,
    session: AgentSession,
    workspaces: AgentAttemptWorkspaceOwner,
) -> AgentAttemptCancellationAccepted | None:
    """Attest one exact cleanup, or leave a durable redrive marker.

    A cancelled attempt's workspace is removed only behind its durable cleanup
    attestation, so a run whose cleanup could not yet be attested keeps its
    directory for the recovery that will own it.
    """

    attempt = store.load(request.attempt_id)
    if attempt.state in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED}:
        result = store.request_cancellation(request)
        if not isinstance(result, AgentAttemptCancellationAccepted):
            raise RuntimeError("terminal cancellation retry lost its exact command")
        session.release(result.attempt)
        workspaces.release(request.attempt_id)
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
        session.release(terminal.attempt)
        workspaces.release(request.attempt_id)
        return terminal
    try:
        disposition, owner, generation = session.cancel(attempt)
    except AgentProcessOwnerNotLocal:
        store.mark_cancellation_owner_not_local(request)
        attempt = store.load(request.attempt_id)
        try:
            disposition, owner, generation = session.recover(attempt)
        except AgentProcessOwnerNotLocal:
            return None
    terminal = store.attest_cancellation_cleanup(
        request, disposition, owner, generation
    )
    session.release(terminal.attempt)
    workspaces.release(request.attempt_id)
    return terminal
