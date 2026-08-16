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
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurableAgentConfigurationRevisionMissing,
    DurableAgentExecutorBindingUnavailable,
    DurableAgentExecutorCapabilityUnavailable,
    DurableInvalidAgentBindings,
    DurablePublishedRunStarter,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunFormatNotExecutable,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableWriteUnavailable,
    StartPublishedRunRequest,
    StartPublishedRunRequestV2,
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


type StartPublishedRunResult = (
    RunCreated
    | RunExisting
    | RevisionMissing
    | RunIdentityConflict
    | RunFormatNotExecutable
    | InvalidAgentBindings
    | AgentConfigurationRevisionMissing
    | AgentExecutorBindingUnavailable
    | WriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class AuthoredAgentBinding:
    """One role and the agent-configuration revision an author bound it to."""

    role: str
    agent_configuration_revision_hash: str


def start_published_run(
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    bindings: tuple[AuthoredAgentBinding, ...] | None,
    starter: DurablePublishedRunStarter,
) -> StartPublishedRunResult:
    """Start one published revision, from the values an author supplied.

    Building the durable request is part of the decision, not a step before it: a
    role or a revision hash that is not one refuses the start, and it refuses it in
    the same vocabulary as everything else that can go wrong here. A caller above
    therefore has one outcome to read rather than an outcome and an exception.

    `bindings` is `None` for a revision that binds no agent, which is a different
    statement from binding an empty set.
    """
    request = _durable_request(run_id, revision_hash, bindings)
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
) -> AnyStartPublishedRunRequest | None:
    if bindings is None:
        return StartPublishedRunRequest(run_id, revision_hash)
    try:
        return StartPublishedRunRequestV2(
            run_id,
            revision_hash,
            AgentBindingSet(
                tuple(
                    AgentBinding(
                        AgentRole(binding.role),
                        AgentConfigurationRevisionHash(
                            binding.agent_configuration_revision_hash
                        ),
                    )
                    for binding in bindings
                )
            ),
        )
    except (TypeError, ValueError):
        return None
