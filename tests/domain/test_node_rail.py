"""The node rail, proven without a browser.

Every sentence the cockpit's own rail suite pins in TypeScript has its named
Python counterpart here, with `succeeded` where that suite still says `done` or
`completed`. What the browser could only assert through a rendered component is
asserted here against the derivation itself.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from atelier2.application.project_node_rail import (
    NodeRailAttempt,
    NodeRailEntry,
    NodeRailUnprojectable,
    project_node_rail,
)
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agents import (
    AgentBindingSet,
    AgentExecutionRequestHash,
)
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
    LogicalEffectKey,
    OperatorAuthoritativeAbsence,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.run_events import (
    PersistedRunEvent,
)
from atelier2.contracts.run_projections import (
    AgentAttemptProjection,
    NodeState,
    PublicAgentAttemptState,
    RunProjection,
    WaitingReconciliationProjection,
)
from atelier2.contracts.runs import Run, RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflows import (
    ActionNode,
    AgentNode,
    AgentNodeV2,
    SubworkflowNode,
    WaitNode,
    WorkflowGraph,
    WorkflowGraphV2,
)

RUN_ID = RunId("node-rail")
REVISION_HASH = WorkflowRevisionHash("a" * 64)
REQUEST_HASH = AgentExecutionRequestHash("1" * 64)
RECEIPT_KINDS = frozenset(
    {RunEventKind.ACTION_RECONCILIATION_RESOLVED, RunEventKind.ACTION_COMPLETED}
)
CANCELLATION_KINDS = frozenset(
    {
        RunEventKind.AGENT_CANCEL_REQUESTED,
        RunEventKind.AGENT_CANCELLED,
        RunEventKind.AGENT_INTERRUPTED,
    }
)
SETTLED_CANCELLATION_KINDS = CANCELLATION_KINDS - {RunEventKind.AGENT_CANCEL_REQUESTED}


def terminal_node() -> SubworkflowNode:
    return SubworkflowNode(
        id="final", type="subworkflow", operation="add", operands=(2, 3), next=None
    )


def v1_graph() -> WorkflowGraph:
    """agent -> action -> wait -> final: the walk the V1 cockpit renders."""
    return WorkflowGraph(
        format_version=1,
        start="agent",
        nodes=(
            AgentNode(
                id="agent",
                type="agent",
                job="Build it",
                output="candidate",
                next="action",
            ),
            ActionNode(id="action", type="action", next="wait"),
            WaitNode(id="wait", type="wait", answer_type="integer", next="final"),
            terminal_node(),
        ),
    )


def v2_graph() -> WorkflowGraphV2:
    """agent -> final: the shortest walk that still has a successor to mislead."""
    return WorkflowGraphV2(
        format_version=2,
        start="agent",
        nodes=(
            AgentNodeV2(
                id="agent", type="agent", role="builder", job="Build", next="final"
            ),
            terminal_node(),
        ),
    )


def durable_event(
    sequence: int,
    node_id: str,
    kind: RunEventKind,
    *,
    workflow_format_version: int = 1,
    attempt_ordinal: int | None = None,
) -> PersistedRunEvent:
    payload = b"1"
    carries_receipt = kind in RECEIPT_KINDS
    cancelling = kind in CANCELLATION_KINDS
    event = RunEvent(
        RUN_ID,
        REVISION_HASH,
        sequence,
        node_id,
        NodeExecutionId.for_node(RUN_ID, REVISION_HASH, node_id),
        kind,
        payload,
        receipt_logical_key=(
            LogicalEffectKey("node-rail/effect") if carries_receipt else None
        ),
        receipt_result_hash=Sha256Hash.of(payload) if carries_receipt else None,
        agent_attempt_id="b" * 64 if attempt_ordinal is not None else None,
        attempt_ordinal=attempt_ordinal,
        cancellation_command_id="cancel" if cancelling else None,
        replacement="NONE" if cancelling else None,
        cancellation_disposition=(
            "REAPED_AFTER_TERM" if kind in SETTLED_CANCELLATION_KINDS else None
        ),
    )
    return PersistedRunEvent(event, None, workflow_format_version)


def v2_agent_event(kind: RunEventKind, sequence: int = 1) -> PersistedRunEvent:
    return durable_event(
        sequence, "agent", kind, workflow_format_version=2, attempt_ordinal=1
    )


def waiting_reconciliation(*, accepted: bool) -> WaitingReconciliationProjection:
    """The Action node waiting on the operator, with or without their answer."""
    intent = EffectIntent(
        EffectBinding(
            LogicalEffectKey("node-rail/effect"),
            RUN_ID,
            REVISION_HASH,
            AdapterRevision("node-rail-adapter"),
            EffectDestination("node-rail-destination"),
            AdapterOperationalIdentity("node-rail-operation"),
        ),
        CanonicalRequest(b"request"),
    )
    command = ReconcileCommand(
        ReconcileCommandId("command"),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator"),
        "inspected the exact request",
        OperatorAuthoritativeAbsence(),
    )
    return WaitingReconciliationProjection(
        EffectIntentSnapshot(
            intent,
            EffectIntentState.WAITING_RECONCILIATION,
            EffectIntentStateVersion(1),
        ),
        (
            ReconcileCommandSnapshot(command, ReconcileCommandState.PENDING)
            if accepted
            else None
        ),
    )


def v1_projection(
    state: RunState,
    current_node_id: str,
    last_event_sequence: int,
    reconciliation: WaitingReconciliationProjection | None = None,
) -> RunProjection:
    return RunProjection(
        Run(
            RUN_ID,
            REVISION_HASH,
            state,
            current_node_id,
            0,
            last_event_sequence,
            Sha256Hash.of(b"terminal") if state is RunState.COMPLETED else None,
        ),
        v1_graph(),
        reconciliation,
    )


def v2_projection(
    attempts: tuple[AgentAttemptProjection, ...],
    last_event_sequence: int = 0,
) -> RunProjection:
    return RunProjection(
        RunV2(
            RUN_ID,
            REVISION_HASH,
            AgentBindingSet(()).binding_set_hash,
            (),
            RunState.STARTED,
            "agent",
            0,
            last_event_sequence,
        ),
        v2_graph(),
        None,
        attempts,
    )


def agent_attempt(
    ordinal: int, state: PublicAgentAttemptState
) -> AgentAttemptProjection:
    execution_id = NodeExecutionId.for_node(RUN_ID, REVISION_HASH, "agent")
    return AgentAttemptProjection(
        AgentAttemptId.for_execution(execution_id, REQUEST_HASH, ordinal),
        execution_id,
        REQUEST_HASH,
        ordinal,
        state,
        None,
    )


def v2_possibly_ran() -> RunProjection:
    """The one V2 snapshot whose attempt an event is free to overtake."""
    return v2_projection((agent_attempt(1, PublicAgentAttemptState.POSSIBLY_RAN),))


def state_names(rail: Sequence[NodeRailEntry]) -> list[str]:
    """The rail read as the operator's own words, the way the TS suite reads it."""
    return [entry.state.value for entry in rail]


