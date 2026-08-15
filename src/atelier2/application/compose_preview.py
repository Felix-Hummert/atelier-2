"""Deriving the one composed preview decision 0006 makes the truth before a start.

This is a projection, not a second opinion. The graph is the parsed document, the
capability demands are exactly what the executability record derives, the verdict
is exactly what it decides, and the registries answer through the same resolution
the run-configuration binding uses. Nothing here re-derives any of it.

Where a slice before this one already ruled, its ruling is propagated rather than
softened: a child that does not bind is the subworkflow binding's refusal, and a
role bound to no agent-configuration revision is the executability record's. What
this decision does add is tolerance for the state decision 0006 exists for — a
revision that is publishable and not yet executable — so an unresolved reference
and an unproven capability are drawn instead of ending the drawing.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass

from atelier2.application.resolve_references import resolve_declared_reference
from atelier2.contracts.agents import ResolvedAgentBinding
from atelier2.contracts.capabilities_v3 import (
    AttestedCapabilities,
    CapabilityRequirement,
    ExecutabilityRefusal,
    NotExecutable,
    PublishedSkills,
    decide_executability,
    required_capabilities,
)
from atelier2.contracts.composed_preview_v3 import (
    ComposedPreview,
    ComposedPreviewGraph,
    ConfigurationBinding,
    PreviewEdge,
    PreviewNode,
    PreviewNodeKind,
    PreviewRole,
)
from atelier2.contracts.run_configuration_v3 import (
    ReferenceChain,
    ReferenceRefusal,
    ResolvedReference,
    declared_references,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflow_bindings_v3 import BoundSubworkflow, SubworkflowBinding
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    SubworkflowNodeV3,
    WorkflowGraphV3,
    WorkflowNodeV3,
)
from atelier2.ports.published_revisions import PublishedRevisionRegistry


def compose_preview(
    workflow_revision_hash: WorkflowRevisionHash,
    document: WorkflowGraphV3,
    subworkflows: SubworkflowBinding,
    agent_bindings: tuple[ResolvedAgentBinding, ...],
    skills: PublishedSkills,
    attested: AttestedCapabilities,
    registry: PublishedRevisionRegistry,
    configuration: ConfigurationBinding,
) -> ComposedPreview:
    """The whole renderable truth of one V3 revision, over the closure it reuses.

    Requirements are derived once for the closure and then hung on the node that
    demanded them, so a surface reads per-node demands without a second traversal
    and the verdict stays one decision about the whole run.
    """
    requirements = required_capabilities(document, subworkflows, agent_bindings, skills)
    executability = decide_executability(requirements, attested)
    unproven = (
        executability.refusals if isinstance(executability, NotExecutable) else ()
    )
    derivation = _Derivation(
        requirements,
        unproven,
        {binding.role.value: binding for binding in agent_bindings},
        registry,
    )
    graph = _preview_graph(document, subworkflows.subworkflows, (), derivation)
    return ComposedPreview(workflow_revision_hash, configuration, graph, executability)


@dataclass(frozen=True)
class _Derivation:
    """What every graph of one preview reads while it is being drawn."""

    requirements: tuple[CapabilityRequirement, ...]
    unproven: tuple[ExecutabilityRefusal, ...]
    roles: Mapping[str, ResolvedAgentBinding]
    registry: PublishedRevisionRegistry


def _preview_graph(
    graph: WorkflowGraphV3,
    bound: tuple[BoundSubworkflow, ...],
    chain: ReferenceChain,
    derivation: _Derivation,
) -> ComposedPreviewGraph:
    children = {child.node_id: child for child in bound}
    resolved: list[ResolvedReference] = []
    unresolved: list[ReferenceRefusal] = []
    for declared in declared_references(graph, chain):
        resolution = resolve_declared_reference(declared, derivation.registry)
        if isinstance(resolution, ResolvedReference):
            resolved.append(resolution)
        else:
            unresolved.append(resolution)
    return ComposedPreviewGraph(
        tuple(
            _preview_node(node, graph, children, chain, derivation)
            for node in graph.nodes
        ),
        tuple(
            PreviewEdge(dependency, node.id)
            for node in graph.nodes
            for dependency in node.depends_on
        ),
        tuple(resolved),
        tuple(unresolved),
    )


def _preview_node(
    node: WorkflowNodeV3,
    graph: WorkflowGraphV3,
    children: dict[str, BoundSubworkflow],
    chain: ReferenceChain,
    derivation: _Derivation,
) -> PreviewNode:
    demands = _distinct(
        requirement
        for requirement in derivation.requirements
        if _demanded_by(requirement, node, chain)
    )
    demanded = set(demands)
    return PreviewNode(
        node.id,
        PreviewNodeKind.of(node),
        graph.join_of(node.id),
        _preview_role(node, derivation),
        node.mode if isinstance(node, AgentNodeV3) else None,
        demands,
        _distinct(
            refusal
            for refusal in derivation.unproven
            if refusal.requirement in demanded
        ),
        _preview_child(node, children, chain, derivation),
    )


def _distinct[Entry: Hashable](entries: Iterable[Entry]) -> tuple[Entry, ...]:
    """The entries in the order they were derived, each of them once.

    A node demands a capability or waits for one; how many times a requirement
    entered is not a fact the preview claims. Two subworkflow nodes binding the
    same child revision reach that child through one and the same reference, so
    its demands are derived once per node that binds it and would otherwise be
    drawn twice on each node of the child.
    """
    return tuple(dict.fromkeys(entries))


def _preview_child(
    node: WorkflowNodeV3,
    children: dict[str, BoundSubworkflow],
    chain: ReferenceChain,
    derivation: _Derivation,
) -> ComposedPreviewGraph | None:
    if not isinstance(node, SubworkflowNodeV3):
        return None
    child = children[node.id]
    return _preview_graph(
        child.child, child.children, chain + (child.reference,), derivation
    )


def _demanded_by(
    requirement: CapabilityRequirement, node: WorkflowNodeV3, chain: ReferenceChain
) -> bool:
    """Whether one requirement of the closure entered at this node of this graph.

    A node's own requirements carry the chain its graph was reached by. The one
    exception is a grant a bound skill carries: it enters through that skill, so it
    names the node's chain extended by the skill the node itself declares.
    """
    if requirement.site.node != node.id:
        return False
    if requirement.site.chain == chain:
        return True
    skills = node.skills if isinstance(node, AgentNodeV3) else ()
    return requirement.site.chain in {chain + (skill,) for skill in skills}


def _preview_role(node: WorkflowNodeV3, derivation: _Derivation) -> PreviewRole | None:
    """The provider, model and configuration revision one agent node is bound to.

    The lookup is total because the executability record already refused every
    agent node whose role the run binds to no configuration revision, and it ran
    before this projection did.
    """
    if not isinstance(node, AgentNodeV3):
        return None
    binding = derivation.roles[node.role]
    return PreviewRole(
        node.role,
        binding.auth_profile.provider_id.value,
        binding.configuration.model,
        binding.configuration.revision_hash.value,
    )
