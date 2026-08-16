from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import assert_never

from atelier2.contracts.runs import WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.workflow_bindings_v3 import (
    BoundSubworkflow,
    SubworkflowBinding,
    SubworkflowBindingRefusal,
    SubworkflowBindingRefusalReason,
    SubworkflowBindingRefused,
)
from atelier2.contracts.workflows_v3 import (
    GraphInput,
    GraphInputSource,
    GraphOutput,
    NodeInput,
    NodeOutput,
    NodeOutputSource,
    SubworkflowNodeV3,
    VersionedReference,
    WorkflowGraphV3,
)
from atelier2.ports.workflow_revisions import (
    PublishedWorkflowFound,
    PublishedWorkflowMissing,
    PublishedWorkflowResolver,
    WorkflowDocumentParser,
)

type ReachedBy = tuple[VersionedReference, ...]


def bind_subworkflow_boundaries(
    document: WorkflowGraphV3,
    resolver: PublishedWorkflowResolver,
    parser: WorkflowDocumentParser,
    maximum_nesting_depth: int,
    maximum_iteration_rounds: int,
) -> SubworkflowBinding:
    """Bind every subworkflow node of one document against real child content.

    Each node's declared inputs and outputs must match the published child's
    `graph_inputs` and `graph_outputs` one to one by name and schema revision, no
    chain may run deeper than the attested `maximum_nesting_depth`, and any
    deviation raises `SubworkflowBindingRefused` naming the node and the chain.
    """
    if maximum_iteration_rounds < 1:
        raise ValueError("a proven round bound admits at least one round")
    if maximum_nesting_depth < 0:
        raise ValueError("the attested nesting depth bound must not be negative")
    binder = _Binder(resolver, parser, maximum_nesting_depth, maximum_iteration_rounds)
    return SubworkflowBinding(binder.bind_nodes(document, _Chain()))


@dataclass(frozen=True)
class _Chain:
    """How one child revision was reached: the references followed to get there."""

    references: ReachedBy = ()

    def reaching(self, reference: VersionedReference) -> ReachedBy:
        return self.references + (reference,)

    def extended(self, reference: VersionedReference) -> _Chain:
        return _Chain(self.reaching(reference))


@dataclass(frozen=True)
class _Binder:
    resolver: PublishedWorkflowResolver
    parser: WorkflowDocumentParser
    maximum_nesting_depth: int
    maximum_iteration_rounds: int

    def bind_nodes(
        self, graph: WorkflowGraphV3, chain: _Chain
    ) -> tuple[BoundSubworkflow, ...]:
        return tuple(
            self.bind_node(node, graph, chain)
            for node in graph.nodes
            if isinstance(node, SubworkflowNodeV3)
        )

    def bind_node(
        self, node: SubworkflowNodeV3, graph: WorkflowGraphV3, chain: _Chain
    ) -> BoundSubworkflow:
        reached_by = chain.reaching(node.workflow)
        self._refuse_excess_depth(node, reached_by)
        self._refuse_excess_rounds(node, reached_by)
        revision = self._resolve(node, reached_by)
        child = self._parse(node, reached_by, revision.document)
        _refuse_broken_boundary(node, graph, child, reached_by)
        return BoundSubworkflow(
            node.id,
            node.workflow,
            revision.revision_hash,
            child,
            self.bind_nodes(child, chain.extended(node.workflow)),
        )

    def _resolve(
        self, node: SubworkflowNodeV3, reached_by: ReachedBy
    ) -> WorkflowRevision:
        """Resolve the exact revision the node names, or refuse naming why not.

        A reference names one immutable revision by its hash, so a store that
        answers with another revision is refused rather than silently bound.
        """
        requested = _requested_revision_hash(node, reached_by)
        resolved = self.resolver.resolve(node.workflow)
        match resolved:
            case PublishedWorkflowFound(revision):
                if revision.revision_hash != requested:
                    raise _refuse(
                        SubworkflowBindingRefusalReason.RESOLVED_REVISION_MISMATCH,
                        node,
                        reached_by,
                        f"this reference names revision {requested.value}, and the "
                        f"publication store answered with {revision.revision_hash.value}",
                    )
                return revision
            case PublishedWorkflowMissing():
                raise _refuse(
                    SubworkflowBindingRefusalReason.UNPUBLISHED_WORKFLOW_REFERENCE,
                    node,
                    reached_by,
                    "no published revision carries this reference",
                )
            case _ as unreachable:
                assert_never(unreachable)

    def _parse(
        self, node: SubworkflowNodeV3, reached_by: ReachedBy, document: bytes
    ) -> WorkflowGraphV3:
        try:
            parsed = self.parser(document)
        except ValueError as error:
            raise _refuse(
                SubworkflowBindingRefusalReason.CHILD_DOCUMENT_REFUSED,
                node,
                reached_by,
                f"the published child revision is refused: {error}",
            ) from error
        if not isinstance(parsed, WorkflowGraphV3):
            raise _refuse(
                SubworkflowBindingRefusalReason.CHILD_FORMAT_UNSUPPORTED,
                node,
                reached_by,
                "the published child revision declares no reusable graph boundary",
            )
        return parsed

    def _refuse_excess_rounds(
        self, node: SubworkflowNodeV3, reached_by: ReachedBy
    ) -> None:
        """Whether the composition proved as many rounds as the document asks for.

        The bound is handed to this binding the way the nesting bound already is,
        not attested on a capability: what a runtime can carry is a property of
        the composition that assembled it. A document asking for more is refused
        before any round exists rather than stopped in the middle of one.
        """
        if node.iterate is None or (
            node.iterate.maximum_rounds <= self.maximum_iteration_rounds
        ):
            return
        raise _refuse(
            SubworkflowBindingRefusalReason.ITERATION_ROUNDS_EXCEEDED,
            node,
            reached_by,
            f"the iteration asks for {node.iterate.maximum_rounds} rounds, past "
            f"the proven maximum of {self.maximum_iteration_rounds}",
        )

    def _refuse_excess_depth(
        self, node: SubworkflowNodeV3, reached_by: ReachedBy
    ) -> None:
        if len(reached_by) > self.maximum_nesting_depth:
            raise _refuse(
                SubworkflowBindingRefusalReason.NESTING_DEPTH_EXCEEDED,
                node,
                reached_by,
                f"this chain nests {len(reached_by)} deep, past the attested "
                f"maximum of {self.maximum_nesting_depth}",
            )


