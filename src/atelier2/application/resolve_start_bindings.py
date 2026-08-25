from __future__ import annotations

from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AuthProfileRevisionHash,
    ResolvedAgentBinding,
)
from atelier2.contracts.host_configuration import OccupancyRevision
from atelier2.contracts.workflows import AgentNodeV2, WorkflowGraphV2
from atelier2.contracts.workflows_v3 import AgentNodeV3, WorkflowGraphV3
from atelier2.ports.agent_configurations import AgentConfigurationBindingReads
from atelier2.ports.agent_executions import AgentExecutorKey, AgentExecutorRegistry
from atelier2.ports.durable_runs import (
    DurableAgentConfigurationRevisionMissing,
    DurableAgentExecutorBindingUnavailable,
    DurableAgentExecutorCapabilityUnavailable,
    DurableBindingConstraintRefused,
    DurableInvalidAgentBindings,
)

type ResolveStartBindingsResult = (
    tuple[ResolvedAgentBinding, ...]
    | DurableInvalidAgentBindings
    | DurableAgentConfigurationRevisionMissing
    | DurableAgentExecutorBindingUnavailable
    | DurableAgentExecutorCapabilityUnavailable
    | DurableBindingConstraintRefused
)


class AuthProfileMissingForConfiguration(Exception):
    """A published agent configuration's own auth profile is absent.

    A configuration is never published without its auth profile hash already
    resolving (`AgentConfigurationCatalog.publish_agent_configuration_revision`
    refuses one that does not), so this is never a request a caller could have
    made honestly wrong. It is the durable store disagreeing with itself, and
    the one fail-loud word for that state: every caller of
    `resolve_start_bindings` maps it to its own "the store is corrupt" answer
    rather than a refusal a retry could fix.
    """

    def __init__(self, auth_profile_revision_hash: AuthProfileRevisionHash) -> None:
        super().__init__(
            "agent configuration auth profile "
            f"{auth_profile_revision_hash.value} is missing"
        )
        self.auth_profile_revision_hash = auth_profile_revision_hash


def declared_agent_roles(graph: WorkflowGraphV2 | WorkflowGraphV3) -> frozenset[str]:
    """Every role this document declares an `Agent` node for."""
    return frozenset(
        node.role
        for node in graph.nodes
        if isinstance(node, (AgentNodeV2, AgentNodeV3))
    )


def agent_role_completeness_refusal(
    graph: WorkflowGraphV2 | WorkflowGraphV3, agent_bindings: AgentBindingSet
) -> DurableInvalidAgentBindings | None:
    """Whether every declared `Agent` role has exactly one requested binding.

    Split out from `resolve_start_bindings` because it is that decision's
    first question and its only one that reads nothing: a caller with its own
    reason to ask it before paying for a read -- `DbosDurableRunStarter` asks
    it before its existing-run retry check, so an invalid request is refused
    by role before it is ever compared against a stored run -- may ask it
    alone. `resolve_start_bindings` still asks it first internally for every
    other caller, so the one answer has one owner either way.
    """
    requested_roles = {binding.role.value for binding in agent_bindings.bindings}
    if declared_agent_roles(graph) != requested_roles:
        return DurableInvalidAgentBindings()
    return None


def cast_unbound_roles(
    graph: WorkflowGraphV2 | WorkflowGraphV3,
    requested: AgentBindingSet,
    recommendation: OccupancyRevision | None,
) -> AgentBindingSet:
    """The requested bindings, with roles nobody bound taken from the occupancy.

    The precedence is fixed here because it is one decision: an explicit
    binding stands, a role the caller left open is filled from the served
    project's occupancy, and a role neither answers stays open -- so
    `agent_role_completeness_refusal` still refuses that start rather than a
    guess being started. The occupancy fills only roles this document declares:
    a recommendation older than the document cannot inject a role the document
    no longer has.
    """
    if recommendation is None:
        return requested
    bound = {binding.role.value for binding in requested.bindings}
    open_roles = declared_agent_roles(graph) - bound
    cast = tuple(
        AgentBinding(binding.role, binding.agent_configuration_revision_hash)
        for binding in recommendation.bindings
        if binding.role.value in open_roles
    )
    if not cast:
        return requested
    return AgentBindingSet(requested.bindings + cast)


def resolve_start_bindings(
    graph: WorkflowGraphV2 | WorkflowGraphV3,
    agent_bindings: AgentBindingSet,
    reads: AgentConfigurationBindingReads,
    registry: AgentExecutorRegistry,
) -> ResolveStartBindingsResult:
    """The one binding decision a V2 or V3 start makes, in its fixed order.

    Role completeness is judged first and alone
    (`agent_role_completeness_refusal`). Only once that holds does each
    requested binding resolve, in the binding set's own order, the first
    refusal winning: its agent configuration must exist
    (`DurableAgentConfigurationRevisionMissing`), its own auth profile must
    exist (a corrupt state, so it fails loud as
    `AuthProfileMissingForConfiguration` rather than joining the refusals a
    caller could act on), the executor it names must be registered
    (`DurableAgentExecutorBindingUnavailable`), must declare the requested
    capability (`DurableAgentExecutorCapabilityUnavailable`), and must be
    currently startable (`DurableAgentExecutorBindingUnavailable`). Only after
    every binding has resolved are a V3 graph's `distinct_from` constraints
    checked, last, because they compare resolutions nothing before this point
    has produced.
    """
    role_refusal = agent_role_completeness_refusal(graph, agent_bindings)
    if role_refusal is not None:
        return role_refusal

    resolved: list[ResolvedAgentBinding] = []
    for binding in agent_bindings.bindings:
        found = reads.agent_configuration_revision(
            binding.agent_configuration_revision_hash
        )
        if found is None:
            return DurableAgentConfigurationRevisionMissing()
        configuration, auth = found
        executor_key = AgentExecutorKey(
            auth.provider_id, configuration.executor_revision
        )
        if not registry.contains(executor_key):
            return DurableAgentExecutorBindingUnavailable()
        if configuration.requested_capability not in registry.declared_capabilities(
            executor_key
        ):
            return DurableAgentExecutorCapabilityUnavailable()
        if not registry.is_startable(executor_key, configuration.requested_capability):
            return DurableAgentExecutorBindingUnavailable()
        resolved.append(ResolvedAgentBinding(binding.role, configuration, auth))

    resolved_bindings = tuple(resolved)
    if isinstance(graph, WorkflowGraphV3):
        occupied = _refused_distinct_occupation(graph, resolved_bindings)
        if occupied is not None:
            return occupied
    return resolved_bindings


def _refused_distinct_occupation(
    graph: WorkflowGraphV3, resolved: tuple[ResolvedAgentBinding, ...]
) -> DurableBindingConstraintRefused | None:
    """Refuse a start whose `distinct_from` pair resolved to one occupation.

    The document already held the names. This is the binding seam: same
    configuration hash means the same agent sits on both nodes. Nothing
    about judgment is compared. No process has started.
    """

    by_role = {binding.role.value: binding for binding in resolved}
    for node in graph.nodes:
        if not isinstance(node, AgentNodeV3) or node.binding_constraint is None:
            continue
        other = graph.node(node.binding_constraint.distinct_from)
        assert isinstance(other, AgentNodeV3)
        left = by_role[node.role]
        right = by_role[other.role]
        if left.configuration.revision_hash == right.configuration.revision_hash:
            return DurableBindingConstraintRefused(
                node.id, node.binding_constraint.distinct_from
            )
    return None
