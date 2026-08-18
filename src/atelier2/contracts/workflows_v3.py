from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Annotated, Literal, assert_never

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

from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL
from atelier2.contracts.workflow_refusals import (
    WorkflowDocumentRefused,
    WorkflowRefusal,
    WorkflowRefusalReason,
)
from atelier2.contracts.workflows import (
    AnyWorkflowGraph,
    AnyWorkflowNode,
    NonemptyString,
    WaitNode,
    WorkflowGraph,
    WorkflowGraphV2,
)

MAXIMUM_INSTRUCTION_BYTES = 16 * 1024
MAXIMUM_DOCUMENT_NAME_BYTES = 200
MAXIMUM_DOCUMENT_DESCRIPTION_BYTES = 4 * 1024

type JoinRule = Literal["all_succeeded", "all_terminal"]
type AgentMode = Literal["headless", "headless_with_tools", "interactive"]

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


class IterationGreenCondition(_ClosedV3Model):
    """The child result whose arrival ends the loop, under the revision it agreed.

    It names a result the child declares, never an agent's word for being done.
    """

    output: NonemptyString
    schema_reference: VersionedReference = Field(alias="schema")


class IterationCarry(_ClosedV3Model):
    """One round's result, handed to the next round as part of its order.

    This is a binding the runtime applies between two child runs, deliberately
    not a fourth input source: a source form pointing at the previous round
    would be a document edge onto itself, and the cycle refusal of ADR 0002
    would need an exception it must never have.
    """

    name: NonemptyString
    from_output: NonemptyString
    seed: InputSource


class IterateBlock(_ClosedV3Model):
    """Repeat one child until its declared result says green, at most so often.

    `maximum_rounds` has no default: an unbounded loop must not be reachable by
    omission. Rounds are counted by the atelier itself, so the bound carries no
    meter revision and never becomes a sum across providers (ADR 0013).
    """

    maximum_rounds: int
    until: IterationGreenCondition
    carry: Annotated[tuple[IterationCarry, ...], DeclaredSequence] = ()


class LoopDeclaration(_ClosedV3Model):
    """One stretch of this graph, run again from its head a bounded number of times.

    The loop is the only legal way back. A `depends_on` edge pointing backwards
    stays refused as a cycle, unconditionally, so the graph a reader walks is
    still the acyclic one ADR 0002 decided; what repeats is declared beside the
    edges rather than hidden inside them.

    `maximum_rounds` has no default. An unbounded loop must not be reachable by
    omission, because a run nobody can bound is a run nobody can pay for.
    """

    id: NonemptyString
    body: Annotated[tuple[NonemptyString, ...], DeclaredSequence, Field(min_length=1)]
    maximum_rounds: int


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
    iterate: IterateBlock | None = None


class ActionNodeV3(_NodeV3):
    type: Literal["action"]
    operation: VersionedReference
    required_context: Annotated[tuple[RequiredContextEntry, ...], DeclaredSequence] = ()
    inputs: Annotated[tuple[NodeInput, ...], DeclaredSequence] = ()
    outputs: Annotated[tuple[NodeOutput, ...], DeclaredSequence] = ()


WorkflowNodeV3Kinds = (
    AgentNodeV3,
    DeterministicNodeV3,
    WaitNodeV3,
    SubworkflowNodeV3,
    ActionNodeV3,
)
"""The five V3 node classes, as a runtime tuple for isinstance narrowing."""

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
    loops: Annotated[tuple[LoopDeclaration, ...], DeclaredSequence] = ()

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
            _refuse_broken_iteration(node, self)
            _refuse_unconfirmed_operator_outputs(node)
        _refuse_broken_graph_boundary(self)
        _refuse_broken_loops(self)
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

    def loop_of(self, node_id: str) -> LoopDeclaration | None:
        """The loop that repeats this node, or None where nothing repeats it.

        One node belongs to at most one loop; two loops claiming it is refused
        while the document is read, so this answer is never a choice.
        """
        self.node(node_id)
        for loop in self.loops:
            if node_id in loop.body:
                return loop
        return None

    def declared_rounds_of(self, node_id: str) -> range:
        """Every round this node may run in, from the document alone.

        A bounded loop is what makes this answerable before anything runs, and
        answerable is the point: a durable record that must exist before a round
        can be receipted has to be writable before the round starts.
        """
        loop = self.loop_of(node_id)
        if loop is None:
            return range(FIRST_ROUND_ORDINAL, FIRST_ROUND_ORDINAL + 1)
        return range(FIRST_ROUND_ORDINAL, FIRST_ROUND_ORDINAL + loop.maximum_rounds)

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
type AnyWorkflowDocumentNode = AnyWorkflowNode | WorkflowNodeV3

