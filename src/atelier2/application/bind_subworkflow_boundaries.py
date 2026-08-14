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
) -> SubworkflowBinding:
    """Bind every subworkflow node of one document against real child content.

    Each node's declared inputs and outputs must match the published child's
    `graph_inputs` and `graph_outputs` one to one by name and schema revision, no
    chain may run deeper than the attested `maximum_nesting_depth`, and any
    deviation raises `SubworkflowBindingRefused` naming the node and the chain.
    """
    if maximum_nesting_depth < 0:
        raise ValueError("the attested nesting depth bound must not be negative")
    binder = _Binder(resolver, parser, maximum_nesting_depth)
    return SubworkflowBinding(binder.bind_nodes(document, _Chain()))


@dataclass(frozen=True)
class _Chain:
    """How one child revision was reached: the references, and what they resolved to."""

    references: ReachedBy = ()
    revision_hashes: tuple[WorkflowRevisionHash, ...] = ()

    def reaching(self, reference: VersionedReference) -> ReachedBy:
        return self.references + (reference,)

    def extended(
        self, reference: VersionedReference, revision_hash: WorkflowRevisionHash
    ) -> _Chain:
        return _Chain(self.reaching(reference), self.revision_hashes + (revision_hash,))


@dataclass(frozen=True)
class _Binder:
    resolver: PublishedWorkflowResolver
    parser: WorkflowDocumentParser
    maximum_nesting_depth: int

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
        revision = self._resolve(node, reached_by)
        child = self._parse(node, reached_by, revision.document)
        _refuse_recursion(node, reached_by, chain, revision.revision_hash)
        self._refuse_excess_depth(node, reached_by)
        _refuse_broken_boundary(node, graph, child, reached_by)
        return BoundSubworkflow(
            node.id,
            node.workflow,
            revision.revision_hash,
            child,
            self.bind_nodes(
                child, chain.extended(node.workflow, revision.revision_hash)
            ),
        )

    def _resolve(
        self, node: SubworkflowNodeV3, reached_by: ReachedBy
    ) -> WorkflowRevision:
        resolved = self.resolver.resolve(node.workflow)
        match resolved:
            case PublishedWorkflowFound(revision):
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


def _refuse_recursion(
    node: SubworkflowNodeV3,
    reached_by: ReachedBy,
    chain: _Chain,
    revision_hash: WorkflowRevisionHash,
) -> None:
    """No revision can carry its own hash, so a resolver contradicting that is loud."""
    if revision_hash in chain.revision_hashes:
        raise _refuse(
            SubworkflowBindingRefusalReason.RECURSIVE_WORKFLOW_REFERENCE,
            node,
            reached_by,
            "this chain resolves to a revision it already entered through",
        )


def _refuse_broken_boundary(
    node: SubworkflowNodeV3,
    graph: WorkflowGraphV3,
    child: WorkflowGraphV3,
    reached_by: ReachedBy,
) -> None:
    _refuse_unmatched_names(
        node,
        reached_by,
        "graph input",
        [entry.name for entry in node.inputs],
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
        bound_input = _named(node.inputs, graph_input.name)
        _refuse_differing_schema(
            node,
            reached_by,
            f"graph input {graph_input.name!r}",
            _upstream_schema(bound_input, graph),
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
    bound: VersionedReference | None,
    declared: VersionedReference,
) -> None:
    if bound is None or bound == declared:
        return
    raise _refuse(
        SubworkflowBindingRefusalReason.SCHEMA_REVISION_MISMATCH,
        node,
        reached_by,
        f"{subject} binds schema {bound.ref}@{bound.revision}, while the child "
        f"declares {declared.ref}@{declared.revision}",
    )


def _named[Declared: (NodeInput, NodeOutput)](
    declared: Sequence[Declared], name: str
) -> Declared:
    return next(entry for entry in declared if entry.name == name)


def _upstream_schema(
    bound: NodeInput, graph: WorkflowGraphV3
) -> VersionedReference | None:
    """The schema an input carries, where the document itself declares one."""
    source = bound.source
    if not isinstance(source, NodeOutputSource):
        return None
    return _named(graph.node(source.node).outputs, source.output).schema_reference


def _sourced_schema(
    child: WorkflowGraphV3, graph_output: GraphOutput
) -> VersionedReference:
    source = graph_output.source
    return _named(child.node(source.node).outputs, source.output).schema_reference
