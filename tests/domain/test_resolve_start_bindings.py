"""`resolve_start_bindings`: the one binding decision a V2 or V3 start makes."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from atelier2.application.resolve_start_bindings import (
    AuthProfileMissingForConfiguration,
    agent_role_completeness_refusal,
    cast_unbound_roles,
    resolve_start_bindings,
)
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import (
    OccupancyBinding,
    OccupancyRevision,
    ProjectId,
)
from atelier2.contracts.workflows import AgentNodeV2, SubworkflowNode, WorkflowGraphV2
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    BindingConstraint,
    WorkflowGraphV3,
)
from atelier2.ports.agent_executions import (
    AgentExecutorKey,
    AgentExecutorRegistration,
    AgentExecutorRegistry,
    AgentExecutorV2,
)
from atelier2.ports.durable_runs import (
    DurableAgentConfigurationRevisionMissing,
    DurableAgentExecutorBindingUnavailable,
    DurableAgentExecutorCapabilityUnavailable,
    DurableBindingConstraintRefused,
    DurableInvalidAgentBindings,
)


@dataclass(frozen=True)
class FakeExecutorFactory:
    """The narrow shape `AgentExecutorRegistry` reads from a factory, faked."""

    provider: str
    revision: str = "v1"
    capabilities: frozenset[AgentExecutionCapability] = frozenset(
        {AgentExecutionCapability.HEADLESS}
    )

    @property
    def key(self) -> AgentExecutorKey:
        return AgentExecutorKey(
            ProviderId(self.provider), AgentExecutorRevision(self.revision)
        )

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return AgentExecutorOperationalIdentity(f"{self.provider}-operation")

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        return self.capabilities

    def open(self) -> AgentExecutorV2:
        raise AssertionError("resolving a binding never opens an executor")


BindingAnswer = tuple[AgentConfigurationRevision, AuthProfileRevision] | None


@dataclass
class ScriptedBindingReads:
    """Answers `agent_configuration_revision` from a fixed hash-keyed table.

    A hash mapped to `AuthProfileMissingForConfiguration` raises it, exactly as
    a real implementation does for a configuration whose own auth profile the
    store no longer holds.
    """

    answers: dict[
        AgentConfigurationRevisionHash,
        BindingAnswer | AuthProfileMissingForConfiguration,
    ]
    calls: list[AgentConfigurationRevisionHash] = field(default_factory=list)

    def agent_configuration_revision(
        self, revision_hash: AgentConfigurationRevisionHash
    ) -> BindingAnswer:
        self.calls.append(revision_hash)
        answer = self.answers.get(revision_hash)
        if isinstance(answer, AuthProfileMissingForConfiguration):
            raise answer
        return answer


def _auth(profile_id: str = "max") -> AuthProfileRevision:
    return AuthProfileRevision(
        profile_id, 1, ProviderId("exact"), AuthMode.SUBSCRIPTION
    )


def _configuration(
    auth: AuthProfileRevision,
    *,
    model: str = "opus",
    executor_revision: str = "v1",
    capability: AgentExecutionCapability = AgentExecutionCapability.HEADLESS,
) -> AgentConfigurationRevision:
    return AgentConfigurationRevision(
        model,
        auth.revision_hash,
        AgentExecutorRevision(executor_revision),
        capability,
        AgentConfigurationRevisionFormatVersion.V2,
    )


def _registry(
    *factories: FakeExecutorFactory, unavailable: bool = False
) -> AgentExecutorRegistry:
    build = (
        AgentExecutorRegistration.unavailable
        if unavailable
        else AgentExecutorRegistration.startable
    )
    return AgentExecutorRegistry(tuple(build(factory) for factory in factories))


def _v2_graph(role: str = "builder") -> WorkflowGraphV2:
    """agent -> final: the shortest V2 graph, with one role to bind."""
    return WorkflowGraphV2(
        format_version=2,
        start="agent",
        nodes=(
            AgentNodeV2(id="agent", type="agent", role=role, job="Build", next="final"),
            SubworkflowNode(
                id="final",
                type="subworkflow",
                operation="add",
                operands=(2, 3),
                next=None,
            ),
        ),
    )


def _v3_graph(*, distinct_from: bool = False) -> WorkflowGraphV3:
    """implement -> merge: two roles, optionally held to distinct occupations."""
    return WorkflowGraphV3(
        format_version=3,
        name="Implement, then land",
        nodes=(
            AgentNodeV3(
                id="implement",
                type="agent",
                role="builder",
                mode="headless",
                instruction="Write the change.",
            ),
            AgentNodeV3(
                id="merge",
                type="agent",
                role="merger",
                mode="headless",
                instruction="Land the change.",
                depends_on=("implement",),
                binding_constraint=(
                    BindingConstraint(distinct_from="implement")
                    if distinct_from
                    else None
                ),
            ),
        ),
    )


def _occupancy(*bindings: tuple[str, str]) -> OccupancyRevision:
    """What the operator cast on this workflow, as the project keeps it."""
    return OccupancyRevision(
        ProjectId("atelier"),
        CatalogLineageId("b" * 64),
        1,
        tuple(
            OccupancyBinding(AgentRole(role), AgentConfigurationRevisionHash(hash_))
            for role, hash_ in bindings
        ),
    )


def test_occupancy_fills_only_declared_roles_the_caller_left_open() -> None:
    explicit = AgentConfigurationRevisionHash("a" * 64)
    occupied = AgentConfigurationRevisionHash("b" * 64)
    requested = AgentBindingSet((AgentBinding(AgentRole("builder"), explicit),))

    cast = cast_unbound_roles(
        _v3_graph(),
        requested,
        _occupancy(
            ("builder", occupied.value),
            ("merger", occupied.value),
            ("stranger", occupied.value),
        ),
    )

    assert cast == AgentBindingSet(
        (
            AgentBinding(AgentRole("builder"), explicit),
            AgentBinding(AgentRole("merger"), occupied),
        )
    )


def test_without_a_recommendation_the_requested_bindings_stand() -> None:
    requested = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), AgentConfigurationRevisionHash("a" * 64)),)
    )

    assert cast_unbound_roles(_v3_graph(), requested, None) == requested


@pytest.mark.parametrize(
    "bindings",
    [
        AgentBindingSet(()),
        AgentBindingSet(
            (
                AgentBinding(
                    AgentRole("reviewer"), AgentConfigurationRevisionHash("a" * 64)
                ),
            )
        ),
    ],
    ids=["missing_role", "unknown_role"],
)
def test_role_completeness_refuses_before_any_read(bindings: AgentBindingSet) -> None:
    reads = ScriptedBindingReads({})

    result = resolve_start_bindings(_v2_graph(), bindings, reads, _registry())

    assert result == DurableInvalidAgentBindings()
    assert reads.calls == []


def test_agent_role_completeness_refusal_is_the_same_decision_standalone() -> None:
    graph = _v2_graph()
    complete = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), AgentConfigurationRevisionHash("a" * 64)),)
    )

    assert agent_role_completeness_refusal(graph, complete) is None
    assert agent_role_completeness_refusal(graph, AgentBindingSet(())) == (
        DurableInvalidAgentBindings()
    )


def test_missing_agent_configuration_refuses() -> None:
    missing_hash = AgentConfigurationRevisionHash("a" * 64)
    bindings = AgentBindingSet((AgentBinding(AgentRole("builder"), missing_hash),))
    reads = ScriptedBindingReads({})

    result = resolve_start_bindings(_v2_graph(), bindings, reads, _registry())

    assert result == DurableAgentConfigurationRevisionMissing()


def test_missing_auth_profile_fails_loud_rather_than_refusing() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads(
        {
            configuration.revision_hash: AuthProfileMissingForConfiguration(
                auth.revision_hash
            )
        }
    )

    with pytest.raises(AuthProfileMissingForConfiguration):
        resolve_start_bindings(_v2_graph(), bindings, reads, _registry())


def test_unregistered_executor_refuses() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})

    result = resolve_start_bindings(_v2_graph(), bindings, reads, _registry())

    assert result == DurableAgentExecutorBindingUnavailable()


def test_undeclared_capability_refuses() -> None:
    auth = _auth()
    configuration = _configuration(auth, capability=AgentExecutionCapability.HEADLESS)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    registry = _registry(
        FakeExecutorFactory(
            "exact",
            capabilities=frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS}),
        )
    )

    result = resolve_start_bindings(_v2_graph(), bindings, reads, registry)

    assert result == DurableAgentExecutorCapabilityUnavailable()


def test_declared_but_unstartable_executor_refuses() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    registry = _registry(FakeExecutorFactory("exact"), unavailable=True)

    result = resolve_start_bindings(_v2_graph(), bindings, reads, registry)

    assert result == DurableAgentExecutorBindingUnavailable()


def test_resolves_every_binding_for_a_v2_graph() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    registry = _registry(FakeExecutorFactory("exact"))

    result = resolve_start_bindings(_v2_graph(), bindings, reads, registry)

    assert result == (ResolvedAgentBinding(AgentRole("builder"), configuration, auth),)


def test_first_refusal_in_request_binding_order_wins() -> None:
    """Two bindings can both refuse; the first in the set's own order does."""
    auth = _auth()
    missing_hash = AgentConfigurationRevisionHash("a" * 64)
    merger_configuration = _configuration(auth, model="sonnet")
    # `AgentBindingSet` orders its bindings by role, so "builder" resolves
    # before "merger" -- and "builder" is the one whose configuration is
    # missing, while "merger" would refuse too (its executor is never
    # registered) if it were ever reached.
    bindings = AgentBindingSet(
        (
            AgentBinding(AgentRole("merger"), merger_configuration.revision_hash),
            AgentBinding(AgentRole("builder"), missing_hash),
        )
    )
    reads = ScriptedBindingReads(
        {merger_configuration.revision_hash: (merger_configuration, auth)}
    )

    result = resolve_start_bindings(_v3_graph(), bindings, reads, _registry())

    assert result == DurableAgentConfigurationRevisionMissing()
    assert reads.calls == [missing_hash]