type AnyWaitNode = WaitNode | WaitNodeV3
ANY_WAIT_NODE_KINDS = (WaitNode, WaitNodeV3)
"""The node classes that hold a run for a person, across every executable format.

A store guard asks "is this the kind that may stand in WAITING_INPUT", and the
answer is one question with two spellings. Kept together so a guard that admits
one format cannot silently refuse the other, which is how a durable run ends up
in a state its own reader calls impossible.
"""


class MultipleSinkCompletionUnsupported(ValueError):
    """V3 needs all-sinks state before one of several sinks can complete a run."""

    def __init__(self, sink_node_ids: tuple[str, ...]) -> None:
        super().__init__(
            "V3 run completion currently requires exactly one sink node, "
            f"not {len(sink_node_ids)}"
        )
        self.sink_node_ids = sink_node_ids


class BranchingAdvanceUnsupported(ValueError):
    """A node's successor is not one declared edge, so no linear rule fits.

    The typed shape exists before the edge it guards: while only a single V3 node
    was startable this could not be reached at all, and a bare `ValueError` there
    would have surfaced through the attempt path as an unhandled error on the day
    it became reachable. Naming it is what lets the caller that opens fan-out --
    the ready set of #86 -- refuse in its own words instead of crashing.
    """

    def __init__(self, node_id: str, dependents: tuple[str, ...]) -> None:
        super().__init__(
            f"node {node_id!r} is followed by {len(dependents)} nodes "
            f"({', '.join(dependents) or 'none'}); advancing past a branch needs "
            "the ready set and its join semantics, which this rule does not decide"
        )
        self.node_id = node_id
        self.dependents = dependents


def _dependents_of(graph: WorkflowGraphV3, node_id: str) -> tuple[str, ...]:
    return tuple(node.id for node in graph.nodes if node_id in node.depends_on)


def is_linear_chain(graph: WorkflowGraphV3) -> bool:
    """Whether the graph is one line, whose advance needs no scheduler.

    Two conditions say that and no more -- one entry, and no node with a second
    dependent. A fan-in needs no clause of its own: reaching two dependencies
    from one entry requires a node somewhere with two dependents, which the
    second condition already refuses. Choosing between waiting dependents is the
    ready set's decision, and this rule declines to make it rather than guessing.
    """

    if any(len(_dependents_of(graph, node.id)) > 1 for node in graph.nodes):
        return False
    entries = [node.id for node in graph.nodes if not node.depends_on]
    return len(entries) == 1


def linear_successor_id(graph: WorkflowGraphV3, node_id: str) -> str:
    """The one node the author declared to run after this one."""

    graph.node(node_id)
    dependents = _dependents_of(graph, node_id)
    if len(dependents) != 1:
        raise BranchingAdvanceUnsupported(node_id, dependents)
    successor = graph.node(dependents[0])
    if len(successor.depends_on) != 1:
        raise BranchingAdvanceUnsupported(successor.id, tuple(successor.depends_on))
    return successor.id


