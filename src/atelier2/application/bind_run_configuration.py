from __future__ import annotations

from typing import assert_never

from atelier2.application.resolve_references import (
    BoundChildren,
    bound_children,
    declared_through,
    resolve_declared_reference,
)
from atelier2.contracts.agents import AgentBindingSetHash
from atelier2.contracts.run_configuration_v3 import (
    DeclaredReference,
    ReferenceRefusal,
    ReferenceResolutionRefused,
    ResolvedReference,
    RunConfigurationRevision,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflow_bindings_v3 import SubworkflowBinding
from atelier2.contracts.workflows_v3 import WorkflowGraphV3
from atelier2.ports.published_revisions import PublishedRevisionRegistry


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
    children = bound_children(binding)
    resolutions = tuple(
        _resolve(declared, registry, children)
        for declared in declared_through(document, binding)
    )
    return RunConfigurationRevision(
        workflow_revision_hash, binding_set_hash, resolutions
    )


def _resolve(
    declared: DeclaredReference,
    registry: PublishedRevisionRegistry,
    children: BoundChildren,
) -> ResolvedReference:
    """The binding's half of the shared resolution: the first refusal ends it."""
    match resolve_declared_reference(declared, registry, children):
        case ResolvedReference() as resolved:
            return resolved
        case ReferenceRefusal() as refusal:
            raise ReferenceResolutionRefused(refusal)
        case _ as unreachable:
            assert_never(unreachable)
