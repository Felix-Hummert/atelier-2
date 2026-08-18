from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from atelier2.contracts.runs import FIRST_ROUND_ORDINAL, require_exact_round_ordinal
from atelier2.contracts.verdicts import Verdict

if TYPE_CHECKING:
    from atelier2.contracts.workflows_v3 import AnyWorkflowDocument

NonemptyString = Annotated[str, StringConstraints(min_length=1)]


class _StrictWorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AgentNode(_StrictWorkflowModel):
    id: NonemptyString
    type: Literal["agent"]
    job: NonemptyString
    output: NonemptyString
    next: NonemptyString


class AgentNodeV2(_StrictWorkflowModel):
    id: NonemptyString
    type: Literal["agent"]
    role: NonemptyString
    job: NonemptyString
    next: NonemptyString


class ActionNode(_StrictWorkflowModel):
    id: NonemptyString
    type: Literal["action"]
    next: NonemptyString


class WaitNode(_StrictWorkflowModel):
    id: NonemptyString
    type: Literal["wait"]
    answer_type: Literal["integer"]
    next: NonemptyString


class SubworkflowNode(_StrictWorkflowModel):
    id: NonemptyString
    type: Literal["subworkflow"]
    operation: Literal["add"]
    operands: tuple[int, int]
    next: None

    @model_validator(mode="after")
    def reject_boolean_operands(self) -> SubworkflowNode:
        if len(self.operands) != 2 or any(
            type(operand) is not int for operand in self.operands
        ):
            raise ValueError("subworkflow operands must be exactly two strict integers")
        return self


WorkflowNode = Annotated[
    AgentNode | ActionNode | WaitNode | SubworkflowNode,
    Field(discriminator="type"),
]

WorkflowNodeV2 = Annotated[
    AgentNodeV2 | ActionNode | WaitNode | SubworkflowNode,
    Field(discriminator="type"),
]


def _sink_node_id(nodes: Sequence[Any]) -> str:
    """Name the one node no successor is configured for.

    The run's terminal condition is this structural fact, not a node kind: the
    sink's completion completes the run, and for a one-node document the sink
    is that node. Each executable format decides separately which kinds may
    stand there -- V1 and V2 both require it to be their Subworkflow -- but no
    runtime rule asks about the kind.
    """

    sinks = [node.id for node in nodes if node.next is None]
    if len(sinks) != 1:
        raise ValueError("a workflow graph requires exactly one sink node")
    return sinks[0]


