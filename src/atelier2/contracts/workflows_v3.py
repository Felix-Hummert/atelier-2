from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    ValidationError,
    field_validator,
    model_validator,
)

from atelier2.contracts.workflow_refusals import (
    WorkflowDocumentRefused,
    WorkflowRefusal,
    WorkflowRefusalReason,
)
from atelier2.contracts.workflows import AnyWorkflowGraph, NonemptyString

MAXIMUM_INSTRUCTION_BYTES = 16 * 1024
MAXIMUM_DOCUMENT_NAME_BYTES = 200
MAXIMUM_DOCUMENT_DESCRIPTION_BYTES = 4 * 1024

type JoinRule = Literal["all_succeeded", "all_terminal"]
type AgentMode = Literal["headless", "interactive"]

DEFAULT_SINGLE_DEPENDENCY_JOIN: JoinRule = "all_succeeded"

RETIRED_KEY_REPLACEMENTS: Mapping[str, str] = {
    "start": "depends_on, from which the entry set is derived",
    "next": "depends_on",
    "job": "instruction",
    "output": "outputs",
    "answer_type": "the single declared output and its versioned schema",
    "operands": "inputs",
    "arguments": "inputs",
    "logical_key": "the idempotency key the core derives",
}


def _declared_sequence(value: object) -> object:
    return tuple(value) if type(value) is list else value


DeclaredSequence = BeforeValidator(_declared_sequence)


def _bounded_authored_text(text: str, subject: str, maximum_bytes: int) -> str:
    if not text.strip():
        raise ValueError(f"{subject} must carry text")
    if len(text.encode("utf-8")) > maximum_bytes:
        raise ValueError(f"{subject} exceeds {maximum_bytes} UTF-8 bytes")
    return text


class _ClosedV3Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class VersionedReference(_ClosedV3Model):
    ref: NonemptyString
    revision: NonemptyString


class RequiredContextSource(_ClosedV3Model):
    ref: NonemptyString
    revision: NonemptyString
    selector: NonemptyString


class RequiredContextEntry(_ClosedV3Model):
    name: NonemptyString
    source: RequiredContextSource


class AvailableContextEntry(_ClosedV3Model):
    name: NonemptyString
    source: VersionedReference
    read_operations: Annotated[
        tuple[VersionedReference, ...], DeclaredSequence, Field(min_length=1)
    ]


class NodeOutputSource(_ClosedV3Model):
    node: NonemptyString
    output: NonemptyString


class NodeReceiptSource(_ClosedV3Model):
    node: NonemptyString
    receipt: Literal["terminal"]


class ContextEntrySource(_ClosedV3Model):
    context: NonemptyString


class GraphInputSource(_ClosedV3Model):
    """One order the graph itself was started with, read by name.

    The other three sources name something the run produces; this one names
    something the run was given. It is therefore the only source available to an
    entry node, and the only one a start has to satisfy before the first node runs.
    """

    graph_input: NonemptyString


