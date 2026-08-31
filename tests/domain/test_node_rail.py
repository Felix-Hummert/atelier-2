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
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptReplacement,
)
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
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventCancellationBinding,
    RunEventKind,
    WaitAnswerActor,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
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
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflows_v3 import (
    ActionNodeV3,
    AgentNodeV3,
    LoopDeclaration,
    NodeOutput,
    VersionedReference,
    WaitNodeV3,
    WorkflowGraphV3,
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


def durable_event(
    sequence: int,
    node_id: str,
    kind: RunEventKind,
    *,
    workflow_format_version: WorkflowFormatVersion = WorkflowFormatVersion.V3,
    attempt_ordinal: int | None = None,
    round_ordinal: int = 1,
) -> PersistedRunEvent:
    payload = b"1"
    carries_receipt = kind in RECEIPT_KINDS
    cancelling = kind in CANCELLATION_KINDS
    attempt_binding = None
    if attempt_ordinal is not None:
        attempt_id = AgentAttemptId("b" * 64)
        attempt_binding = (
            RunEventCancellationBinding(
                attempt_id,
                attempt_ordinal,
                AgentAttemptReplacement.NONE,
                "cancel",
                (
                    AgentAttemptCancellationDisposition.REAPED_AFTER_TERM
                    if kind in SETTLED_CANCELLATION_KINDS
                    else None
                ),
            )
            if cancelling
            else RunEventAgentAttemptBinding(attempt_id, attempt_ordinal)
        )
    event = RunEvent(
        RUN_ID,
        REVISION_HASH,
        sequence,
        node_id,
        NodeExecutionId.for_node(RUN_ID, REVISION_HASH, node_id, round_ordinal),
        kind,
        payload,
        receipt_logical_key=(
            LogicalEffectKey("node-rail/effect") if carries_receipt else None
        ),
        receipt_result_hash=Sha256Hash.of(payload) if carries_receipt else None,
        attempt_binding=attempt_binding,
        round_ordinal=round_ordinal,
        wait_answer_actor=(
            WaitAnswerActor.OPERATOR if kind is RunEventKind.WAITING_INPUT else None
        ),
    )
    return PersistedRunEvent(
        event,
        None,
        workflow_format_version,
        wait_answer_actor=(
            WaitAnswerActor.OPERATOR
            if workflow_format_version is WorkflowFormatVersion.V3
            and kind is RunEventKind.WAIT_ANSWERED
            else None
        ),
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


def v3_full_line_graph() -> WorkflowGraphV3:
    """agent -> action -> wait -> final: every node kind the rail names."""
    return WorkflowGraphV3(
        format_version=3,
        name="Every node kind the rail names, one after another",
        nodes=(
            AgentNodeV3(
                id="agent",
                type="agent",
                role="builder",
                mode="headless",
                instruction="Build it",
            ),
            ActionNodeV3(
                id="action",
                type="action",
                operation=VersionedReference(ref="op", revision="e" * 64),
                depends_on=("agent",),
            ),
            WaitNodeV3(
                id="wait",
                type="wait",
                prompt="Approve it",
                depends_on=("action",),
                outputs=(
                    NodeOutput(
                        name="answer",
                        schema=VersionedReference(
                            ref="answer-schema", revision="d" * 64
                        ),
                    ),
                ),
            ),
            AgentNodeV3(
                id="final",
                type="agent",
                role="builder",
                mode="headless",
                instruction="Close it out",
                depends_on=("wait",),
            ),
        ),
    )


def v3_full_line_projection(
    state: RunState,
    current_node_id: str,
    last_event_sequence: int,
    reconciliation: WaitingReconciliationProjection | None = None,
    attempts: tuple[AgentAttemptProjection, ...] = (),
) -> RunProjection:
    return RunProjection(
        RunV3(
            RUN_ID,
            REVISION_HASH,
            AgentBindingSet(()).binding_set_hash,
            (),
            state,
            current_node_id,
            0,
            last_event_sequence,
            RunConfigurationRevisionHash("c" * 64),
            Sha256Hash.of(b"terminal") if state is RunState.COMPLETED else None,
        ),
        v3_full_line_graph(),
        reconciliation,
        attempts,
    )


def agent_attempt(
    ordinal: int, state: PublicAgentAttemptState, node_id: str = "agent"
) -> AgentAttemptProjection:
    execution_id = NodeExecutionId.for_node(RUN_ID, REVISION_HASH, node_id)
    return AgentAttemptProjection(
        AgentAttemptId.for_execution(execution_id, REQUEST_HASH, ordinal),
        execution_id,
        REQUEST_HASH,
        ordinal,
        state,
        None,
    )


def state_names(rail: Sequence[NodeRailEntry]) -> list[str]:
    """The rail read as the operator's own words, the way the TS suite reads it."""
    return [entry.state.value for entry in rail]


def test_a_prepared_replacement_stays_working_after_terminal_cancellation_history() -> (
    None
):
    projection = v3_full_line_projection(
        RunState.STARTED,
        "agent",
        1,
        attempts=(
            agent_attempt(1, PublicAgentAttemptState.CANCELLED),
            agent_attempt(2, PublicAgentAttemptState.PREPARED),
        ),
    )

    rail = project_node_rail(
        projection,
        (durable_event(1, "agent", RunEventKind.AGENT_CANCELLED, attempt_ordinal=1),),
    )

    assert state_names(rail) == ["working", "queued", "queued", "queued"]


@pytest.mark.proves("the-node-state-derivation-is-provable-without-a-browser")
def test_the_rail_is_ordered_from_the_start_edge_and_names_every_durable_state() -> (
    None
):
    projection = v3_full_line_projection(RunState.WAITING_INPUT, "wait", 3)
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
    projection = v3_full_line_projection(
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
    projection = v3_full_line_projection(
        RunState.WAITING_RECONCILIATION,
        "action",
        3,
        waiting_reconciliation(accepted=False),
    )

    rail = project_node_rail(projection, ())

    assert rail[1].state is NodeState.NEEDS_YOU


def test_the_snapshot_holds_until_events_lead_it_and_then_the_events_decide() -> None:
    projection = v3_full_line_projection(RunState.STARTED, "action", 1)
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


def test_a_completed_run_reads_every_node_as_succeeded() -> None:
    projection = v3_full_line_projection(RunState.COMPLETED, "final", 4)

    rail = project_node_rail(projection, ())

    assert state_names(rail) == ["succeeded"] * 4


def test_a_failed_run_snapshot_without_events_reads_the_current_node_as_failed() -> (
    None
):
    rail = project_node_rail(v3_failed_projection(), ())

    assert tuple((entry.node_id, entry.state, entry.attempt) for entry in rail) == (
        ("implement", NodeState.FAILED, None),
        ("review", NodeState.QUEUED, None),
    )


def test_a_failed_snapshot_and_its_failure_event_name_the_same_rail() -> None:
    projection = v3_failed_projection(
        (agent_attempt(1, PublicAgentAttemptState.FAILED, "implement"),)
    )
    event = v3_agent_event(RunEventKind.AGENT_FAILED)

    listed = project_node_rail(projection, ())
    streamed = project_node_rail(projection, (event,))

    # last_event is provenance the wire does not carry; the doors name state
    # and attempt.
    assert [(entry.node_id, entry.state, entry.attempt) for entry in listed] == [
        (entry.node_id, entry.state, entry.attempt) for entry in streamed
    ]
    assert listed[0].state is NodeState.FAILED
    assert listed[0].attempt == NodeRailAttempt(1, PublicAgentAttemptState.FAILED)


def test_a_run_whose_current_node_is_absent_from_its_revision_is_refused() -> None:
    projection = v3_full_line_projection(RunState.STARTED, "invented", 0)

    with pytest.raises(NodeRailUnprojectable):
        project_node_rail(projection, ())


def v3_graph() -> WorkflowGraphV3:
    """implement -> review: the line the V3 driver actually walks."""
    return WorkflowGraphV3(
        format_version=3,
        name="Two agents in a line",
        nodes=(
            AgentNodeV3(
                id="implement",
                type="agent",
                role="builder",
                mode="headless",
                instruction="Do the one thing this chain is for.",
            ),
            AgentNodeV3(
                id="review",
                type="agent",
                role="builder",
                mode="headless",
                instruction="Check what the node before you did.",
                depends_on=("implement",),
            ),
        ),
    )


def v3_wait_loop_graph() -> WorkflowGraphV3:
    """wait -> implement -> wait: the smallest reachable round transition."""
    return WorkflowGraphV3(
        format_version=3,
        name="A person and an agent take another round",
        nodes=(
            WaitNodeV3(
                id="wait",
                type="wait",
                prompt="What should this round do?",
                outputs=(
                    NodeOutput(
                        name="answer",
                        schema=VersionedReference(
                            ref="answer-schema", revision="d" * 64
                        ),
                    ),
                ),
            ),
            AgentNodeV3(
                id="implement",
                type="agent",
                role="builder",
                mode="headless",
                instruction="Do what the person asked this round.",
                depends_on=("wait",),
            ),
        ),
        loops=(
            LoopDeclaration(
                id="conversation",
                body=("wait", "implement"),
                maximum_rounds=2,
            ),
        ),
    )


def v3_projection(
    current_node_id: str,
    last_event_sequence: int,
    attempts: tuple[AgentAttemptProjection, ...] = (),
    current_round_ordinal: int = 1,
    graph: WorkflowGraphV3 | None = None,
) -> RunProjection:
    return RunProjection(
        RunV3(
            RUN_ID,
            REVISION_HASH,
            AgentBindingSet(()).binding_set_hash,
            (),
            RunState.STARTED,
            current_node_id,
            0,
            last_event_sequence,
            RunConfigurationRevisionHash("c" * 64),
            current_round_ordinal=current_round_ordinal,
        ),
        v3_graph() if graph is None else graph,
        None,
        attempts,
    )


def v3_failed_projection(
    attempts: tuple[AgentAttemptProjection, ...] = (),
) -> RunProjection:
    return RunProjection(
        RunV3(
            RUN_ID,
            REVISION_HASH,
            AgentBindingSet(()).binding_set_hash,
            (),
            RunState.FAILED,
            "implement",
            1,
            1,
            RunConfigurationRevisionHash("c" * 64),
            Sha256Hash.of(b"terminal"),
        ),
        v3_graph(),
        None,
        attempts,
    )


def v3_agent_event(
    kind: RunEventKind,
    sequence: int = 1,
    node_id: str = "implement",
    attempt_ordinal: int | None = 1,
) -> PersistedRunEvent:
    return durable_event(
        sequence,
        node_id,
        kind,
        workflow_format_version=WorkflowFormatVersion.V3,
        attempt_ordinal=attempt_ordinal,
    )


def v3_possibly_ran() -> RunProjection:
    return v3_projection(
        "implement",
        0,
        (agent_attempt(1, PublicAgentAttemptState.POSSIBLY_RAN, "implement"),),
    )


def test_a_v3_line_shows_the_node_that_finished_and_the_one_now_running() -> None:
    """The rail of a V3 run walks the edge its author declared.

    A V3 run used to have no rail at all: the walk answered with nothing, because
    the only startable shape was a single node and the order a longer graph takes
    was the ready set's to decide. A line of agent nodes starts and runs now, and
    the order it takes is not open -- it is the same edge `durable_node` follows
    from entry to sink, so answering with it invents nothing.
    """
    rail = project_node_rail(
        v3_projection("review", 1),
        (
            durable_event(
                1, "implement", RunEventKind.AGENT_COMPLETED, attempt_ordinal=1
            ),
        ),
    )

    assert tuple((entry.node_id, entry.state) for entry in rail) == (
        ("implement", NodeState.SUCCEEDED),
        ("review", NodeState.WORKING),
    )


def test_an_overtaking_event_fences_the_whole_rail_to_its_one_round() -> None:
    projection = v3_projection(
        "implement",
        2,
        current_round_ordinal=1,
        graph=v3_wait_loop_graph(),
    )
    events = (
        durable_event(
            1,
            "wait",
            RunEventKind.WAITING_INPUT,
            workflow_format_version=WorkflowFormatVersion.V3,
            round_ordinal=1,
        ),
        durable_event(
            2,
            "wait",
            RunEventKind.WAIT_ANSWERED,
            workflow_format_version=WorkflowFormatVersion.V3,
            round_ordinal=1,
        ),
        durable_event(
            3,
            "implement",
            RunEventKind.AGENT_COMPLETED,
            workflow_format_version=WorkflowFormatVersion.V3,
            attempt_ordinal=1,
            round_ordinal=1,
        ),
    )

    rail = project_node_rail(projection, events)

    assert [(entry.node_id, entry.state) for entry in rail] == [
        ("wait", NodeState.WORKING),
        ("implement", NodeState.SUCCEEDED),
    ]
    assert rail[0].last_event is None
    predecessor = rail[1].last_event
    assert predecessor is not None
    assert predecessor is events[-1]
    assert predecessor.event.node_execution_id == NodeExecutionId.for_node(
        RUN_ID, REVISION_HASH, "implement", 1
    )


def test_a_leading_event_for_no_graph_node_fails_loud() -> None:
    projection = v3_projection("implement", 0)
    unknown = durable_event(
        1,
        "removed-node",
        RunEventKind.AGENT_COMPLETED,
        workflow_format_version=WorkflowFormatVersion.V3,
        attempt_ordinal=1,
    )

    with pytest.raises(NodeRailUnprojectable, match="leading event node"):
        project_node_rail(projection, (unknown,))


def test_a_leading_node_with_the_wrong_execution_identity_fails_loud() -> None:
    projection = v3_projection("implement", 0)
    corrupted = durable_event(
        1,
        "implement",
        RunEventKind.AGENT_COMPLETED,
        workflow_format_version=WorkflowFormatVersion.V3,
        attempt_ordinal=1,
    )
    object.__setattr__(corrupted.event, "node_execution_id", NodeExecutionId("f" * 64))

    with pytest.raises(NodeRailUnprojectable, match="exact execution evidence"):
        project_node_rail(projection, (corrupted,))


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
def test_the_agent_node_of_a_v3_snapshot_reads_its_own_current_attempt(
    attempt_state: PublicAgentAttemptState, node_state: NodeState
) -> None:
    projection = v3_projection(
        "implement", 0, (agent_attempt(1, attempt_state, "implement"),)
    )

    rail = project_node_rail(projection, ())

    assert rail[0].state is node_state
    assert rail[0].attempt == NodeRailAttempt(1, attempt_state)
    assert rail[1].state is NodeState.QUEUED
    assert rail[1].attempt is None


@pytest.mark.parametrize(
    ("kind", "attempt_state"),
    [
        (RunEventKind.AGENT_FAILED, PublicAgentAttemptState.FAILED),
        (RunEventKind.AGENT_CANCELLED, PublicAgentAttemptState.CANCELLED),
        (RunEventKind.AGENT_INTERRUPTED, PublicAgentAttemptState.INTERRUPTED),
        (RunEventKind.AGENT_CANCEL_REQUESTED, PublicAgentAttemptState.CANCEL_REQUESTED),
        (RunEventKind.AGENT_COMPLETED, None),
    ],
)
def test_a_format_3_event_proves_the_attempt_a_v3_agent_just_ended(
    kind: RunEventKind, attempt_state: PublicAgentAttemptState | None
) -> None:
    rail = project_node_rail(v3_possibly_ran(), (v3_agent_event(kind),))

    assert rail[0].attempt == NodeRailAttempt(1, attempt_state)


def test_a_completed_format_3_event_still_names_the_attempt_the_run_walked_past() -> (
    None
):
    rail = project_node_rail(
        v3_projection("review", 1), (v3_agent_event(RunEventKind.AGENT_COMPLETED),)
    )

    assert rail[0].attempt == NodeRailAttempt(1, None)
    assert rail[1].attempt is None


@pytest.mark.parametrize(
    ("kind", "agent_state", "successor_state"),
    [
        (RunEventKind.AGENT_COMPLETED, "succeeded", "working"),
        (RunEventKind.AGENT_FAILED, "failed", "queued"),
        (RunEventKind.AGENT_CANCELLED, "cancelled", "queued"),
        (RunEventKind.AGENT_INTERRUPTED, "interrupted", "queued"),
    ],
)
def test_a_v3_agent_ending_is_named_without_inventing_successor_progress(
    kind: RunEventKind, agent_state: str, successor_state: str
) -> None:
    rail = project_node_rail(v3_possibly_ran(), (v3_agent_event(kind),))

    assert [entry.node_id for entry in rail] == ["implement", "review"]
    assert state_names(rail) == [agent_state, successor_state]


def test_a_format_1_event_does_not_invent_a_v3_attempt() -> None:
    """A historical format-1 event tags no attempt; nothing invents one for it.

    `WorkflowFormatVersion.V1` stays a named member for the durable layer's
    historical rows even though no document may declare it anymore (#901 slice
    5), so an event still carrying that historical tag must not be misread as
    the V3 attempt vocabulary.
    """
    rail = project_node_rail(
        v3_possibly_ran(),
        (
            durable_event(
                1,
                "implement",
                RunEventKind.AGENT_FAILED,
                workflow_format_version=WorkflowFormatVersion.V1,
                attempt_ordinal=1,
            ),
        ),
    )

    assert rail[0].attempt == NodeRailAttempt(1, PublicAgentAttemptState.POSSIBLY_RAN)