@pytest.mark.parametrize(
    ("kind", "agent_state", "successor_state"),
    [
        (RunEventKind.AGENT_COMPLETED, "succeeded", "working"),
        (RunEventKind.AGENT_FAILED, "failed", "queued"),
        (RunEventKind.AGENT_CANCELLED, "cancelled", "queued"),
        (RunEventKind.AGENT_INTERRUPTED, "interrupted", "queued"),
    ],
)
def test_a_v2_agent_ending_is_named_without_inventing_successor_progress(
    kind: RunEventKind, agent_state: str, successor_state: str
) -> None:
    rail = project_node_rail(v2_possibly_ran(), (v2_agent_event(kind),))

    assert state_names(rail) == [agent_state, successor_state]


@pytest.mark.parametrize(
    ("kind", "settled_state"),
    [
        (RunEventKind.AGENT_CANCELLED, PublicAgentAttemptState.CANCELLED),
        (RunEventKind.AGENT_INTERRUPTED, PublicAgentAttemptState.INTERRUPTED),
    ],
)
def test_a_prepared_replacement_stays_working_after_terminal_cancellation_history(
    kind: RunEventKind, settled_state: PublicAgentAttemptState
) -> None:
    projection = v2_projection(
        (
            agent_attempt(1, settled_state),
            agent_attempt(2, PublicAgentAttemptState.PREPARED),
        ),
        last_event_sequence=1,
    )

    rail = project_node_rail(projection, (v2_agent_event(kind),))

    assert state_names(rail) == ["working", "queued"]


@pytest.mark.proves("the-node-state-derivation-is-provable-without-a-browser")
def test_the_rail_is_ordered_from_the_start_edge_and_names_every_durable_state() -> (
    None
):
    projection = v1_projection(RunState.WAITING_INPUT, "wait", 3)
    events = (
        durable_event(1, "agent", RunEventKind.AGENT_COMPLETED),
        durable_event(2, "action", RunEventKind.ACTION_COMPLETED),
        durable_event(3, "wait", RunEventKind.WAITING_INPUT),
    )

    rail = project_node_rail(projection, events)

    assert [entry.node_id for entry in rail] == ["agent", "action", "wait", "final"]
    assert state_names(rail) == ["succeeded", "succeeded", "needs_you", "queued"]
    assert [
        None if entry.last_event is None else entry.last_event.event.event_kind.value
        for entry in rail
    ] == ["AGENT_COMPLETED", "ACTION_COMPLETED", "WAITING_INPUT", None]