def _input_source_form(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    for key in ("output", "receipt", "context", "graph_input"):
        if key in value:
            return key
    return None


InputSource = Annotated[
    Annotated[NodeOutputSource, Tag("output")]
    | Annotated[NodeReceiptSource, Tag("receipt")]
    | Annotated[ContextEntrySource, Tag("context")]
    | Annotated[GraphInputSource, Tag("graph_input")],
    Discriminator(_input_source_form),
]


class NodeInput(_ClosedV3Model):
    name: NonemptyString
    source: InputSource | None = Field(default=None, alias="from")
    value: str | None = None

    @model_validator(mode="after")
    def require_exactly_one_source(self) -> NodeInput:
        if (self.source is None) == (self.value is None):
            raise ValueError(
                f"input {self.name!r} must name exactly one of an upstream source "
                "or an authored value"
            )
        return self


class NodeOutput(_ClosedV3Model):
    name: NonemptyString
    schema_reference: VersionedReference = Field(alias="schema")
    confirmed_by: Literal["operator"] | None = None


class GraphInput(_ClosedV3Model):
    name: NonemptyString
    schema_reference: VersionedReference = Field(alias="schema")


class GraphOutput(_ClosedV3Model):
    name: NonemptyString
    source: NodeOutputSource = Field(alias="from")


class _NodeV3(_ClosedV3Model):
    id: NonemptyString
    depends_on: Annotated[tuple[NonemptyString, ...], DeclaredSequence] = ()
    join: JoinRule | None = None
    cancellation: VersionedReference | None = None


class AgentNodeV3(_NodeV3):
    type: Literal["agent"]
    role: NonemptyString
    mode: AgentMode
    instruction: NonemptyString
    profile: VersionedReference | None = None
    skills: Annotated[tuple[VersionedReference, ...], DeclaredSequence] = ()
    tools: Annotated[tuple[VersionedReference, ...], DeclaredSequence] = ()
    policy: VersionedReference | None = None
    required_context: Annotated[tuple[RequiredContextEntry, ...], DeclaredSequence] = ()
    available_context: Annotated[
        tuple[AvailableContextEntry, ...], DeclaredSequence
    ] = ()
    inputs: Annotated[tuple[NodeInput, ...], DeclaredSequence] = ()
    outputs: Annotated[tuple[NodeOutput, ...], DeclaredSequence] = ()
    budget: VersionedReference | None = None
    retry: VersionedReference | None = None

    @field_validator("instruction")
    @classmethod
    def bound_authored_instruction(cls, instruction: str) -> str:
        return _bounded_authored_text(
            instruction, "instruction", MAXIMUM_INSTRUCTION_BYTES
        )


class DeterministicNodeV3(_NodeV3):
    type: Literal["deterministic"]
    operation: VersionedReference
    required_context: Annotated[tuple[RequiredContextEntry, ...], DeclaredSequence] = ()
    inputs: Annotated[tuple[NodeInput, ...], DeclaredSequence] = ()
    outputs: Annotated[tuple[NodeOutput, ...], DeclaredSequence, Field(min_length=1)]
    retry: VersionedReference | None = None


class WaitNodeV3(_NodeV3):
    type: Literal["wait"]
    prompt: NonemptyString
    required_context: Annotated[tuple[RequiredContextEntry, ...], DeclaredSequence] = ()
    inputs: Annotated[tuple[NodeInput, ...], DeclaredSequence] = ()
    outputs: Annotated[
        tuple[NodeOutput, ...], DeclaredSequence, Field(min_length=1, max_length=1)
    ]


class SubworkflowNodeV3(_NodeV3):
    type: Literal["subworkflow"]
    workflow: VersionedReference
    inputs: Annotated[tuple[NodeInput, ...], DeclaredSequence] = ()
    outputs: Annotated[tuple[NodeOutput, ...], DeclaredSequence] = ()
    budget: VersionedReference | None = None


class ActionNodeV3(_NodeV3):
    type: Literal["action"]
    operation: VersionedReference
    required_context: Annotated[tuple[RequiredContextEntry, ...], DeclaredSequence] = ()
    inputs: Annotated[tuple[NodeInput, ...], DeclaredSequence] = ()
    outputs: Annotated[tuple[NodeOutput, ...], DeclaredSequence] = ()


WorkflowNodeV3 = Annotated[
    AgentNodeV3 | DeterministicNodeV3 | WaitNodeV3 | SubworkflowNodeV3 | ActionNodeV3,
    Field(discriminator="type"),
]


class WorkflowGraphV3(_ClosedV3Model):
    format_version: Literal[3]
    name: NonemptyString
    description: NonemptyString | None = None
    graph_inputs: Annotated[tuple[GraphInput, ...], DeclaredSequence] = ()
    graph_outputs: Annotated[tuple[GraphOutput, ...], DeclaredSequence] = ()
    nodes: Annotated[tuple[WorkflowNodeV3, ...], DeclaredSequence, Field(min_length=1)]

    @field_validator("name")
    @classmethod
    def bound_one_line_name(cls, name: str) -> str:
        # A label a surface shows on one line must carry no boundary any renderer
        # could break at, so the test is the whole Unicode set the language splits
        # on — CR, VT, FF, the separators, NEL, LS and PS as well as LF.
        if name.splitlines() != [name]:
            raise ValueError("name is the one line a picker shows")
        return _bounded_authored_text(name, "name", MAXIMUM_DOCUMENT_NAME_BYTES)

    @field_validator("description")
    @classmethod
    def bound_description(cls, description: str) -> str:
        return _bounded_authored_text(
            description, "description", MAXIMUM_DOCUMENT_DESCRIPTION_BYTES
        )

    @model_validator(mode="after")
    def validate_vocabulary(self) -> WorkflowGraphV3:
        _refuse_colliding_ids(self.nodes)
        _refuse_broken_control_edges(self.nodes)
        _refuse_unresolvable_order(self.nodes)
        for node in self.nodes:
            _refuse_wrong_join_arity(node)
            _refuse_colliding_declared_names(node)
            _refuse_unbound_inputs(node, self)
            _refuse_unconfirmed_operator_outputs(node)
        _refuse_broken_graph_boundary(self)
        return self

    def node(self, node_id: str) -> WorkflowNodeV3:
        for node in self.nodes:
            if node.id == node_id:
                return node
        raise KeyError(node_id)

    @property
    def entry_node_ids(self) -> tuple[str, ...]:
        return tuple(node.id for node in self.nodes if not node.depends_on)

    @property
    def sink_node_ids(self) -> tuple[str, ...]:
        depended_upon = {
            dependency for node in self.nodes for dependency in node.depends_on
        }
        return tuple(node.id for node in self.nodes if node.id not in depended_upon)

    def join_of(self, node_id: str) -> JoinRule | None:
        """The join a scheduler applies: the omitted single-edge join is explicit."""
        node = self.node(node_id)
        if not node.depends_on:
            return None
        return node.join or DEFAULT_SINGLE_DEPENDENCY_JOIN

    def dependency_closure(self, node_id: str) -> frozenset[str]:
        """Every node reachable by following `depends_on` from this node."""
        closure: set[str] = set()
        pending = list(self.node(node_id).depends_on)
        while pending:
            dependency = pending.pop()
            if dependency in closure:
                continue
            closure.add(dependency)
            pending.extend(self.node(dependency).depends_on)
        return frozenset(closure)


type AnyWorkflowDocument = AnyWorkflowGraph | WorkflowGraphV3


def _refuse(
    reason: WorkflowRefusalReason,
    field: str,
    detail: str,
    node: str | None = None,
) -> WorkflowDocumentRefused:
    return WorkflowDocumentRefused(WorkflowRefusal(reason, field, detail, node))


def _first_duplicate(names: Iterable[str]) -> str | None:
    seen: set[str] = set()
    for name in names:
        if name in seen:
            return name
        seen.add(name)
    return None


def _refuse_colliding_ids(nodes: Sequence[WorkflowNodeV3]) -> None:
    duplicate = _first_duplicate(node.id for node in nodes)
    if duplicate is not None:
        raise _refuse(
            WorkflowRefusalReason.DUPLICATE_NODE_ID,
            "id",
            "two nodes claim the same id",
            duplicate,
        )


def _refuse_broken_control_edges(nodes: Sequence[WorkflowNodeV3]) -> None:
    declared = {node.id for node in nodes}
    for node in nodes:
        duplicate = _first_duplicate(node.depends_on)
        if duplicate is not None:
            raise _refuse(
                WorkflowRefusalReason.DUPLICATE_NAME,
                "depends_on",
                f"dependency {duplicate!r} is declared twice",
                node.id,
            )
        for dependency in node.depends_on:
            if dependency == node.id:
                raise _refuse(
                    WorkflowRefusalReason.CYCLE,
                    "depends_on",
                    "a node may not depend on itself",
                    node.id,
                )
            if dependency not in declared:
                raise _refuse(
                    WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
                    "depends_on",
                    f"dependency {dependency!r} names no declared node",
                    node.id,
                )


def _refuse_unresolvable_order(nodes: Sequence[WorkflowNodeV3]) -> None:
    resolved: set[str] = set()
    pending = list(nodes)
    while pending:
        ready = [node for node in pending if resolved.issuperset(node.depends_on)]
        if not ready:
            unresolved = sorted(node.id for node in pending)
            raise _refuse(
                WorkflowRefusalReason.CYCLE,
                "depends_on",
                f"the dependency edges of {', '.join(unresolved)} never release",
                pending[0].id,
            )
        resolved.update(node.id for node in ready)
        ready_ids = {node.id for node in ready}
        pending = [node for node in pending if node.id not in ready_ids]


def _refuse_wrong_join_arity(node: WorkflowNodeV3) -> None:
    if not node.depends_on and node.join is not None:
        raise _refuse(
            WorkflowRefusalReason.JOIN_WITHOUT_DEPENDENCY,
            "join",
            "a node with no dependency has nothing to join",
            node.id,
        )
    if len(node.depends_on) > 1 and node.join is None:
        raise _refuse(
            WorkflowRefusalReason.JOIN_REQUIRED,
            "join",
            "a node with more than one dependency must declare its join",
            node.id,
        )


def _declared_context_names(node: WorkflowNodeV3) -> tuple[str, ...]:
    if isinstance(node, SubworkflowNodeV3):
        return ()
    return tuple(entry.name for entry in node.required_context)


def _refuse_colliding_declared_names(node: WorkflowNodeV3) -> None:
    for field, names in (
        ("inputs", [entry.name for entry in node.inputs]),
        ("outputs", [entry.name for entry in node.outputs]),
        ("required_context", list(_declared_context_names(node))),
        (
            "available_context",
            [entry.name for entry in node.available_context]
            if isinstance(node, AgentNodeV3)
            else [],
        ),
    ):
        duplicate = _first_duplicate(names)
        if duplicate is not None:
            raise _refuse(
                WorkflowRefusalReason.DUPLICATE_NAME,
                field,
                f"{duplicate!r} is declared twice",
                node.id,
            )


def _refuse_unbound_inputs(node: WorkflowNodeV3, graph: WorkflowGraphV3) -> None:
    closure = graph.dependency_closure(node.id)
    context_names = _declared_context_names(node)
    graph_input_names = {entry.name for entry in graph.graph_inputs}
    for entry in node.inputs:
        source = entry.source
        if isinstance(source, ContextEntrySource):
            if source.context not in context_names:
                raise _refuse(
                    WorkflowRefusalReason.UNDECLARED_CONTEXT,
                    "inputs",
                    f"input {entry.name!r} reads context {source.context!r}, "
                    "which this node does not require",
                    node.id,
                )
            continue
        if isinstance(source, GraphInputSource):
            if source.graph_input not in graph_input_names:
                raise _refuse(
                    WorkflowRefusalReason.UNDECLARED_GRAPH_INPUT,
                    "inputs",
                    f"input {entry.name!r} reads graph input "
                    f"{source.graph_input!r}, which this graph does not declare",
                    node.id,
                )
            continue
        if source is None:
            continue
        _refuse_unordered_data_edge(node.id, entry.name, source, closure, graph)


def _refuse_unordered_data_edge(
    node_id: str,
    input_name: str,
    source: NodeOutputSource | NodeReceiptSource,
    closure: frozenset[str],
    graph: WorkflowGraphV3,
) -> None:
    if source.node not in {node.id for node in graph.nodes}:
        raise _refuse(
            WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
            "inputs",
            f"input {input_name!r} reads node {source.node!r}, which is not declared",
            node_id,
        )
    if source.node not in closure:
        raise _refuse(
            WorkflowRefusalReason.DATA_EDGE_OUTSIDE_CLOSURE,
            "inputs",
            f"input {input_name!r} reads node {source.node!r}, which no "
            "depends_on edge orders before this node",
            node_id,
        )
    if isinstance(source, NodeOutputSource):
        _refuse_undeclared_output(
            node_id,
            "inputs",
            source,
            graph,
            f"input {input_name!r} reads",
        )


def _refuse_undeclared_output(
    node_id: str | None,
    field: str,
    source: NodeOutputSource,
    graph: WorkflowGraphV3,
    subject: str,
) -> None:
    declared = {output.name for output in graph.node(source.node).outputs}
    if source.output not in declared:
        raise _refuse(
            WorkflowRefusalReason.UNDECLARED_OUTPUT,
            field,
            f"{subject} output {source.output!r}, which node "
            f"{source.node!r} does not declare",
            node_id,
        )


def _refuse_unconfirmed_operator_outputs(node: WorkflowNodeV3) -> None:
    interactive = isinstance(node, AgentNodeV3) and node.mode == "interactive"
    for output in node.outputs:
        if interactive and output.confirmed_by is None:
            raise _refuse(
                WorkflowRefusalReason.UNCONFIRMED_INTERACTIVE_OUTPUT,
                "outputs",
                f"output {output.name!r} of an interactive node must be "
                "confirmed by the operator",
                node.id,
            )
        if not interactive and output.confirmed_by is not None:
            raise _refuse(
                WorkflowRefusalReason.CONFIRMATION_WITHOUT_INTERACTIVE_MODE,
                "outputs",
                f"output {output.name!r} claims operator confirmation, which "
                "only an interactive agent node can carry",
                node.id,
            )


def _refuse_broken_graph_boundary(graph: WorkflowGraphV3) -> None:
    for field, names in (
        ("graph_inputs", [entry.name for entry in graph.graph_inputs]),
        ("graph_outputs", [entry.name for entry in graph.graph_outputs]),
    ):
        duplicate = _first_duplicate(names)
        if duplicate is not None:
            raise _refuse(
                WorkflowRefusalReason.DUPLICATE_NAME,
                field,
                f"{duplicate!r} is declared twice",
            )
    read_graph_inputs = {
        entry.source.graph_input
        for node in graph.nodes
        for entry in node.inputs
        if isinstance(entry.source, GraphInputSource)
    }
    for graph_input in graph.graph_inputs:
        if graph_input.name not in read_graph_inputs:
            raise _refuse(
                WorkflowRefusalReason.GRAPH_INPUT_UNREAD,
                "graph_inputs",
                f"graph input {graph_input.name!r} is read by no node, so every "
                "start would have to supply a value nothing consumes",
            )
    declared = {node.id for node in graph.nodes}
    sinks = set(graph.sink_node_ids)
    for entry in graph.graph_outputs:
        if entry.source.node not in declared:
            raise _refuse(
                WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
                "graph_outputs",
                f"graph output {entry.name!r} reads node "
                f"{entry.source.node!r}, which is not declared",
            )
        if entry.source.node not in sinks:
            raise _refuse(
                WorkflowRefusalReason.GRAPH_OUTPUT_NOT_SINK,
                "graph_outputs",
                f"graph output {entry.name!r} reads node "
                f"{entry.source.node!r}, which other nodes depend on",
            )
        _refuse_undeclared_output(
            None,
            "graph_outputs",
            entry.source,
            graph,
            f"graph output {entry.name!r} reads",
        )


_VOCABULARY_FIELDS = frozenset(
    field.alias or name
    for model in (
        WorkflowGraphV3,
        AgentNodeV3,
        DeterministicNodeV3,
        WaitNodeV3,
        SubworkflowNodeV3,
        ActionNodeV3,
        NodeInput,
        NodeOutput,
        GraphInput,
        GraphOutput,
        RequiredContextEntry,
        AvailableContextEntry,
        VersionedReference,
        RequiredContextSource,
        NodeOutputSource,
        NodeReceiptSource,
        ContextEntrySource,
        GraphInputSource,
    )
    for name, field in model.model_fields.items()
)


def validate_workflow_graph_v3(document: Mapping[str, object]) -> WorkflowGraphV3:
    """Validate one loaded format-3 document into the closed node vocabulary."""
    _refuse_retired_keys(document)
    try:
        return WorkflowGraphV3.model_validate(document, strict=True)
    except ValidationError as error:
        raise _refusal_from(error, document) from error


def _refuse_retired_keys(document: Mapping[str, object]) -> None:
    _refuse_retired_keys_of(document, None)
    nodes = document.get("nodes")
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        return
    for node in nodes:
        if isinstance(node, Mapping):
            identifier = node.get("id")
            _refuse_retired_keys_of(
                node, identifier if isinstance(identifier, str) else None
            )


def _refuse_retired_keys_of(mapping: Mapping[str, object], node: str | None) -> None:
    for key in mapping:
        replacement = RETIRED_KEY_REPLACEMENTS.get(key)
        if replacement is not None:
            raise _refuse(
                WorkflowRefusalReason.RETIRED_KEY,
                key,
                f"format 3 retired {key!r}; it is replaced by {replacement}",
                node,
            )


def _refusal_from(
    error: ValidationError, document: Mapping[str, object]
) -> WorkflowDocumentRefused:
    first = error.errors()[0]
    location = first["loc"]
    error_type = str(first["type"])
    raw_context = first.get("ctx")
    context: Mapping[str, object] = (
        raw_context if isinstance(raw_context, Mapping) else {}
    )
    cause = context.get("error")
    detail = str(cause) if cause is not None else str(first["msg"])
    tag_field = _tag_carrying_field(error_type, context)
    if tag_field is None:
        field = _field_name(location)
        reason = _reason_for(error_type, field)
    else:
        field = tag_field
        reason = (
            WorkflowRefusalReason.MISSING_FIELD
            if error_type == "union_tag_not_found"
            else WorkflowRefusalReason.INVALID_VALUE
        )
    return _refuse(reason, field, detail, _node_id_at(location, document))


def _tag_carrying_field(error_type: str, context: Mapping[str, object]) -> str | None:
    """The field whose value chooses the member of a closed union, when one does.

    A node kind is chosen by its own `type` field, so an unknown or absent kind
    is refused there rather than at the collection that holds the node. A
    discriminator that reads the shape of a value instead of one field names no
    field, and its refusal stays where the location points.
    """
    if error_type not in ("union_tag_invalid", "union_tag_not_found"):
        return None
    discriminator = str(context.get("discriminator", ""))
    if len(discriminator) < 3 or not discriminator.startswith("'"):
        return None
    if not discriminator.endswith("'"):
        return None
    return discriminator[1:-1]


def _field_name(location: Sequence[str | int]) -> str:
    for element in reversed(location):
        if isinstance(element, str):
            return element
    return "nodes"


def _reason_for(error_type: str, field: str) -> WorkflowRefusalReason:
    if error_type == "missing":
        return WorkflowRefusalReason.MISSING_FIELD
    if error_type != "extra_forbidden":
        return WorkflowRefusalReason.INVALID_VALUE
    if field in RETIRED_KEY_REPLACEMENTS:
        return WorkflowRefusalReason.RETIRED_KEY
    if field in _VOCABULARY_FIELDS:
        return WorkflowRefusalReason.REFUSED_FIELD
    return WorkflowRefusalReason.UNKNOWN_FIELD


def _node_id_at(
    location: Sequence[str | int], document: Mapping[str, object]
) -> str | None:
    if len(location) < 2 or location[0] != "nodes":
        return None
    index = location[1]
    nodes = document.get("nodes")
    if not isinstance(index, int) or not isinstance(nodes, Sequence):
        return None
    if index >= len(nodes):
        return None
    node = nodes[index]
    if not isinstance(node, Mapping):
        return None
    identifier = node.get("id")
    return identifier if isinstance(identifier, str) else None
