from __future__ import annotations

from collections.abc import Iterator
from typing import assert_never

from atelier2.contracts.agents import AgentBindingSetHash
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_configuration_v3 import (
    DeclaredReference,
    ReferenceChain,
    ReferenceRefusal,
    ReferenceRefusalReason,
    ReferenceResolutionRefused,
    ResolvedReference,
    RunConfigurationRevision,
    SubworkflowResolution,
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
    refused naming the node, the field and the reference. Nothing durable is written
    here, so a refusal leaves no run behind rather than one to clean up.
    """
    resolutions = tuple(
        _resolve(declared, registry)
        for declared in _declared_through(document, binding)
    )
    return RunConfigurationRevision(
        workflow_revision_hash,
        binding_set_hash,
        resolutions,
        tuple(_subworkflow_resolutions(binding.subworkflows, ())),
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


def _subworkflow_resolutions(
    bound: tuple[BoundSubworkflow, ...], chain: ReferenceChain
) -> Iterator[SubworkflowResolution]:
    for child in bound:
        reached_by = chain + (child.reference,)
        yield SubworkflowResolution(
            child.node_id, reached_by, child.child_revision_hash
        )
        yield from _subworkflow_resolutions(child.children, reached_by)


def _resolve(
    declared: DeclaredReference, registry: PublishedRevisionRegistry
) -> ResolvedReference:
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