def is_sink_node(graph: AnyWorkflowDocument, node_id: str) -> bool:
    """Whether this node's completion completes the run, in any executable format.

    One decider for every graph family the runtime can load, because the run's
    terminal condition is one rule with two spellings: V1 and V2 configure a
    single successor, so their sink is the node naming none; V3 declares
    dependencies, so its sinks are the nodes nothing depends on. Asking a graph
    object for the spelling it happens to carry would hold only until the other
    family reaches the same call, and that break would surface in a completion
    run rather than here.
    """

    match graph:
        case WorkflowGraphV3():
            graph.node(node_id)
            sink_node_ids = graph.sink_node_ids
            if len(sink_node_ids) != 1:
                raise MultipleSinkCompletionUnsupported(sink_node_ids)
            return node_id == sink_node_ids[0]
        case WorkflowGraph() | WorkflowGraphV2():
            return graph.is_sink(node_id)
        case _ as unreachable:
            assert_never(unreachable)


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
    for entry in node.inputs:
        if entry.source is not None:
            _refuse_unbound_source(
                node, graph, "inputs", f"input {entry.name!r}", entry.source, closure
            )


def _refuse_unbound_source(
    node: WorkflowNodeV3,
    graph: WorkflowGraphV3,
    field: str,
    subject: str,
    source: InputSource,
    closure: frozenset[str],
) -> None:
    """Hold one declared read to the boundary of the node that declared it.

    Every source form a document may write is bound here, so a reader that
    arrives later — a round's handover seed, say — obeys the rules an input
    already obeys instead of growing a second, weaker owner.
    """
    if isinstance(source, ContextEntrySource):
        if source.context not in _declared_context_names(node):
            raise _refuse(
                WorkflowRefusalReason.UNDECLARED_CONTEXT,
                field,
                f"{subject} reads context {source.context!r}, "
                "which this node does not require",
                node.id,
            )
        return
    if isinstance(source, GraphInputSource):
        if source.graph_input not in {entry.name for entry in graph.graph_inputs}:
            raise _refuse(
                WorkflowRefusalReason.UNDECLARED_GRAPH_INPUT,
                field,
                f"{subject} reads graph input "
                f"{source.graph_input!r}, which this graph does not declare",
                node.id,
            )
        return
    _refuse_unordered_data_edge(node.id, field, subject, source, closure, graph)