def _refuse(
    reason: SubworkflowBindingRefusalReason,
    node: SubworkflowNodeV3,
    reached_by: ReachedBy,
    detail: str,
) -> SubworkflowBindingRefused:
    return SubworkflowBindingRefused(
        SubworkflowBindingRefusal(reason, node.id, reached_by, detail)
    )


def _requested_revision_hash(
    node: SubworkflowNodeV3, reached_by: ReachedBy
) -> WorkflowRevisionHash:
    """The exact revision a workflow reference names, or a refusal that it names none."""
    try:
        return WorkflowRevisionHash(node.workflow.revision)
    except ValueError as error:
        raise _refuse(
            SubworkflowBindingRefusalReason.MALFORMED_WORKFLOW_REFERENCE,
            node,
            reached_by,
            f"a workflow reference names one revision hash, and "
            f"{node.workflow.revision!r} is not one",
        ) from error


def _refuse_broken_boundary(
    node: SubworkflowNodeV3,
    graph: WorkflowGraphV3,
    child: WorkflowGraphV3,
    reached_by: ReachedBy,
) -> None:
    carried = _carried_names(node)
    _refuse_unmatched_names(
        node,
        reached_by,
        "graph input",
        [entry.name for entry in node.inputs] + sorted(carried),
        [entry.name for entry in child.graph_inputs],
        SubworkflowBindingRefusalReason.MISSING_INPUT_BINDING,
        SubworkflowBindingRefusalReason.EXTRA_INPUT_BINDING,
    )
    _refuse_unmatched_names(
        node,
        reached_by,
        "graph output",
        [entry.name for entry in node.outputs],
        [entry.name for entry in child.graph_outputs],
        SubworkflowBindingRefusalReason.MISSING_OUTPUT_BINDING,
        SubworkflowBindingRefusalReason.EXTRA_OUTPUT_BINDING,
    )
    for graph_input in child.graph_inputs:
        if graph_input.name in carried:
            continue
        bound_input = _named(node.inputs, graph_input.name)
        _refuse_differing_schema(
            node,
            reached_by,
            f"graph input {graph_input.name!r}",
            _proven_schema(node, reached_by, bound_input, graph),
            graph_input.schema_reference,
        )
    for graph_output in child.graph_outputs:
        bound_output = _named(node.outputs, graph_output.name)
        _refuse_differing_schema(
            node,
            reached_by,
            f"graph output {graph_output.name!r}",
            bound_output.schema_reference,
            _sourced_schema(child, graph_output),
        )
    _refuse_unbound_iteration(node, child, reached_by)


def _carried_names(node: SubworkflowNodeV3) -> frozenset[str]:
    """Every order of the child that a round hands to the round after it.

    A carried order is not missing from the boundary when the node does not bind
    it: the round before supplies it, and its seed supplies the first. It is the
    one child order whose value never comes from the parent graph directly.
    """
    if node.iterate is None:
        return frozenset()
    return frozenset(entry.name for entry in node.iterate.carry)