def test_distinct_from_refuses_only_after_both_bindings_resolve() -> None:
    auth = _auth()
    configuration = _configuration(auth)
    bindings = AgentBindingSet(
        (
            AgentBinding(AgentRole("builder"), configuration.revision_hash),
            AgentBinding(AgentRole("merger"), configuration.revision_hash),
        )
    )
    reads = ScriptedBindingReads({configuration.revision_hash: (configuration, auth)})
    registry = _registry(FakeExecutorFactory("exact"))

    result = resolve_start_bindings(
        _v3_graph(distinct_from=True), bindings, reads, registry
    )

    assert result == DurableBindingConstraintRefused("merge", "implement")
    assert reads.calls == [configuration.revision_hash, configuration.revision_hash]


def test_distinct_configurations_satisfy_distinct_from() -> None:
    auth = _auth()
    builder_configuration = _configuration(auth, model="opus")
    merger_configuration = _configuration(auth, model="sonnet")
    bindings = AgentBindingSet(
        (
            AgentBinding(AgentRole("builder"), builder_configuration.revision_hash),
            AgentBinding(AgentRole("merger"), merger_configuration.revision_hash),
        )
    )
    reads = ScriptedBindingReads(
        {
            builder_configuration.revision_hash: (builder_configuration, auth),
            merger_configuration.revision_hash: (merger_configuration, auth),
        }
    )
    registry = _registry(FakeExecutorFactory("exact"))

    result = resolve_start_bindings(
        _v3_graph(distinct_from=True), bindings, reads, registry
    )

    assert result == (
        ResolvedAgentBinding(AgentRole("builder"), builder_configuration, auth),
        ResolvedAgentBinding(AgentRole("merger"), merger_configuration, auth),
    )
