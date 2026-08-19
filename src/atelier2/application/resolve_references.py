"""Where one declared reference of a V3 document lands, or why it lands nowhere.

Two callers need the same answer and disagree only about what to do with it: the
run-configuration binding refuses the whole snapshot at the first reference that
does not resolve, while the composed preview names every one of them and keeps
drawing. Deciding it twice would let a preview and a binding disagree about what
resolved, so the decision lives here and each caller acts on it.

A `workflow` reference is answered from the subworkflow binding rather than the
registry: that binder already resolved the child, read its bytes and proved its
identity, and a second registry answer could contradict it with no arbiter.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import assert_never

from atelier2.contracts.adapter_operations_v3 import (
    AdapterOperationRefused,
    read_adapter_operation_document,
)
from atelier2.contracts.budgets_v3 import (
    BudgetRevisionRefused,
    read_budget_revision_document,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_configuration_v3 import (
    DeclaredReference,
    ReferenceChain,
    ReferenceRefusal,
    ReferenceRefusalReason,
    ResolvedReference,
    declared_references,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.schemas_v3 import SchemaRefused, read_schema_document
from atelier2.contracts.tool_grants_v3 import (
    ToolGrantRefused,
    read_tool_grant_document,
)
from atelier2.contracts.workflow_bindings_v3 import BoundSubworkflow, SubworkflowBinding
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionResolver,
)

type ReferenceResolution = ResolvedReference | ReferenceRefusal
type BoundChildren = Mapping[tuple[str | None, ReferenceChain], WorkflowRevisionHash]


def declared_through(
    document: WorkflowGraphV3, binding: SubworkflowBinding
) -> Iterator[DeclaredReference]:
    """The document's own references, then those of every child it reuses."""
    yield from declared_references(document)
    yield from _declared_in_children(binding.subworkflows, ())


def bound_children(binding: SubworkflowBinding) -> BoundChildren:
    """Which child revision the subworkflow binder bound at each node of each chain."""
    return dict(_bound_children(binding.subworkflows, ()))


def resolve_declared_reference(
    declared: DeclaredReference,
    resolver: PublishedRevisionResolver,
    children: BoundChildren,
) -> ReferenceResolution:
    """The published revision one declared reference binds, or the named refusal."""
    if declared.kind is RevisionKind.WORKFLOW:
        return _bound_child(declared, children)
    try:
        revision_hash = PublishedRevisionHash(declared.reference.revision)
    except ValueError:
        return _refusal(
            ReferenceRefusalReason.MALFORMED_REVISION,
            declared,
            "a pinned revision must be 64 lowercase hexadecimal characters",
        )
    resolved = resolver.resolve(declared.kind, revision_hash)
    match resolved:
        case PublishedRevisionFound(revision):
            if revision.kind is not declared.kind:
                return _refusal(
                    ReferenceRefusalReason.REVISION_KIND_MISMATCH,
                    declared,
                    f"the registry answered with a {revision.kind.value} revision",
                )
            if revision.revision_hash != revision_hash:
                return _refusal(
                    ReferenceRefusalReason.RESOLVED_REVISION_MISMATCH,
                    declared,
                    f"the registry answered with revision {revision.revision_hash.value}",
                )
            unusable = _unreadable_document(declared, revision)
            if unusable is not None:
                return unusable
            return ResolvedReference(
                declared.site, declared.kind, declared.reference, revision_hash
            )
        case PublishedRevisionMissing():
            return _refusal(
                ReferenceRefusalReason.UNPUBLISHED_REVISION,
                declared,
                f"no published {declared.kind.value} revision carries this hash",
            )
        case _ as unreachable:
            assert_never(unreachable)


def _unreadable_document(
    declared: DeclaredReference, revision: PublishedRevision
) -> ReferenceRefusal | None:
    """Whether a revision resolved to bytes that are not the thing it was read as.

    Most kinds resolve on identity alone, because nothing here knows what their
    bytes must say. Four kinds this product must read to do its job at all are
    the exception, and the reference that pins them is where the reading belongs:
    a `schema`, which a value is read against, a `tool` grant, whose capability
    decides what an attempt is going to redeem, a `budget_policy`, whose bounds
    decide how far an attempt may run, and an `adapter_operation`, whose name
    decides which effect an Action performs. Each is refused here rather than at
    the attempt, because a run that started under it has already told its author
    it would be honoured -- and for a budget that promise is the whole point: an
    unreadable one would leave an attempt unbounded while its author reads a
    bound.
    """
    if declared.kind is RevisionKind.SCHEMA:
        verdict = read_schema_document(revision.document)
        if isinstance(verdict, SchemaRefused):
            return _refusal(
                ReferenceRefusalReason.UNUSABLE_SCHEMA_DOCUMENT,
                declared,
                "the published revision is not a schema this product enforces "
                f"({verdict})",
            )
        return None
    if declared.kind is RevisionKind.TOOL:
        grant = read_tool_grant_document(revision.document)
        if isinstance(grant, ToolGrantRefused):
            return _refusal(
                ReferenceRefusalReason.UNREDEEMABLE_TOOL_GRANT,
                declared,
                "the published revision is not a tool grant this runtime "
                f"redeems ({grant})",
            )
        return None
    if declared.kind is RevisionKind.BUDGET_POLICY:
        budget = read_budget_revision_document(revision.document)
        if isinstance(budget, BudgetRevisionRefused):
            return _refusal(
                ReferenceRefusalReason.UNUSABLE_BUDGET_DOCUMENT,
                declared,
                f"the published revision bounds no attempt this runtime runs ({budget})",
            )
        return None
    if declared.kind is RevisionKind.ADAPTER_OPERATION:
        operation = read_adapter_operation_document(revision.document)
        if isinstance(operation, AdapterOperationRefused):
            return _refusal(
                ReferenceRefusalReason.UNUSABLE_ADAPTER_OPERATION,
                declared,
                "the published revision is not an adapter operation this "
                f"runtime performs ({operation})",
            )
        return None
    return None


def _bound_child(
    declared: DeclaredReference, children: BoundChildren
) -> ReferenceResolution:
    """The child revision the subworkflow binder bound for this exact reference."""
    reached_by = declared.site.chain + (declared.reference,)
    child_revision_hash = children.get((declared.site.node, reached_by))
    if child_revision_hash is None:
        return _refusal(
            ReferenceRefusalReason.UNBOUND_WORKFLOW_REFERENCE,
            declared,
            "no bound child of this document was reached through this reference",
        )
    return ResolvedReference(
        declared.site,
        declared.kind,
        declared.reference,
        PublishedRevisionHash(child_revision_hash.value),
    )


def _declared_in_children(
    bound: tuple[BoundSubworkflow, ...], chain: ReferenceChain
) -> Iterator[DeclaredReference]:
    for child in bound:
        reached_by = chain + (child.reference,)
        yield from declared_references(child.child, reached_by)
        yield from _declared_in_children(child.children, reached_by)


def _bound_children(
    bound: tuple[BoundSubworkflow, ...], chain: ReferenceChain
) -> Iterator[tuple[tuple[str | None, ReferenceChain], WorkflowRevisionHash]]:
    for child in bound:
        reached_by = chain + (child.reference,)
        yield (child.node_id, reached_by), child.child_revision_hash
        yield from _bound_children(child.children, reached_by)


def _refusal(
    reason: ReferenceRefusalReason, declared: DeclaredReference, detail: str
) -> ReferenceRefusal:
    return ReferenceRefusal(
        reason, declared.site, declared.kind, declared.reference, detail
    )
