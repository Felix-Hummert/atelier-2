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
    """A completed node advances the run to this exact successor."""

    node_id: str

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("a run successor node id must not be empty")


@dataclass(frozen=True)
class RunCompletes:
    """A completed node is the run's terminal node."""


type NodeCompletion = RunContinues | RunCompletes


def completion_after_node(graph: AnyWorkflowDocument, node_id: str) -> NodeCompletion:
    """Decide continuation once, without inventing a successor for the sink.

    One rule for every format: a sink completes the run, anything else hands on.
    A V3 graph answers the sink half through its own `depends_on` edges, and has
    no successor to hand to -- the one startable V3 shape is a single node, and
    advancing beyond it is the ready set H2 and #86 own.
    """
    # V3's vocabulary is built on this module's, so `workflows_v3` imports here and
    # never the other way at module scope; reading its sink rule needs this import.
    from atelier2.contracts.workflows_v3 import WorkflowGraphV3, is_sink_node

    if isinstance(graph, WorkflowGraphV3):
        if is_sink_node(graph, node_id):
            return RunCompletes()
        raise ValueError(
            f"V3 node {node_id!r} is not a sink, and no runtime advances past it yet"
        )
    if graph.is_sink(node_id):
        return RunCompletes()
    return RunContinues(graph.successor(node_id).id)
