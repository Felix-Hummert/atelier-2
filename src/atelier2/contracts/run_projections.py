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
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId
from atelier2.contracts.when import RecordedAt
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


class RunCancellationRefusal(StrEnum):
    """Why a V3 run cannot take a *new* operator run-cancel command right now.

    #439's D3 names five reasons a run cannot be cancelled. This is the closed
    set every layer shares: the store's own truth when it refuses a submitted
    command, and the API's `RunResourceV3.cancellation` predicate, which renders
    the full standing so the cockpit shows the operator sentence rather than
    guessing from the rail. It lives here, beside the run projection the API
    reads, because a refusal token the adapter writes and the API projects is a
    shared value with one owner -- not a shape the port seam invents. One token
    per reason, never two spellings of the same fact.
    """

    BETWEEN_NODES = "between-nodes"
    WAITING_FOR_YOU = "waiting-for-you"
    NODE_RUNS_NO_AGENT = "node-runs-no-agent"
    ALREADY_CANCELLING = "already-cancelling"
    ALREADY_ENDED = "already-ended"
    ANSWER_IN_FLIGHT = "answer-in-flight"
    """An accepted answer to this pause is still being applied (#668).

    Only the store can see it, and only for the moment between accepting an
    operator's message and folding it into the run -- so this token reaches a
    reader as a refused command, never as the cancellability predicate, which
    describes a standing rather than a moment. Ending the run here would drop a
    message the product already told a person it had taken.
    """


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
    started_at: RecordedAt | None = None
    ended_at: RecordedAt | None = None
    orders: tuple[RunInput, ...] = ()

    @property
    def current_agent_attempt(self) -> AgentAttemptProjection | None:
        return self.agent_attempts[-1] if self.agent_attempts else None


@dataclass(frozen=True)
class RunPage:
    runs: tuple[RunProjection, ...]
    next_after: RunId | None


@dataclass(frozen=True)
class NodeAnswer:
    """What one node wrote, as the run kept it.

    The bytes come from the node's own completion event, and the hash is the one
    that event stored -- so a reader is looking at the value the run recorded,
    not at a copy somebody made of it.
    """

    value: bytes
    value_hash: Sha256Hash


@dataclass(frozen=True)
class NodeProvenance:
    """Which agent, under which configuration, produced a node's answer.

    Every field is read from the receipt the attempt wrote. Usage is still
    absent: no receipt holds cost. Duration is recorded beside the attempt, not
    on this receipt.
    """

    role: str
    provider_id: str
    model: str
    executor_revision: str
    executor_operational_identity: str
    auth_mode: str
    profile_id: str
    agent_configuration_revision_hash: str
    request_hash: str
    receipt_hash: str


@dataclass(frozen=True)
class NodeDetail:
    """One node of a run, read the way an operator asks about it.

    Three questions and one refusal. **What was it asked** is the job the run
    really handed its provider, recomputed through the one composition owner and
    bound to the request hash the attempt stored. **What did it answer** is its
    completion payload with the hash the event kept. **Who did it** is the
    receipt's provenance. And **what is it waiting on** is the refusal that stops
    the run here, when one stops it.

    Each of the three may be absent, and the absence is the answer: a node that
    has not run yet was asked nothing this reader can prove, wrote nothing, and
    has no receipt. A refusal exists only where something really refuses.
    """

    run_id: RunId
    node_id: str
    state: NodeState
    job: bytes | None
    job_hash: str | None
    answer: NodeAnswer | None
    provenance: NodeProvenance | None
    refusal: str | None
    started_at: RecordedAt | None = None
    ended_at: RecordedAt | None = None
