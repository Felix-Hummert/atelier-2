from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonemptyString = Annotated[str, StringConstraints(min_length=1)]


class _StrictWorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AgentNode(_StrictWorkflowModel):
    id: NonemptyString
    type: Literal["agent"]
    job: NonemptyString
    output: NonemptyString
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

    def successor(self, node_id: str) -> WorkflowNode:
        node = self.node(node_id)
        if isinstance(node, SubworkflowNode):
            raise TypeError("terminal Subworkflow has no successor")
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
