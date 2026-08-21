from __future__ import annotations

from typing import assert_never

from atelier2.application.resolve_references import (
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
from atelier2.ports.published_revisions import PublishedRevisionResolver


def bind_run_configuration(
    workflow_revision_hash: WorkflowRevisionHash,
    document: WorkflowGraphV3,
    binding: SubworkflowBinding,
    binding_set_hash: AgentBindingSetHash,
    resolver: PublishedRevisionResolver,
) -> RunConfigurationRevision:
    resolutions = tuple(
        _resolve(declared, resolver) for declared in declared_through(document, binding)
    )
    return RunConfigurationRevision(
        workflow_revision_hash, binding_set_hash, resolutions
    )


def _resolve(
    declared: DeclaredReference,
    resolver: PublishedRevisionResolver,
) -> ResolvedReference:
    match resolve_declared_reference(declared, resolver):
        case ResolvedReference() as resolved:
            return resolved
        case ReferenceRefusal() as refusal:
            raise ReferenceResolutionRefused(refusal)
        case _ as unreachable:
            assert_never(unreachable)