def _refuse_unordered_data_edge(
    node_id: str,
    field: str,
    subject: str,
    source: NodeOutputSource | NodeReceiptSource,
    closure: frozenset[str],
    graph: WorkflowGraphV3,
) -> None:
    if source.node not in {node.id for node in graph.nodes}:
        raise _refuse(
            WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
            field,
            f"{subject} reads node {source.node!r}, which is not declared",
            node_id,
        )
    if source.node not in closure:
        raise _refuse(
            WorkflowRefusalReason.DATA_EDGE_OUTSIDE_CLOSURE,
            field,
            f"{subject} reads node {source.node!r}, which no "
            "depends_on edge orders before this node",
            node_id,
        )
    if isinstance(source, NodeOutputSource):
        _refuse_undeclared_output(
            node_id,
            field,
            source,
            graph,
            f"{subject} reads",
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


def _declared_reads(node: WorkflowNodeV3) -> tuple[InputSource, ...]:
    """Every source this node reads, whichever field declared it.

    A round's handover seed is as much a read of the graph as an input is, so
    the boundary rules count it instead of calling its order unread.
    """
    seeds = (
        tuple(entry.seed for entry in node.iterate.carry)
        if isinstance(node, SubworkflowNodeV3) and node.iterate is not None
        else ()
    )
    return (
        tuple(entry.source for entry in node.inputs if entry.source is not None) + seeds
    )


def _refuse_broken_iteration(node: WorkflowNodeV3, graph: WorkflowGraphV3) -> None:
    """Bound one declared iteration, as far as the document alone can judge it.

    The child's own boundary is not readable here, so what this refuses is what
    the document contradicts by itself. Whether the child declares the result
    the green condition reads is the binding step's refusal, not this one.
    """
    if not isinstance(node, SubworkflowNodeV3) or node.iterate is None:
        return
    iterate = node.iterate
    if not 1 <= iterate.maximum_rounds <= MAXIMUM_SIGNED_INT64:
        raise _refuse(
            WorkflowRefusalReason.UNBOUNDED_ITERATION,
            "maximum_rounds",
            f"{iterate.maximum_rounds} is no round count; a bounded iteration "
            f"declares 1..{MAXIMUM_SIGNED_INT64} rounds",
            node.id,
        )
    for output in node.outputs:
        if (
            output.name == iterate.until.output
            and output.schema_reference != iterate.until.schema_reference
        ):
            raise _refuse(
                WorkflowRefusalReason.ITERATION_GREEN_CONDITION_UNPROVABLE,
                "iterate",
                f"the green condition reads {iterate.until.output!r} under another "
                "schema revision than this node declares for it",
                node.id,
            )
    duplicate = _first_duplicate(entry.name for entry in iterate.carry)
    if duplicate is not None:
        raise _refuse(
            WorkflowRefusalReason.ITERATION_CARRY_UNBOUND,
            "iterate",
            f"{duplicate!r} is carried into the next round twice",
            node.id,
        )
    closure = graph.dependency_closure(node.id)
    for entry in iterate.carry:
        _refuse_unbound_source(
            node, graph, "iterate", f"carry {entry.name!r}", entry.seed, closure
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
        source.graph_input
        for node in graph.nodes
        for source in _declared_reads(node)
        if isinstance(source, GraphInputSource)
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


def _refuse_broken_loops(graph: WorkflowGraphV3) -> None:
    """Hold every declared loop to what the graph alone can judge about it.

    Three things are decided here and nowhere else: that the loop is bounded,
    that no node is claimed by two loops, and that the body is one uninterrupted
    stretch of the order the edges already declare. The third is what keeps the
    construct from becoming a second scheduler: repeating a scattered set of
    nodes would need a rule for what happens between them, and no owner decides
    that.
    """
    duplicate = _first_duplicate(loop.id for loop in graph.loops)
    if duplicate is not None:
        raise _refuse(
            WorkflowRefusalReason.DUPLICATE_NAME,
            "loops",
            f"two loops claim the id {duplicate!r}",
        )
    repeated: dict[str, str] = {}
    for loop in graph.loops:
        if not 1 <= loop.maximum_rounds <= MAXIMUM_SIGNED_INT64:
            raise _refuse(
                WorkflowRefusalReason.UNBOUNDED_ITERATION,
                "maximum_rounds",
                f"{loop.maximum_rounds} is no round count; loop {loop.id!r} "
                f"declares 1..{MAXIMUM_SIGNED_INT64} rounds",
            )
        for member in loop.body:
            if member in repeated:
                raise _refuse(
                    WorkflowRefusalReason.DUPLICATE_NAME,
                    "loops",
                    f"node {member!r} is repeated by loop {repeated[member]!r} "
                    f"and by loop {loop.id!r}",
                    member,
                )
            repeated[member] = loop.id
        _refuse_body_that_is_not_one_line(loop, graph)


def _refuse_body_that_is_not_one_line(
    loop: LoopDeclaration, graph: WorkflowGraphV3
) -> None:
    declared = {node.id for node in graph.nodes}
    for member in loop.body:
        if member not in declared:
            raise _refuse(
                WorkflowRefusalReason.UNKNOWN_NODE_REFERENCE,
                "loops",
                f"loop {loop.id!r} repeats {member!r}, which is not declared",
            )
    body = set(loop.body)
    head, *rest = loop.body
    if set(graph.node(head).depends_on) & body:
        raise _refuse(
            WorkflowRefusalReason.LOOP_BODY_NOT_ONE_LINE,
            "loops",
            f"loop {loop.id!r} enters at {head!r}, which already depends on a "
            "node the same loop repeats",
            head,
        )
    walked = [head]
    for member in rest:
        if tuple(graph.node(member).depends_on) != (walked[-1],):
            raise _refuse(
                WorkflowRefusalReason.LOOP_BODY_NOT_ONE_LINE,
                "loops",
                f"loop {loop.id!r} repeats {member!r} after {walked[-1]!r}, and "
                "no single declared edge orders it there",
                member,
            )
        walked.append(member)
    for node in graph.nodes:
        if node.id in body:
            continue
        entered = set(node.depends_on) & body
        if entered - {loop.body[-1]}:
            raise _refuse(
                WorkflowRefusalReason.LOOP_BODY_NOT_ONE_LINE,
                "loops",
                f"loop {loop.id!r} is left at {min(entered)!r}, which is "
                "not the round's last node",
                node.id,
            )


_ITERATION_FIELD_REFUSALS: Mapping[str, WorkflowRefusalReason] = {
    "maximum_rounds": WorkflowRefusalReason.UNBOUNDED_ITERATION,
    "seed": WorkflowRefusalReason.ITERATION_CARRY_UNBOUND,
}

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
        IterateBlock,
        IterationGreenCondition,
        IterationCarry,
        LoopDeclaration,
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
    # An iteration field that never parses is refused under the token that names
    # what it costs — an unbounded loop, an unbound handover — rather than under
    # the shape it happened to break, which no author can act on.
    named = _ITERATION_FIELD_REFUSALS.get(field)
    if named is not None:
        return named
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


V3_UNBOUND_AUTHORED_FORMS = (
    "join",
    "profile",
    "skills",
    "policy",
    "budget",
    "retry",
    "cancellation",
    "required_context",
    "available_context",
)
"""Every authored form of an interpreted V3 node that nothing binds at run start.

Naming the forms rather than the kinds is the point. "Only Agent nodes" would
still admit a document declaring skills, a policy or a budget, and the run would
start having silently ignored what its author wrote. A document is executable
only when nothing in it is waiting for an owner.

The list is read against whichever forms a node actually declares, so it holds
for the Wait kind too without naming it twice: a Wait node carries `join`,
`cancellation` and `required_context` and no others, and each of the three is
refused on it for the same reason it is refused on an Agent node.

`inputs` left this list when the start began binding one: it is admitted per
source by `V3_BOUND_INPUT_SOURCES` rather than as a whole form, because only an
order the graph declares has an owner at the start today. `tools` left it the
same way: an attempt now redeems the grant a node pins, one grant at a time, and
`MAXIMUM_REDEEMED_TOOL_GRANTS` is where that "one" is refused rather than
silently narrowed.
"""

MAXIMUM_REDEEMED_TOOL_GRANTS = 1
"""How many tool grants one node may pin, because an attempt redeems that many.

A redemption leaves exactly one receipt per node execution, so a second grant on
the same node would either go unredeemed or answer for the first. Which of the
two happened is not something an author should have to guess, so the document is
refused by the count it wrote.
"""

AGENT_OUTPUT_SHAPE_UNAVAILABLE = "agent-output-shape-unavailable"
"""The name a V3 Agent node is refused under when its output shape has no owner.

The one executable shape is `single-json-output/v1`: exactly one declared output,
whose whole decoded bytes are its value. A node declaring none promises bytes no
schema judges, and a node declaring several promises a runtime that can tell one
of its values from another -- neither has an owner, so both are refused by this
one name rather than by two descriptions of the same missing shape.
"""

AGENT_OUTPUTS_ONE_SHAPE_KEEPS = 1
"""How many outputs `single-json-output/v1` binds, because an attempt writes that many.

An agent attempt completes with one payload, and that payload is the value of the
output its author declared. Zero leaves the bytes unjudged and several leave one
value answered by another; the count is where the document is refused rather than
started under a shape nobody enforces.
"""


V3_BOUND_INPUT_SOURCES = (GraphInputSource, NodeOutputSource)
"""Which `inputs` source a node may read, because the runtime actually binds it.

An order the graph declares is supplied at the start and handed to the node. A
node output is what the node before it produced: the runtime writes that value
durably when the producing node completes, and hands it to the reader as part of
the job. The remaining two sources -- a node's receipt and a context entry --
name something nothing produces yet, so a document that reads one is refused by
the source it named rather than started and quietly given nothing.
"""


V3_INTERPRETED_NODE_KINDS = (AgentNodeV3, WaitNodeV3)
"""The node classes a run of this build actually reaches and executes.

An Agent node runs its attempt through the durable attempt path; a Wait node
holds the run for a person and is carried on by their answer. The remaining
three name work no runtime performs, so a document declaring one is refused by
the kind it wrote rather than started and abandoned when the run arrives there.
"""


def what_a_v3_document_still_waits_for(graph: WorkflowGraphV3) -> str | None:
    """What this document declares that no runtime binds yet, or None if nothing.

    The executable shape is a line of Agent and Wait nodes: each entered by at
    most one dependency and followed by at most one dependent, ending in a single
    sink, declaring nothing else optional. It is checked as a form rather than as
    a list of kinds, because a kind check would admit a document whose skills,
    policy or budget the run start then ignores in silence.

    A branch is still refused on purpose. `depends_on` is bound only where it
    names one edge; where a node has several dependents, choosing between them is
    the ready set ADR 0006 hands the scheduler, with fan-out and join semantics
    decided in passing. #86 owns that, so this shape stops at the line.
    """
    foreign = sorted(
        {
            node.type
            for node in graph.nodes
            if not isinstance(node, V3_INTERPRETED_NODE_KINDS)
        }
    )
    if foreign:
        return f"node kinds no runtime interprets: {', '.join(foreign)}"
    if not is_linear_chain(graph):
        return (
            f"{len(graph.nodes)} nodes that do not form one line, and choosing "
            "between them needs the ready set no runtime has yet"
        )
    # Authored, not truthy. `skills: []` and `depends_on: []` are things the
    # author wrote, and a run that ignored them would ignore a statement rather
    # than an absence -- so presence in the parsed document decides, and an empty
    # authored form is refused exactly like a filled one.
    declared = sorted(
        {
            form
            for node in graph.nodes
            for form in V3_UNBOUND_AUTHORED_FORMS
            if form in node.model_fields_set
        }
    )
    if declared:
        return f"authored forms nothing binds yet: {', '.join(declared)}"
    unredeemed = _unredeemed_tool_grants(graph)
    if unredeemed is not None:
        return unredeemed
    unbound_output = _unbound_output_forms(graph)
    if unbound_output is not None:
        return unbound_output
    unbound_prompt = _unbound_wait_forms(graph)
    if unbound_prompt is not None:
        return unbound_prompt
    unrepeatable = _unrepeatable_loop_forms(graph)
    if unrepeatable is not None:
        return unrepeatable
    return _unbound_input_sources(graph)


def _unrepeatable_loop_forms(graph: WorkflowGraphV3) -> str | None:
    """What a declared loop asks for that no round of this build carries.

    Two things are still nobody's. A round is a second execution of a node, and
    only the Agent kind mints one today -- a repeated Wait would ask the same
    person the same question under an identity the answer path does not carry.
    And a value read *out of* a loop names no round: the reader would have to
    say which round wrote it, and choosing is the verdict-driven continuation
    this head does not build.
    """
    for loop in graph.loops:
        repeated = sorted(
            {
                graph.node(member).type
                for member in loop.body
                if not isinstance(graph.node(member), AgentNodeV3)
            }
        )
        if repeated:
            return f"node kinds no round repeats in loop {loop.id!r}: " + ", ".join(
                repeated
            )
    for node in graph.nodes:
        if graph.loop_of(node.id) is not None:
            continue
        for source in _declared_reads(node):
            if not isinstance(source, NodeOutputSource):
                continue
            loop = graph.loop_of(source.node)
            if loop is not None:
                return (
                    f"node {node.id!r} reads {source.output!r} of {source.node!r}, "
                    f"which loop {loop.id!r} writes once per round, and no rule "
                    "here names which round it reads"
                )
    return None


def _unbound_wait_forms(graph: WorkflowGraphV3) -> str | None:
    """What an authored Wait node declares that the pause does not carry.

    Two of its forms are kept. `prompt` is what the person is asked, and the one
    declared output is the schema their answer is read against -- the same owner
    that judges every other value this run produces.

    `inputs` is not kept: nothing composes an earlier node's value into the
    question, so a document that wrote one would have its answer judged against a
    contract the operator was never shown. Refused by the form it wrote rather
    than accepted and dropped.
    """
    for node in graph.nodes:
        if isinstance(node, WaitNodeV3) and "inputs" in node.model_fields_set:
            return (
                f"inputs on wait node {node.id!r} that nothing composes into the "
                "question a person is asked"
            )
    return None


def _unredeemed_tool_grants(graph: WorkflowGraphV3) -> str | None:
    """What an authored `tools` entry pins that no attempt of this run redeems.

    `tools` left the blanket refusal when an attempt began redeeming the grant a
    node pins: the revision is resolved before the run exists, read as a grant
    where it is pinned, and the redemption leaves its own durable receipt. What
    the grant grants is decided by the published revision rather than here, so
    the only part of the form this pure reading can judge is how many were
    written -- and more than one is refused by its count.
    """
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3):
            continue
        if len(node.tools) > MAXIMUM_REDEEMED_TOOL_GRANTS:
            return (
                f"{len(node.tools)} tool grants on node {node.id!r}, and one "
                "attempt redeems one grant"
            )
    return None


def _unbound_output_forms(graph: WorkflowGraphV3) -> str | None:
    """What an authored `outputs` entry declares that nothing here keeps.

    `outputs` left the blanket refusal when the runtime began handing a node's
    value to the node that reads it, and admitting the form means keeping what it
    says. Two parts of it are still nobody's:

    * `confirmed_by` promises a human confirmed the artifact, and nothing asks
      anyone;
    * an output count other than one promises a shape `single-json-output/v1` is
      not, and both directions are refused under `AGENT_OUTPUT_SHAPE_UNAVAILABLE`.

    The part that is kept is the value itself: the run writes it durably when the
    producing node completes, hash-bound, and reads it against the schema its
    author pinned -- before the success is written and again before it is handed
    on. What #57 still owns around that is join, retry, canary, and the operator
    confirmation refused here.
    """
    if graph.graph_outputs:
        return "graph outputs nothing carries out of a run: " + ", ".join(
            sorted(entry.name for entry in graph.graph_outputs)
        )
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3):
            continue
        if len(node.outputs) != AGENT_OUTPUTS_ONE_SHAPE_KEEPS:
            return (
                f"{AGENT_OUTPUT_SHAPE_UNAVAILABLE}: {len(node.outputs)} outputs on "
                f"node {node.id!r}, and an agent node completes with the one value "
                "its own schema judges"
            )
        if any(output.confirmed_by is not None for output in node.outputs):
            return "an output confirmed by an operator nothing asks"
    return None


def _unbound_input_sources(graph: WorkflowGraphV3) -> str | None:
    """Which `inputs` source this document reads that the start cannot supply.

    An authored `value` is refused with the rest: it is a constant inside the
    document, which is the very thing an order replaces, and nothing hands it to
    a node today either.
    """
    unbound = sorted(
        {
            _source_form(entry)
            for node in graph.nodes
            if isinstance(node, AgentNodeV3)
            for entry in node.inputs
            if not isinstance(entry.source, V3_BOUND_INPUT_SOURCES)
        }
    )
    if unbound:
        return f"input sources nothing binds yet: {', '.join(unbound)}"
    return None


def _source_form(entry: NodeInput) -> str:
    """What an author wrote as this input's source, named back to them."""
    if entry.source is None:
        return "value"
    if isinstance(entry.source, NodeOutputSource):
        return "output"
    if isinstance(entry.source, NodeReceiptSource):
        return "receipt"
    if isinstance(entry.source, ContextEntrySource):
        return "context"
    return "graph_input"
