"""Deriving the state of every node of one run from durable facts alone.

This is the machine the browser used to be the only owner of. It reads three
durable things — the run snapshot, the workflow revision that snapshot is bound
to, and the events recorded since — and answers with one state per node, in the
order the graph walks them.

Two of those three arrive together in `RunProjection`, because a snapshot without
the revision it walks cannot place a node at all. The fourth input the browser
also folded in here does *not* appear: which form the operator currently has open
is a fact about a window, not about a run. Two windows may honestly disagree, so
that overlay stays in the browser and is applied after this rail, never inside it.

The snapshot is authoritative until an event overtakes it. Once one does, the
events decide alone: a replayed page that has not yet caught up must not drag a
node backwards, and a page that has run ahead must not be held back by a
snapshot that was read before it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from atelier2.contracts.agent_attempts import AgentAttemptState
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.run_bindings import RunV2, RunV3
from atelier2.contracts.run_events import (
    PersistedRunEvent,
)
from atelier2.contracts.run_projections import (
    NodeState,
    PublicAgentAttemptState,
    RunProjection,
    public_agent_attempt_state,
)
from atelier2.contracts.runs import RunState
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflows import (
    AgentNode,
    AgentNodeV2,
    AnyWorkflowNode,
)
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    AnyWorkflowDocument,
    WorkflowGraphV3,
    WorkflowNodeV3,
    is_sink_node,
    linear_successor_id,
)

type _RailNode = AnyWorkflowNode | WorkflowNodeV3
"""Every node a rail can stand on, across the formats a run may be bound to.

