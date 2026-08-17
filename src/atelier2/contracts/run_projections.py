from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.effects import EffectIntentSnapshot, ReconcileCommandSnapshot
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId
from atelier2.contracts.workflows_v3 import AnyWorkflowDocument


class NodeState(StrEnum):
    """What a reader of a run is told about one node of that run.

    Two axes flattened into one word, because a reader makes one display decision
    per node: where the node stands in its lifecycle, and — once it stands still —
    how it ended. Success has exactly one name here. Which fact proved it, a
    durable event or the run having walked past the node, is provenance and is not
    a state; carrying it as a second name is what left `done` and `completed`
    side by side in the browser with no reader able to tell them apart.

    The terminal names are the dispositions decision 0006 already settled, so a V3
    execution inherits them rather than renaming them. `interrupted` is the one
    name that decision does not carry: it is a durable, separately rendered ending
    today, and naming it truthfully is worth more than a vocabulary that matches a
    decision no execution has reached yet.
    """

    QUEUED = "queued"
    WORKING = "working"
    NEEDS_YOU = "needs_you"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


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


@dataclass(frozen=True)
class WaitingReconciliationProjection:
    intent: EffectIntentSnapshot
    pending_command: ReconcileCommandSnapshot | None


@dataclass(frozen=True)
class AgentAttemptCancellationProjection:
    command_id: str
    replacement: AgentAttemptReplacement
    redrive_state: AgentAttemptRedriveState
    disposition: AgentAttemptCancellationDisposition | None


@dataclass(frozen=True)
class AgentAttemptProjection:
    attempt_id: AgentAttemptId
    node_execution_id: NodeExecutionId
    request_hash: AgentExecutionRequestHash
    attempt_ordinal: int
    state: PublicAgentAttemptState
    failure_code: AgentAttemptFailureCode | None
    cancellation: AgentAttemptCancellationProjection | None = None


@dataclass(frozen=True)
class RunProjection:
    run: AnyRun
    graph: AnyWorkflowDocument
    reconciliation: WaitingReconciliationProjection | None
    agent_attempts: tuple[AgentAttemptProjection, ...] = ()

    @property
    def current_agent_attempt(self) -> AgentAttemptProjection | None:
        return self.agent_attempts[-1] if self.agent_attempts else None


@dataclass(frozen=True)
class RunPage:
    runs: tuple[RunProjection, ...]
    next_after: RunId | None
