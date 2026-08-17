from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.executions import SubmitWaitAnswerRequest, WaitAnswerSnapshot
from atelier2.contracts.node_records_v3 import RunInput
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
    | DurableV3StartInputRefused
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


class V3InputRefusal(StrEnum):
    """Every named way one order stops a start before anything is written."""

    MISSING = "missing"
    UNDECLARED = "undeclared"
    DUPLICATED = "duplicated"
    SCHEMA_MISMATCH = "schema-mismatch"
    VALUE_REFUSED = "value-refused"


@dataclass(frozen=True)
class DurableV3StartInputRefused:
    """One order the start cannot honour, named by the input it is about.

    The name comes first because it is what an operator fixes: a refusal that
    said only "schema violated" would send them to read the document to find out
    which order they got wrong.
    """

    name: str
    refusal: V3InputRefusal
    detail: str | None = None


@dataclass(frozen=True)
class StartPublishedRunRequestV3:
    """A start that carries the orders the document declares, beside it.

    It is a third shape rather than a field on the V2 one because an order only
    exists for a V3 document, and a request that could carry one for a V1 or V2
    run would describe something no graph can read. A V3 document that declares
    no `graph_inputs` still starts through the V2 shape; what refuses a missing
    order is the order itself being absent, not the shape of the request.
    """

    run_id: RunId
    revision_hash: WorkflowRevisionHash
    agent_bindings: AgentBindingSet
    run_inputs: tuple[RunInput, ...] = ()


type AnyStartPublishedRunRequest = (
    StartPublishedRunRequest | StartPublishedRunRequestV2 | StartPublishedRunRequestV3
)


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
