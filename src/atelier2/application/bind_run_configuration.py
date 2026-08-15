from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import assert_never

from atelier2.contracts.agents import AgentBindingSetHash
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_configuration_v3 import (
    DeclaredReference,
    ReferenceChain,
    ReferenceRefusal,
    ReferenceRefusalReason,
    ReferenceResolutionRefused,
    ResolvedReference,
    RunConfigurationRevision,
    declared_references,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflow_bindings_v3 import BoundSubworkflow, SubworkflowBinding
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionRegistry,
)

type BoundChildren = Mapping[tuple[str | None, ReferenceChain], WorkflowRevisionHash]


def bind_run_configuration(
    workflow_revision_hash: WorkflowRevisionHash,
    document: WorkflowGraphV3,
    binding: SubworkflowBinding,
    binding_set_hash: AgentBindingSetHash,
    registry: PublishedRevisionRegistry,
) -> RunConfigurationRevision:
    """Freeze one V3 document's complete resolution matrix before a run exists.

    Every versioned reference the document and each bound child declares must resolve
    to a published revision of the kind it is read under, or the whole binding is
    refused naming the node, the field and the reference. A `workflow` reference
    resolves through the subworkflow binding that already read that child, so the
    snapshot binds the child revision that binder proved rather than a second answer.
    Nothing durable is written here, so a refusal leaves no run behind rather than
    one to clean up.
    """
    children = dict(_bound_children(binding.subworkflows, ()))
    resolutions = tuple(
        _resolve(declared, registry, children)
        for declared in _declared_through(document, binding)
    )
    return RunConfigurationRevision(
        workflow_revision_hash, binding_set_hash, resolutions
    )


def _declared_through(
    document: WorkflowGraphV3, binding: SubworkflowBinding
) -> Iterator[DeclaredReference]:
    """The document's own references, then those of every child it reuses."""
    yield from declared_references(document)
    yield from _declared_in_children(binding.subworkflows, ())


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
    """Which child revision the subworkflow binder bound at each node of each chain."""
    for child in bound:
        reached_by = chain + (child.reference,)
        yield (child.node_id, reached_by), child.child_revision_hash
        yield from _bound_children(child.children, reached_by)


def _resolve(
    declared: DeclaredReference,
    registry: PublishedRevisionRegistry,
    children: BoundChildren,
) -> ResolvedReference:
    if declared.kind is RevisionKind.WORKFLOW:
        return _bound_child(declared, children)
    revision_hash = _revision_hash_of(declared)
    resolved = registry.resolve(declared.kind, revision_hash)
    match resolved:
        case PublishedRevisionFound(revision):
            if revision.kind is not declared.kind:
                raise _refuse(
                    ReferenceRefusalReason.REVISION_KIND_MISMATCH,
                    declared,
                    f"the registry answered with a {revision.kind.value} revision",
                )
            if revision.revision_hash != revision_hash:
                raise _refuse(
                    ReferenceRefusalReason.RESOLVED_REVISION_MISMATCH,
                    declared,
                    "the registry answered with revision "
                    f"{revision.revision_hash.value}",
                )
            return ResolvedReference(
                declared.site, declared.kind, declared.reference, revision_hash
            )
        case PublishedRevisionMissing():
            raise _refuse(
                ReferenceRefusalReason.UNPUBLISHED_REVISION,
                declared,
                f"no published {declared.kind.value} revision carries this hash",
            )
        case _ as unreachable:
            assert_never(unreachable)


def _bound_child(
    declared: DeclaredReference, children: BoundChildren
) -> ResolvedReference:
    """The child revision the subworkflow binder bound for this exact reference."""
    reached_by = declared.site.chain + (declared.reference,)
    child_revision_hash = children.get((declared.site.node, reached_by))
    if child_revision_hash is None:
        raise _refuse(
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


def _revision_hash_of(declared: DeclaredReference) -> PublishedRevisionHash:
    try:
        return PublishedRevisionHash(declared.reference.revision)
    except ValueError as error:
        raise _refuse(
            ReferenceRefusalReason.MALFORMED_REVISION,
            declared,
            "a pinned revision must be 64 lowercase hexadecimal characters",
        ) from error


def _refuse(
    reason: ReferenceRefusalReason, declared: DeclaredReference, detail: str
) -> ReferenceResolutionRefused:
    return ReferenceResolutionRefused(
        ReferenceRefusal(
            reason, declared.site, declared.kind, declared.reference, detail
        )
    )