The rail is the one place that walks nodes without caring which format authored
them, so the union it walks lives here rather than in the contracts: a shared
alias with one consumer would claim a generality nothing else asked for.
"""


_NODE_STATES_ENDED_BY_EVENT: Mapping[RunEventKind, NodeState] = {
    RunEventKind.AGENT_COMPLETED: NodeState.SUCCEEDED,
    RunEventKind.ACTION_COMPLETED: NodeState.SUCCEEDED,
    RunEventKind.WAIT_ANSWERED: NodeState.SUCCEEDED,
    RunEventKind.SUBWORKFLOW_COMPLETED: NodeState.SUCCEEDED,
    RunEventKind.AGENT_FAILED: NodeState.FAILED,
    RunEventKind.AGENT_CANCELLED: NodeState.CANCELLED,
    RunEventKind.AGENT_INTERRUPTED: NodeState.INTERRUPTED,
}
_SUCCESSFUL_ENDING_KINDS = frozenset(
    kind
    for kind, state in _NODE_STATES_ENDED_BY_EVENT.items()
    if state is NodeState.SUCCEEDED
)
_OPERATOR_OWED_KINDS = frozenset(
    {RunEventKind.ACTION_RECONCILIATION_REQUIRED, RunEventKind.WAITING_INPUT}
)
_NODE_STATES_ENDED_BY_ATTEMPT: Mapping[PublicAgentAttemptState, NodeState] = {
    PublicAgentAttemptState.FAILED: NodeState.FAILED,
    PublicAgentAttemptState.CANCELLED: NodeState.CANCELLED,
    PublicAgentAttemptState.INTERRUPTED: NodeState.INTERRUPTED,
}
_ATTEMPT_STATES_PROVEN_BY_EVENT: Mapping[RunEventKind, AgentAttemptState] = {
    RunEventKind.AGENT_COMPLETED: AgentAttemptState.SUCCEEDED,
    RunEventKind.AGENT_FAILED: AgentAttemptState.FAILED,
    RunEventKind.AGENT_CANCEL_REQUESTED: AgentAttemptState.CANCEL_REQUESTED,
    RunEventKind.AGENT_CANCELLED: AgentAttemptState.CANCELLED,
    RunEventKind.AGENT_INTERRUPTED: AgentAttemptState.INTERRUPTED,
}


class NodeRailUnprojectable(RuntimeError):
    """A run and the revision it is bound to disagree about where the run stands."""


@dataclass(frozen=True)
class NodeRailAttempt:
    """The agent attempt one node is currently telling a reader about.

    A succeeded attempt carries no state: the public vocabulary has no word for
    success, because the transition that records it also moves the run past the
    attempt. The node's own `succeeded` is what says the work is done.
    """

    ordinal: int
    state: PublicAgentAttemptState | None


@dataclass(frozen=True)
class NodeRailEntry:
    node_id: str
    state: NodeState
    last_event: PersistedRunEvent | None
    attempt: NodeRailAttempt | None


def project_node_rail(
    projection: RunProjection, events: Sequence[PersistedRunEvent]
) -> tuple[NodeRailEntry, ...]:
    """The state of every node of one run, in the order its graph walks them."""
    derivation = _RailDerivation.of(projection, events)
    return tuple(derivation.entry(index) for index in range(len(derivation.nodes)))


@dataclass(frozen=True)
class _RailDerivation:
    """What the whole rail shares, measured once instead of per node."""

    projection: RunProjection
    nodes: tuple[_RailNode, ...]
    current_index: int
    events: tuple[PersistedRunEvent, ...]
    leading_event: PersistedRunEvent | None
    """The newest event, when it has overtaken the snapshot; otherwise nothing."""

    @classmethod
    def of(
        cls, projection: RunProjection, events: Sequence[PersistedRunEvent]
    ) -> _RailDerivation:
        nodes = _walk_from_start(projection.graph)
        current_index = next(
            (
                index
                for index, node in enumerate(nodes)
                if node.id == projection.run.current_node_id
            ),
            None,
        )
        if current_index is None:
            raise NodeRailUnprojectable(
                "the durable run current node is absent from its workflow revision"
            )
        recorded = tuple(events)
        newest = recorded[-1] if recorded else None
        leads = (
            newest is not None
            and newest.event.event_sequence > projection.run.last_event_sequence
        )
        return cls(
            projection, nodes, current_index, recorded, newest if leads else None
        )

    def entry(self, index: int) -> NodeRailEntry:
        node = self.nodes[index]
        last_event = self._last_event_of(node.id)
        leading_event = self.leading_event
        state = (
            self._event_driven_state(node, last_event, leading_event)
            if leading_event is not None
            else self._snapshot_state(node, index, last_event)
        )
        return NodeRailEntry(
            node.id, state, last_event, self._attempt(node, last_event)
        )

    def _last_event_of(self, node_id: str) -> PersistedRunEvent | None:
        for persisted in reversed(self.events):
            if persisted.event.node_id == node_id:
                return persisted
        return None

    def _snapshot_state(
        self, node: _RailNode, index: int, last_event: PersistedRunEvent | None
    ) -> NodeState:
        run = self.projection.run
        attempt = self.projection.current_agent_attempt
        if (
            index == self.current_index
            and isinstance(run, RunV2 | RunV3)
            and _is_agent(node)
            and attempt is not None
        ):
            return _NODE_STATES_ENDED_BY_ATTEMPT.get(attempt.state, NodeState.WORKING)
        ended = _state_the_event_ended_in(last_event)
        if ended is not None:
            return ended
        if index < self.current_index:
            return NodeState.SUCCEEDED
        if index > self.current_index:
            return NodeState.QUEUED
        if run.state is RunState.COMPLETED:
            return NodeState.SUCCEEDED
        if run.state is RunState.WAITING_INPUT:
            return NodeState.NEEDS_YOU
        if run.state is RunState.WAITING_RECONCILIATION:
            return self._reconciliation_state()
        return NodeState.WORKING

    def _reconciliation_state(self) -> NodeState:
        """An accepted command is the operator's move already made, not one owed."""
        reconciliation = self.projection.reconciliation
        answered = (
            reconciliation is not None and reconciliation.pending_command is not None
        )
        return NodeState.WORKING if answered else NodeState.NEEDS_YOU

    def _event_driven_state(
        self,
        node: _RailNode,
        last_event: PersistedRunEvent | None,
        leading_event: PersistedRunEvent,
    ) -> NodeState:
        ended = _state_the_event_ended_in(last_event)
        if ended is not None:
            return ended
        if leading_event.event.node_id == node.id:
            return (
                NodeState.NEEDS_YOU
                if leading_event.event.event_kind in _OPERATOR_OWED_KINDS
                else NodeState.WORKING
            )
        if (
            leading_event.event.event_kind in _SUCCESSFUL_ENDING_KINDS
            and self._successor_of(leading_event.event.node_id) == node.id
        ):
            return NodeState.WORKING
        return NodeState.QUEUED

    def _successor_of(self, node_id: str) -> str | None:
        """The node the walk reaches next, whichever format declared that edge.

        Asked of the walk rather than of the node: a V1 or V2 node names its
        successor in `next` and a V3 node does not name one at all, but the walk
        has already resolved both into one order, so reading it here keeps a
        single owner of what follows what.
        """
        for index, node in enumerate(self.nodes):
            if node.id == node_id:
                following = self.nodes[index + 1 :]
                return following[0].id if following else None
        return None

    def _attempt(
        self, node: _RailNode, last_event: PersistedRunEvent | None
    ) -> NodeRailAttempt | None:
        run = self.projection.run
        if not isinstance(run, RunV2 | RunV3) or not _is_agent(node):
            return None
        from_event = _attempt_the_event_proves(last_event)
        from_snapshot = (
            self.projection.current_agent_attempt
            if node.id == run.current_node_id
            else None
        )
        if (
            self.leading_event is not None
            and from_event is not None
            and (
                from_snapshot is None
                or from_event.ordinal >= from_snapshot.attempt_ordinal
            )
        ):
            return from_event
        if from_snapshot is None:
            return from_event
        return NodeRailAttempt(from_snapshot.attempt_ordinal, from_snapshot.state)


