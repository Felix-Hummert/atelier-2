from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.executions import SubmitWaitAnswerRequest, WaitAnswerSnapshot
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, WorkflowRevisionHash


@dataclass(frozen=True)
class DurableWriteUnavailable:
    pass


@dataclass(frozen=True)
class DurableStateCorrupt:
    pass


@dataclass(frozen=True)
class DurableRunCreated:
    run: AnyRun


@dataclass(frozen=True)
class DurableRunExisting:
    run: AnyRun


@dataclass(frozen=True)
class DurableRunRevisionMissing:
    pass


@dataclass(frozen=True)
class DurableRunIdentityConflict:
    pass


@dataclass(frozen=True)
class DurableRunFormatNotExecutable:
    """The named revision is published, and no runtime here executes its format."""


@dataclass(frozen=True)
class DurableInvalidAgentBindings:
    pass


@dataclass(frozen=True)
class DurableAgentConfigurationRevisionMissing:
    pass


@dataclass(frozen=True)
class DurableAgentExecutorBindingUnavailable:
    pass


@dataclass(frozen=True)
class DurableAgentExecutorCapabilityUnavailable:
    pass


type DurablePublishedRunResult = (
    DurableRunCreated
    | DurableRunExisting
    | DurableRunRevisionMissing
    | DurableRunIdentityConflict
    | DurableRunFormatNotExecutable
    | DurableInvalidAgentBindings
    | DurableAgentConfigurationRevisionMissing
    | DurableAgentExecutorBindingUnavailable
    | DurableAgentExecutorCapabilityUnavailable
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class StartPublishedRunRequest:
    run_id: RunId
    revision_hash: WorkflowRevisionHash


@dataclass(frozen=True)
class StartPublishedRunRequestV2:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    agent_bindings: AgentBindingSet


type AnyStartPublishedRunRequest = StartPublishedRunRequest | StartPublishedRunRequestV2


class DurablePublishedRunStarter(Protocol):
    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult: ...


@dataclass(frozen=True)
class DurableAnswerCreated:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class DurableAnswerExisting:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class DurableAnswerRunMissing:
    pass


@dataclass(frozen=True)
class DurableAnswerNodeMissing:
    pass


@dataclass(frozen=True)
class DurableAnswerRevisionConflict:
    pass


@dataclass(frozen=True)
class DurableAnswerStateConflict:
    pass


@dataclass(frozen=True)
class DurableAnswerBytesConflict:
    pass


type DurableAnswerResult = (
    DurableAnswerCreated
    | DurableAnswerExisting
    | DurableAnswerRunMissing
    | DurableAnswerNodeMissing
    | DurableAnswerRevisionConflict
    | DurableAnswerStateConflict
    | DurableAnswerBytesConflict
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class TransactionalWaitAnswerer(Protocol):
    def submit_result(
        self, request: SubmitWaitAnswerRequest
    ) -> DurableAnswerResult: ...
