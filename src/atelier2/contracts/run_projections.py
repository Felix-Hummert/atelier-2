from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from atelier2.contracts.agent_attempts import AgentAttemptState


class PublicAgentAttemptState(StrEnum):
    """What a reader of a run is told about that run's current agent attempt."""

    PREPARED = "PREPARED"
    POSSIBLY_RAN = "POSSIBLY_RAN"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"


_PUBLIC_ATTEMPT_STATES: Mapping[AgentAttemptState, PublicAgentAttemptState | None] = {
    AgentAttemptState.PREPARED: PublicAgentAttemptState.PREPARED,
    AgentAttemptState.LAUNCH_ARMED: PublicAgentAttemptState.POSSIBLY_RAN,
    AgentAttemptState.CANCEL_REQUESTED: PublicAgentAttemptState.CANCEL_REQUESTED,
    AgentAttemptState.CANCELLED: PublicAgentAttemptState.CANCELLED,
    AgentAttemptState.INTERRUPTED: PublicAgentAttemptState.INTERRUPTED,
    AgentAttemptState.SUCCEEDED: None,
    AgentAttemptState.FAILED: PublicAgentAttemptState.FAILED,
}


def public_agent_attempt_state(
    durable_state: AgentAttemptState,
) -> PublicAgentAttemptState | None:
    """The public name of one durable attempt state, or None where a run has none.

    A succeeded attempt is never the current one: the transition that records the
    success also moves the run past it, so a reader is told about its successor.
    """
    return _PUBLIC_ATTEMPT_STATES[durable_state]
