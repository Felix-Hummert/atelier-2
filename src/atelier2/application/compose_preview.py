"""Deriving the one composed preview decision 0006 makes the truth before a start.

This is a projection, not a second opinion. The graph is the parsed document, the
capability demands are exactly what the executability record derives, the verdict
is exactly what it decides, and the registries answer through the same resolution
the run-configuration binding uses. Nothing here re-derives any of it.

"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass

from atelier2.application.resolve_references import (
    ReferenceResolution,
    declared_through,
    resolve_declared_reference,
)
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
    PreviewGraphInput,
    PreviewIteration,
    PreviewNode,
    PreviewNodeKind,
    PreviewRole,
    UnknownSkillGrants,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.run_configuration_v3 import (
    DeclaredReference,
    ReferenceChain,
    ReferenceRefusal,
    ResolvedReference,
    declared_references,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflow_bindings_v3 import SubworkflowBinding
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
    resolutions = {
        declared: resolve_declared_reference(declared, registry)
        for declared in declared_through(document, subworkflows)
    }
    withdrawn = skills.with_unresolved(_unresolved_skills(resolutions))
    unread = withdrawn.unread(_resolved_skills(resolutions))
    readable = withdrawn.with_unread(unread)
    requirements = required_capabilities(
        document, subworkflows, agent_bindings, readable
    )
    executability = decide_executability(requirements, attested)
    unproven = (
        executability.refusals if isinstance(executability, NotExecutable) else ()
    )
    derivation = _Derivation(
        requirements,
        unproven,
        {binding.role.value: binding for binding in agent_bindings},
        resolutions,
        frozenset(unread),
    )
    graph = _preview_graph(document, (), derivation)
    return ComposedPreview(workflow_revision_hash, configuration, graph, executability)


def _unresolved_skills(
    resolutions: Mapping[DeclaredReference, ReferenceResolution],
) -> tuple[str, ...]:
    """Every pinned skill revision that resolves to nothing, so nothing was read.

    A withdrawn skill would otherwise end the drawing where it is read for the
    grants it carries, and a preview exists to name what is missing rather than to
    stop at the first thing that is.
    """
    return tuple(
        declared.reference.revision
        for declared, resolution in resolutions.items()
        if declared.kind is RevisionKind.SKILL
        and isinstance(resolution, ReferenceRefusal)
    )


def _resolved_skills(
    resolutions: Mapping[DeclaredReference, ReferenceResolution],
) -> tuple[str, ...]:
    """Every pinned skill revision the registry does carry, whoever read it.

    These are the skills a reading is allowed to be incomplete about: the revision
    exists, so its grants are whatever it installs, and a preview that was handed no
    contents for one has to say that rather than end at it.
    """
    return tuple(
        declared.reference.revision
        for declared, resolution in resolutions.items()
        if declared.kind is RevisionKind.SKILL
        and isinstance(resolution, ResolvedReference)
    )


@dataclass(frozen=True)
class _Derivation:
    """What every graph of one preview reads while it is being drawn."""

    requirements: tuple[CapabilityRequirement, ...]
    unproven: tuple[ExecutabilityRefusal, ...]
    roles: Mapping[str, ResolvedAgentBinding]
    resolutions: Mapping[DeclaredReference, ReferenceResolution]
    unread_skills: frozenset[str]


def _preview_graph(
    graph: WorkflowGraphV3,
    chain: ReferenceChain,
    derivation: _Derivation,
) -> ComposedPreviewGraph:
    resolved: list[ResolvedReference] = []
    unresolved: list[ReferenceRefusal] = []
    unknown_grants: list[UnknownSkillGrants] = []
    for declared in declared_references(graph, chain):
        if declared.kind is RevisionKind.WORKFLOW:
            continue
        resolution = derivation.resolutions[declared]
        if isinstance(resolution, ResolvedReference):
            resolved.append(resolution)
            if (
                declared.kind is RevisionKind.SKILL
                and declared.reference.revision in derivation.unread_skills
            ):
                unknown_grants.append(
                    UnknownSkillGrants(declared.site, declared.reference)
                )
        else:
            unresolved.append(resolution)
    return ComposedPreviewGraph(
        tuple(
            PreviewGraphInput(entry.name, entry.schema_reference)
            for entry in graph.graph_inputs
        ),
        tuple(_preview_node(node, graph, chain, derivation) for node in graph.nodes),
        tuple(
            PreviewEdge(dependency, node.id)
            for node in graph.nodes
            for dependency in node.depends_on
        ),
        tuple(resolved),
        tuple(unresolved),
        tuple(unknown_grants),
    )


def _preview_node(
    node: WorkflowNodeV3,
    graph: WorkflowGraphV3,
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
        _preview_iteration(node),
    )


def _distinct[Entry: Hashable](entries: Iterable[Entry]) -> tuple[Entry, ...]:
    return tuple(dict.fromkeys(entries))


def _preview_iteration(node: WorkflowNodeV3) -> PreviewIteration | None:
    """What a surface must say about a node that repeats, or nothing if it does not."""
    if not isinstance(node, SubworkflowNodeV3) or node.iterate is None:
        return None
    return PreviewIteration(
        node.iterate.maximum_rounds,
        node.iterate.until.output,
        node.iterate.until.schema_reference,
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
