from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.publish_workflow_revision import (
    DurableStateCorrupt,
    WriteUnavailable,
)
from atelier2.contracts.run_bindings import AnyRun
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurableAgentConfigurationRevisionMissing,
    DurableAgentExecutorBindingUnavailable,
    DurableAgentExecutorCapabilityUnavailable,
    DurableInvalidAgentBindings,
    DurablePublishedRunStarter,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableWriteUnavailable,
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
    | InvalidAgentBindings
    | AgentConfigurationRevisionMissing
    | AgentExecutorBindingUnavailable
    | WriteUnavailable
    | DurableStateCorrupt
)


def start_published_run(
    request: AnyStartPublishedRunRequest, starter: DurablePublishedRunStarter
) -> StartPublishedRunResult:
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