def _refuse_unbound_iteration(
    node: SubworkflowNodeV3, child: WorkflowGraphV3, reached_by: ReachedBy
) -> None:
    """Whether the child can answer the green condition and take the handover.

    The document already refused what it could judge alone. What only the child
    knows is whether the result the condition reads exists at all, and whether the
    order a round hands on is one the next round accepts under the schema revision
    both levels agreed. The refusals stay the boundary's own: "the schema
    revisions disagree" must not gain a third name.
    """
    if node.iterate is None:
        return
    green = _child_result(
        node, child, reached_by, "the green condition", node.iterate.until.output
    )
    _refuse_differing_schema(
        node,
        reached_by,
        f"green condition {node.iterate.until.output!r}",
        node.iterate.until.schema_reference,
        _sourced_schema(child, green),
    )
    for entry in node.iterate.carry:
        handed_on = _child_result(
            node, child, reached_by, f"carry {entry.name!r}", entry.from_output
        )
        _refuse_differing_schema(
            node,
            reached_by,
            f"carry {entry.name!r}",
            _sourced_schema(child, handed_on),
            _child_order(node, child, reached_by, entry.name).schema_reference,
        )


def _child_result(
    node: SubworkflowNodeV3,
    child: WorkflowGraphV3,
    reached_by: ReachedBy,
    subject: str,
    name: str,
) -> GraphOutput:
    for declared in child.graph_outputs:
        if declared.name == name:
            return declared
    raise _refuse(
        SubworkflowBindingRefusalReason.MISSING_OUTPUT_BINDING,
        node,
        reached_by,
        f"{subject} reads {name!r}, which the child declares as no result of its own",
    )


def _child_order(
    node: SubworkflowNodeV3, child: WorkflowGraphV3, reached_by: ReachedBy, name: str
) -> GraphInput:
    for declared in child.graph_inputs:
        if declared.name == name:
            return declared
    raise _refuse(
        SubworkflowBindingRefusalReason.MISSING_INPUT_BINDING,
        node,
        reached_by,
        f"carry {name!r} is handed to the child, which declares no such order",
    )


def _refuse_unmatched_names(
    node: SubworkflowNodeV3,
    reached_by: ReachedBy,
    subject: str,
    bound_names: Sequence[str],
    child_names: Sequence[str],
    missing: SubworkflowBindingRefusalReason,
    extra: SubworkflowBindingRefusalReason,
) -> None:
    for name in child_names:
        if name not in bound_names:
            raise _refuse(
                missing,
                node,
                reached_by,
                f"the child declares {subject} {name!r}, which this node does not bind",
            )
    for name in bound_names:
        if name not in child_names:
            raise _refuse(
                extra,
                node,
                reached_by,
                f"this node binds {subject} {name!r}, which the child does not declare",
            )


def _refuse_differing_schema(
    node: SubworkflowNodeV3,
    reached_by: ReachedBy,
    subject: str,
    bound: VersionedReference,
    declared: VersionedReference,
) -> None:
    """One revision is one type, whichever entry name each side reaches it under."""
    if bound.revision == declared.revision:
        return
    raise _refuse(
        SubworkflowBindingRefusalReason.SCHEMA_REVISION_MISMATCH,
        node,
        reached_by,
        f"{subject} binds schema {bound.ref}@{bound.revision}, while the child "
        f"declares {declared.ref}@{declared.revision}",
    )


def _named[Declared: (NodeInput, NodeOutput, GraphInput)](
    declared: Sequence[Declared], name: str
) -> Declared:
    return next(entry for entry in declared if entry.name == name)


def _proven_schema(
    node: SubworkflowNodeV3,
    reached_by: ReachedBy,
    bound: NodeInput,
    graph: WorkflowGraphV3,
) -> VersionedReference:
    """The schema revision one bound input proves, or a refusal that it proves none.

    A child's graph input demands a typed value, and exactly two declarations state
    the schema revision of what they carry: an upstream node output, and an order
    the parent graph itself was started with. An authored literal, a terminal
    receipt and a context entry each declare none, so binding one to a typed graph
    input would assert a type nothing in the document states.

    Passing an order down is what makes one published workflow reusable through a
    whole nesting: the order a run supplies at the top reaches the node that reads
    it, however deep it sits, under the schema revision every level agreed on.
    """
    source = bound.source
    if isinstance(source, NodeOutputSource):
        return _named(graph.node(source.node).outputs, source.output).schema_reference
    if isinstance(source, GraphInputSource):
        return _named(graph.graph_inputs, source.graph_input).schema_reference
    raise _refuse(
        SubworkflowBindingRefusalReason.UNPROVEN_INPUT_SCHEMA,
        node,
        reached_by,
        f"input {bound.name!r} proves no schema revision, so it cannot bind a "
        "typed graph input of the child",
    )


def _sourced_schema(
    child: WorkflowGraphV3, graph_output: GraphOutput
) -> VersionedReference:
    source = graph_output.source
    return _named(child.node(source.node).outputs, source.output).schema_reference
