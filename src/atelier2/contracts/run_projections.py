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
from atelier2.contracts.agent_transcripts import AttemptTranscript
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.effects import (
    EffectIntentSnapshot,
    EffectIntentState,
    ReconcileCommandSnapshot,
)
from atelier2.contracts.executions import NodeExecutionId, logical_effect_key_for
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, RunState
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
    """What a reader of a run is told about that run's current agent attempt.

    `SUCCEEDED` is the one name a run says only while it stands still on the
    effect its finished node asks the operator to reconcile (decision 0010).
    Everywhere else the success has already moved the run on, and the reader is
    told about the successor instead.
    """

    PREPARED = "PREPARED"
    POSSIBLY_RAN = "POSSIBLY_RAN"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    FAILED = "FAILED"
    SUCCEEDED = "SUCCEEDED"


_PUBLIC_ATTEMPT_STATES: Mapping[AgentAttemptState, PublicAgentAttemptState] = {
    AgentAttemptState.PREPARED: PublicAgentAttemptState.PREPARED,
    AgentAttemptState.LAUNCH_ARMED: PublicAgentAttemptState.POSSIBLY_RAN,
    AgentAttemptState.CANCEL_REQUESTED: PublicAgentAttemptState.CANCEL_REQUESTED,
    AgentAttemptState.CANCELLED: PublicAgentAttemptState.CANCELLED,
    AgentAttemptState.INTERRUPTED: PublicAgentAttemptState.INTERRUPTED,
    AgentAttemptState.FAILED: PublicAgentAttemptState.FAILED,
}


def public_agent_attempt_state(
    durable_state: AgentAttemptState,
    *,
    effect_awaits_reconciliation: bool,
) -> PublicAgentAttemptState | None:
    """The public name of one durable attempt state, or None where a run has none.

    A succeeded attempt is normally not the current one: the transition that
    records the success also moves the run past it, so a reader is told about its
    successor. A node whose own effect the operator still has to reconcile is the
    exception the push and open-pr grants create: the agent is done, the run
    parks on that same node, and the reader is told `SUCCEEDED` beside the run's
    `WAITING_RECONCILIATION`. Without that standing the success has no successor
    to speak of, and naming it would tell the reader a run stands where no
    transition ever left it.
    """
    if durable_state is AgentAttemptState.SUCCEEDED:
        return (
            PublicAgentAttemptState.SUCCEEDED if effect_awaits_reconciliation else None
        )
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


_INTENT_STATES_OWED_THE_OPERATOR = frozenset(
    {EffectIntentState.WAITING_RECONCILIATION, EffectIntentState.RECONCILING}
)
"""The two standings of an intent that is still the operator's move to make.

`RECONCILING` is the same standing with the operator's word already submitted
and not yet applied; a reader loses sight of the run if that moment reads
differently from the one before it.
"""


def execution_awaits_effect_reconciliation(
    run_state: RunState,
    reconciliation: WaitingReconciliationProjection | None,
    node_execution_id: NodeExecutionId,
) -> bool:
    """Whether this exact node execution is the one the run stands still on.

    The run's own standing is not enough: a line whose later node parks still
    holds the finished attempts of every node before it, and those successes
    were left behind by transitions that did happen. Only the execution the
    waiting intent was keyed on may be told as `SUCCEEDED`.
    """
    return (
        run_state is RunState.WAITING_RECONCILIATION
        and reconciliation is not None
        and reconciliation.intent.state in _INTENT_STATES_OWED_THE_OPERATOR
        and reconciliation.intent.intent.binding.logical_key
        == logical_effect_key_for(node_execution_id)
    )


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
class RunForkOriginProjection:
    origin_run_id: RunId
    terminal_hash: Sha256Hash
    restart_from_node_id: str
    fork_hash: Sha256Hash


@dataclass(frozen=True)
class RunForkSuccessorProjection:
    successor_run_id: RunId
    restart_from_node_id: str
    fork_hash: Sha256Hash


@dataclass(frozen=True)
class ReusedNodeProjection:
    node_id: str
    source_run_id: RunId
    source_event_hash: Sha256Hash
    source_receipt_hash: Sha256Hash
    source_declared_context_package_hash: Sha256Hash


@dataclass(frozen=True)
class RunProjection:
    run: AnyRun
    graph: AnyWorkflowDocument
    reconciliation: WaitingReconciliationProjection | None
    agent_attempts: tuple[AgentAttemptProjection, ...] = ()
    started_at: RecordedAt | None = None
    ended_at: RecordedAt | None = None
    orders: tuple[RunInput, ...] = ()
    fork_origin: RunForkOriginProjection | None = None
    fork_successors: tuple[RunForkSuccessorProjection, ...] = ()
    reused_nodes: tuple[ReusedNodeProjection, ...] = ()

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

    `refusal_output` is deliberately its own field, not `answer`: `answer` is
    the value the run accepted, and a schema-refused value never was that. It
    carries a redacted presentation of the bytes a schema owner judged and
    refused, read back from the content-addressed artifact the failure
    transaction kept under the receipt's own value hash (#664) -- present only
    where a node's own output was judged and something to keep survived that
    judgment: a run from before that write existed, a refusal with no receipt
    at all (an unavailable executor, a predecessor that has not written),
    judged bytes that were themselves empty, or bytes that do not decode as
    UTF-8 (unscannable for a credential shape, so never shown unredacted) all
    carry this field as honestly absent, never a guess.

    `value` here is not `value_hash`'s exact preimage, on purpose: the caller
    that builds this `NodeAnswer` runs `contracts.secret_redaction` over the
    judged bytes first, so `value` is what is safe to show and `value_hash` is
    still the receipt's own hash of the original, unredacted bytes -- the hash
    proves what was judged, the value is its redacted presentation.

    `transcript` is the decoded, already-redacted steps of the current
    execution's highest attempt that named one. Its v2 events identify the
    recording moment and origin; v1 events explicitly predate transcript
    moments. A null pointer is honest absence; a named address that cannot be
    read is not this field's job to soften.
    """

    run_id: RunId
    node_id: str
    state: NodeState
    job: bytes | None
    job_hash: str | None
    answer: NodeAnswer | None
    provenance: NodeProvenance | None
    refusal: str | None
    refusal_output: NodeAnswer | None = None
    started_at: RecordedAt | None = None
    ended_at: RecordedAt | None = None
    transcript: AttemptTranscript | None = None