def test_an_accepted_reconciliation_reads_as_working_while_the_snapshot_waits() -> None:
    projection = v1_projection(
        RunState.WAITING_RECONCILIATION,
        "action",
        3,
        waiting_reconciliation(accepted=True),
    )

    rail = project_node_rail(
        projection, (durable_event(1, "agent", RunEventKind.AGENT_COMPLETED),)
    )

    assert rail[1].state is NodeState.WORKING


def test_a_reconciliation_nobody_answered_still_needs_the_operator() -> None:
    projection = v1_projection(
        RunState.WAITING_RECONCILIATION,
        "action",
        3,
        waiting_reconciliation(accepted=False),
    )

    rail = project_node_rail(projection, ())

    assert rail[1].state is NodeState.NEEDS_YOU


def test_the_snapshot_holds_until_events_lead_it_and_then_the_events_decide() -> None:
    projection = v1_projection(RunState.STARTED, "action", 1)
    agent_completed = durable_event(1, "agent", RunEventKind.AGENT_COMPLETED)
    reconciliation_required = durable_event(
        2, "action", RunEventKind.ACTION_RECONCILIATION_REQUIRED
    )
    action_completed = durable_event(3, "action", RunEventKind.ACTION_COMPLETED)

    caught_up = project_node_rail(projection, (agent_completed,))
    newly_waiting = project_node_rail(
        projection, (agent_completed, reconciliation_required)
    )
    advanced = project_node_rail(
        projection, (agent_completed, reconciliation_required, action_completed)
    )

    assert state_names(caught_up) == ["succeeded", "working", "queued", "queued"]
    assert state_names(newly_waiting) == ["succeeded", "needs_you", "queued", "queued"]
    assert state_names(advanced) == ["succeeded", "succeeded", "working", "queued"]


@pytest.mark.parametrize(
    ("attempt_state", "node_state"),
    [
        (PublicAgentAttemptState.FAILED, NodeState.FAILED),
        (PublicAgentAttemptState.CANCELLED, NodeState.CANCELLED),
        (PublicAgentAttemptState.INTERRUPTED, NodeState.INTERRUPTED),
        (PublicAgentAttemptState.PREPARED, NodeState.WORKING),
        (PublicAgentAttemptState.POSSIBLY_RAN, NodeState.WORKING),
        (PublicAgentAttemptState.CANCEL_REQUESTED, NodeState.WORKING),
    ],
)
def test_the_agent_node_of_a_snapshot_reads_its_own_current_attempt(
    attempt_state: PublicAgentAttemptState, node_state: NodeState
) -> None:
    projection = v2_projection((agent_attempt(1, attempt_state),))

    rail = project_node_rail(projection, ())

    assert rail[0].state is node_state


def test_a_completed_run_reads_every_node_as_succeeded() -> None:
    projection = v1_projection(RunState.COMPLETED, "final", 4)

    rail = project_node_rail(projection, ())

    assert state_names(rail) == ["succeeded"] * 4


def test_a_run_whose_current_node_is_absent_from_its_revision_is_refused() -> None:
    projection = v1_projection(RunState.STARTED, "invented", 0)

    with pytest.raises(NodeRailUnprojectable):
        project_node_rail(projection, ())


def test_a_leading_event_replaces_the_attempt_the_snapshot_still_shows() -> None:
    rail = project_node_rail(
        v2_possibly_ran(), (v2_agent_event(RunEventKind.AGENT_FAILED),)
    )

    assert rail[0].attempt == NodeRailAttempt(1, PublicAgentAttemptState.FAILED)


def test_a_succeeded_attempt_is_told_by_its_ordinal_alone() -> None:
    rail = project_node_rail(
        v2_possibly_ran(), (v2_agent_event(RunEventKind.AGENT_COMPLETED),)
    )

    assert rail[0].attempt == NodeRailAttempt(1, None)


def test_only_a_v2_agent_node_carries_an_attempt() -> None:
    v1_rail = project_node_rail(v1_projection(RunState.STARTED, "agent", 0), ())
    v2_rail = project_node_rail(
        v2_projection((agent_attempt(1, PublicAgentAttemptState.PREPARED),)), ()
    )

    assert [entry.attempt for entry in v1_rail] == [None, None, None, None]
    assert v2_rail[0].attempt == NodeRailAttempt(1, PublicAgentAttemptState.PREPARED)
    assert v2_rail[1].attempt is None
