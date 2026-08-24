from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevisionHash,
    AgentRole,
)
from atelier2.contracts.orders import AuthoredOrderValue
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurableAgentConfigurationRevisionMissing,
    DurableAgentExecutorBindingUnavailable,
    DurableAgentExecutorCapabilityUnavailable,
    DurableAgentPlatformEffectUnreconcilable,
    DurableBindingConstraintRefused,
    DurableInvalidAgentBindings,
    DurablePublishedRunStarter,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunFormatNotExecutable,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableV3StartInputRefused,
    DurableWriteUnavailable,
    StartPublishedRunRequest,
    StartPublishedRunRequestV2,
    StartPublishedRunRequestV3,
    V3InputRefusal,
)
from atelier2.ports.durable_runs import (
    AuthoredOrder as PortAuthoredOrder,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)


@dataclass(frozen=True)
class RunCreated:
    run: AnyRun


@dataclass(frozen=True)
class RunExisting:
    run: AnyRun


@dataclass(frozen=True)
class RevisionMissing:
    pass


@dataclass(frozen=True)
class RunIdentityConflict:
    pass


@dataclass(frozen=True)
class RunFormatNotExecutable:
    pass


@dataclass(frozen=True)
class InvalidAgentBindings:
    pass


@dataclass(frozen=True)
class AgentConfigurationRevisionMissing:
    pass


@dataclass(frozen=True)
class AgentExecutorBindingUnavailable:
    pass


@dataclass(frozen=True)
class BindingConstraintRefused:
    """The two nodes named by a `distinct_from` resolved to the same occupation."""

    node: str
    distinct_from: str


@dataclass(frozen=True)
class RunInputRefused:
    """One order this start cannot honour, named by the input it is about."""

    name: str
    refusal: V3InputRefusal
    detail: str | None


@dataclass(frozen=True)
class AgentPlatformEffectUnreconcilable:
    """This deployment cannot safely redeem the named agent node's `open-pr` grant.

    The composed effect adapter cannot prove absence, and an agent-authored
    platform effect has no Action-only `WAITING_RECONCILIATION` resting place, so
    the run is refused before it advances (`#430`/`#431`).
    """

    node: str


type StartPublishedRunResult = (
    RunCreated
    | RunExisting
    | RevisionMissing
    | RunIdentityConflict
    | RunFormatNotExecutable
    | InvalidAgentBindings
    | AgentConfigurationRevisionMissing
    | AgentExecutorBindingUnavailable
    | BindingConstraintRefused
    | RunInputRefused
    | AgentPlatformEffectUnreconcilable
    | WriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class AuthoredAgentBinding:
    """One role and the agent-configuration revision an author bound it to."""

    role: str
    agent_configuration_revision_hash: str


@dataclass(frozen=True)
class AuthoredOrder:
    """An order as a caller supplies it: a name and where its value comes from."""

    name: str
    value: AuthoredOrderValue


def start_published_run(
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    bindings: tuple[AuthoredAgentBinding, ...] | None,
    starter: DurablePublishedRunStarter,
    orders: tuple[AuthoredOrder, ...] = (),
) -> StartPublishedRunResult:
    """Start one published revision, from the values an author supplied.

    Building the durable request is part of the decision, not a step before it: a
    role or a revision hash that is not one refuses the start, and it refuses it in
    the same vocabulary as everything else that can go wrong here. A caller above
    therefore has one outcome to read rather than an outcome and an exception.

    `bindings` is `None` for a revision that binds no agent, which is a different
    statement from binding an empty set. `orders` is the material the document
    declared as `graph_inputs`; the start pins each one to the schema the
    document named, so a caller does not repeat that hash.
    """
    request = _durable_request(run_id, revision_hash, bindings, orders)
    if request is None:
        return InvalidAgentBindings()
    result = starter.start_published(request)
    match result:
        case DurableRunCreated(run):
            return RunCreated(run)
        case DurableRunExisting(run):
            return RunExisting(run)
        case DurableRunRevisionMissing():
            return RevisionMissing()
        case DurableRunIdentityConflict():
            return RunIdentityConflict()
        case DurableRunFormatNotExecutable():
            return RunFormatNotExecutable()
        case DurableInvalidAgentBindings():
            return InvalidAgentBindings()
        case DurableAgentConfigurationRevisionMissing():
            return AgentConfigurationRevisionMissing()
        case DurableAgentExecutorBindingUnavailable():
            return AgentExecutorBindingUnavailable()
        case DurableAgentExecutorCapabilityUnavailable():
            return AgentExecutorBindingUnavailable()
        case DurableBindingConstraintRefused(node, distinct_from):
            return BindingConstraintRefused(node, distinct_from)
        case DurableAgentPlatformEffectUnreconcilable(node):
            return AgentPlatformEffectUnreconcilable(node)
        case DurableV3StartInputRefused(name, refusal, detail):
            return RunInputRefused(name, refusal, detail)
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _durable_request(
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    bindings: tuple[AuthoredAgentBinding, ...] | None,
    orders: tuple[AuthoredOrder, ...] = (),
) -> AnyStartPublishedRunRequest | None:
    if bindings is None:
        if orders:
            return None
        return StartPublishedRunRequest(run_id, revision_hash)
    try:
        binding_set = AgentBindingSet(
            tuple(
                AgentBinding(
                    AgentRole(binding.role),
                    AgentConfigurationRevisionHash(
                        binding.agent_configuration_revision_hash
                    ),
                )
                for binding in bindings
            )
        )
        if orders:
            return StartPublishedRunRequestV3(
                run_id,
                revision_hash,
                binding_set,
                orders=tuple(
                    PortAuthoredOrder(order.name, order.value) for order in orders
                ),
            )
        return StartPublishedRunRequestV2(run_id, revision_hash, binding_set)
    except (TypeError, ValueError):
        return None