def _walk_from_start(graph: AnyWorkflowDocument) -> tuple[_RailNode, ...]:
    """Every node in graph order; the graph's own validator proved the walk ends.

    A V3 graph is walked along the one edge its author declared: entry, then each
    node's single dependent, to the sink. That is not an order chosen here -- it
    is the same edge `durable_node` follows when it hands a finished node on, so
    the rail describes the run's real path rather than inventing one. Only a line
    can start, because a graph whose node has several dependents is refused before
    a run exists: choosing between them is the ready set (#86), and no rail is
    asked to decide it.
    """
    if isinstance(graph, WorkflowGraphV3):
        walked_v3: list[_RailNode] = []
        node_id = graph.entry_node_ids[0]
        while True:
            walked_v3.append(graph.node(node_id))
            if is_sink_node(graph, node_id):
                return tuple(walked_v3)
            node_id = linear_successor_id(graph, node_id)
    walked: list[_RailNode] = []
    node: _RailNode = graph.node(graph.start)
    while node.next is not None:
        walked.append(node)
        node = graph.node(node.next)
    walked.append(node)
    return tuple(walked)


def _is_agent(node: _RailNode) -> bool:
    return isinstance(node, AgentNode | AgentNodeV2 | AgentNodeV3)


def _state_the_event_ended_in(
    persisted: PersistedRunEvent | None,
) -> NodeState | None:
    """The ending one event proves, or nothing where it proves the node still runs."""
    if persisted is None:
        return None
    return _NODE_STATES_ENDED_BY_EVENT.get(persisted.event.event_kind)


def _attempt_the_event_proves(
    persisted: PersistedRunEvent | None,
) -> NodeRailAttempt | None:
    if persisted is None or persisted.workflow_format_version not in (
        WorkflowFormatVersion.V2,
        WorkflowFormatVersion.V3,
    ):
        return None
    ordinal = persisted.event.attempt_ordinal
    durable_state = _ATTEMPT_STATES_PROVEN_BY_EVENT.get(persisted.event.event_kind)
    if ordinal is None or durable_state is None:
        return None
    return NodeRailAttempt(ordinal, public_agent_attempt_state(durable_state))