class WorkflowGraph(_StrictWorkflowModel):
    format_version: Literal[1]
    start: NonemptyString
    nodes: tuple[WorkflowNode, ...]

    @field_validator("format_version", mode="before")
    @classmethod
    def require_strict_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("format_version must be a strict integer")
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> WorkflowGraph:
        if not self.nodes:
            raise ValueError("workflow graph requires at least one node")
        by_id = {node.id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("workflow node ids must be unique")
        start_node = by_id.get(self.start)
        if start_node is None:
            raise ValueError("workflow start must reference a node")
        if isinstance(start_node, ActionNode):
            raise TypeError("an Action node may not be the workflow start")

        terminal_nodes = [
            node for node in self.nodes if isinstance(node, SubworkflowNode)
        ]
        if len(terminal_nodes) != 1:
            raise ValueError("workflow requires exactly one terminal Subworkflow")
        actions = [node for node in self.nodes if isinstance(node, ActionNode)]
        if len(actions) > 1:
            raise ValueError("workflow v1 permits at most one Action")

        predecessors: dict[str, list[WorkflowNode]] = {node_id: [] for node_id in by_id}
        for node in self.nodes:
            if isinstance(node, SubworkflowNode):
                continue
            successor = by_id.get(node.next)
            if successor is None:
                raise ValueError(f"node {node.id!r} references an unknown successor")
            predecessors[node.next].append(node)

        for action in actions:
            incoming = predecessors[action.id]
            if len(incoming) != 1 or not isinstance(incoming[0], AgentNode):
                raise ValueError(
                    "the Action node requires exactly one immediate Agent predecessor"
                )

        visited: set[str] = set()
        current: WorkflowNode = start_node
        while True:
            if current.id in visited:
                raise ValueError("workflow graph must be acyclic")
            visited.add(current.id)
            if isinstance(current, SubworkflowNode):
                break
            current = by_id[current.next]
        if visited != set(by_id):
            raise ValueError("every workflow node must be reachable from start")
        return self

    def node(self, node_id: str) -> WorkflowNode:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    @property
    def sink_node_id(self) -> str:
        """The node whose completion completes the run."""

        return _sink_node_id(self.nodes)

    def is_sink(self, node_id: str) -> bool:
        """Whether no successor is configured for this node."""

        return self.node(node_id).next is None

    def successor(self, node_id: str) -> WorkflowNode:
        node = self.node(node_id)
        if node.next is None:
            raise TypeError("the sink node has no successor")
        return self.node(node.next)

    def predecessor(self, node_id: str) -> WorkflowNode:
        found: WorkflowNode | None = None
        for node in self.nodes:
            if isinstance(node, SubworkflowNode):
                continue
            if node.next == node_id:
                if found is not None:
                    raise ValueError("node has more than one predecessor")
                found = node
        if found is None:
            raise ValueError("node has no predecessor")
        return found


class WorkflowGraphV2(_StrictWorkflowModel):
    format_version: Literal[2]
    start: NonemptyString
    nodes: tuple[WorkflowNodeV2, ...]

    @field_validator("format_version", mode="before")
    @classmethod
    def require_strict_integer_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("format_version must be a strict integer")
        return value

    @model_validator(mode="after")
    def validate_graph(self) -> WorkflowGraphV2:
        if not self.nodes:
            raise ValueError("workflow graph requires at least one node")
        by_id = {node.id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("workflow node ids must be unique")
        start_node = by_id.get(self.start)
        if start_node is None:
            raise ValueError("workflow start must reference a node")
        if isinstance(start_node, ActionNode):
            raise TypeError("an Action node may not be the workflow start")

        terminal_nodes = [
            node for node in self.nodes if isinstance(node, SubworkflowNode)
        ]
        if len(terminal_nodes) != 1:
            raise ValueError("workflow requires exactly one terminal Subworkflow")
        actions = [node for node in self.nodes if isinstance(node, ActionNode)]
        if len(actions) > 1:
            raise ValueError("workflow v2 permits at most one Action")

        predecessors: dict[str, list[WorkflowNodeV2]] = {
            node_id: [] for node_id in by_id
        }
        for node in self.nodes:
            if isinstance(node, SubworkflowNode):
                continue
            successor = by_id.get(node.next)
            if successor is None:
                raise ValueError(f"node {node.id!r} references an unknown successor")
            predecessors[node.next].append(node)

        for action in actions:
            incoming = predecessors[action.id]
            if len(incoming) != 1 or not isinstance(incoming[0], AgentNodeV2):
                raise ValueError(
                    "the Action node requires exactly one immediate Agent predecessor"
                )

        visited: set[str] = set()
        current: WorkflowNodeV2 = start_node
        while True:
            if current.id in visited:
                raise ValueError("workflow graph must be acyclic")
            visited.add(current.id)
            if isinstance(current, SubworkflowNode):
                break
            current = by_id[current.next]
        if visited != set(by_id):
            raise ValueError("every workflow node must be reachable from start")
        return self

    def node(self, node_id: str) -> WorkflowNodeV2:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    @property
    def sink_node_id(self) -> str:
        """The node whose completion completes the run."""

        return _sink_node_id(self.nodes)

    def is_sink(self, node_id: str) -> bool:
        """Whether no successor is configured for this node."""

        return self.node(node_id).next is None

    def successor(self, node_id: str) -> WorkflowNodeV2:
        node = self.node(node_id)
        if node.next is None:
            raise TypeError("the sink node has no successor")
        return self.node(node.next)

    def predecessor(self, node_id: str) -> WorkflowNodeV2:
        found: WorkflowNodeV2 | None = None
        for node in self.nodes:
            if isinstance(node, SubworkflowNode):
                continue
            if node.next == node_id:
                if found is not None:
                    raise ValueError("node has more than one predecessor")
                found = node
        if found is None:
            raise ValueError("node has no predecessor")
        return found


type AnyWorkflowNode = WorkflowNode | WorkflowNodeV2
type AnyWorkflowGraph = WorkflowGraph | WorkflowGraphV2


@dataclass(frozen=True)
class RunContinues:
    """A completed node advances the run to this exact successor, in this round.

    The round travels with the successor because they are one decision: the node
    a run moves to and the round it stands in are chosen together, and a caller
    holding only the node would have to guess the other half.
    """

    node_id: str
    round_ordinal: int = FIRST_ROUND_ORDINAL

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("a run successor node id must not be empty")
        require_exact_round_ordinal(self.round_ordinal)


@dataclass(frozen=True)
class RunCompletes:
    """A completed node is the run's terminal node."""


type NodeCompletion = RunContinues | RunCompletes


def completion_after_node(
    graph: AnyWorkflowDocument,
    node_id: str,
    round_ordinal: int = FIRST_ROUND_ORDINAL,
    verdict: Verdict | None = None,
) -> NodeCompletion:
    """Decide continuation once, without inventing a successor for the sink.

    One rule for every format: a sink completes the run, anything else hands on.
    A V3 graph answers both halves through its own `depends_on` edges -- the sink
    rule, and the one declared heir where exactly one exists. Where more than one
    does, it refuses in its own typed words rather than choosing: picking between
    waiting dependents is the ready set #86 owns.

    A declared loop adds the one move the edges cannot express, and adds nothing
    else: the last node of a round hands back to the first. Two things end it,
    and they are read in this order. A verdict the author declared is the early
    exit -- the round's own answer says whether the work goes round again -- and
    the declared bound is the fallback that holds whatever the verdict says. A
    loop that ends either way falls through to the rule above and ends where any
    other node would, with no word invented for it.

    The verdict travels in rather than being read here, because reading it is a
    durable act -- the value a round produced -- and this rule stays a decision
    about the document. A loop that declares one and is asked without it refuses
    by name instead of continuing in whichever direction the omission implies.
    """
    # V3's vocabulary is built on this module's, so `workflows_v3` imports here and
    # never the other way at module scope; reading its rules needs this import.
    from atelier2.contracts.workflows_v3 import (
        LoopVerdictNotRead,
        WorkflowGraphV3,
        is_sink_node,
        linear_successor_id,
    )

    require_exact_round_ordinal(round_ordinal)
    if isinstance(graph, WorkflowGraphV3):
        loop = graph.loop_of(node_id)
        if loop is not None and node_id == loop.body[-1]:
            condition = loop.repeat_while
            if condition is not None and verdict is None:
                raise LoopVerdictNotRead(loop.id, node_id)
            round_again = condition is None or verdict is condition.verdict
            if round_again and round_ordinal < loop.maximum_rounds:
                return RunContinues(loop.body[0], round_ordinal + 1)
        if is_sink_node(graph, node_id):
            return RunCompletes()
        successor_id = linear_successor_id(graph, node_id)
        return RunContinues(successor_id, round_of(graph, successor_id, round_ordinal))
    if graph.is_sink(node_id):
        return RunCompletes()
    return RunContinues(graph.successor(node_id).id)


def round_of(
    graph: AnyWorkflowDocument, node_id: str, current_round_ordinal: int
) -> int:
    """Which round this node stands in while the run is in that round.

    A node inside the loop that is turning stands in the round that is turning;
    a node no loop repeats runs exactly once, whatever the run has been through
    to reach it. One reader of that rule is what keeps a stored execution and the
    execution a later step recomputes from disagreeing.
    """
    from atelier2.contracts.workflows_v3 import WorkflowGraphV3

    if not isinstance(graph, WorkflowGraphV3):
        return FIRST_ROUND_ORDINAL
    if graph.loop_of(node_id) is None:
        return FIRST_ROUND_ORDINAL
    return current_round_ordinal
