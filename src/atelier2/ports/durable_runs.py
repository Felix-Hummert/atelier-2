from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.executions import SubmitWaitAnswerRequest, WaitAnswerSnapshot
from atelier2.contracts.node_records_v3 import (
    ContextPackage,
    NodeArtifact,
    NodeExecutionRequest,
    NodeReceipt,
    NodeReceiptHash,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, PublishedRevisionHash
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
class StartV3RunWithReceiptRequest:
    """One supervised V3 start and its already-decided terminal node truth.

    The `context_package` is the manifest itself and not only its hash, because
    ADR 0006 binds that material to be written once, immutably, before START. A
    request that named a package it did not carry would leave a receipt pointing
    at bytes nobody kept, so the manifest travels with the truth that names it.
    """

    revision: PublishedRevision
    node_request: NodeExecutionRequest
    context_package: ContextPackage
    artifacts: tuple[NodeArtifact, ...]
    receipt: NodeReceipt


@dataclass(frozen=True)
class DurableV3RunCreated:
    run_id: RunId
    revision_hash: PublishedRevisionHash
    receipt_hash: NodeReceiptHash


@dataclass(frozen=True)
class DurableV3RunExisting:
    run_id: RunId
    revision_hash: PublishedRevisionHash
    receipt_hash: NodeReceiptHash


@dataclass(frozen=True)
class DurableV3StartBindingInvalid:
    """The typed request objects do not describe one exact node execution."""


class V3StartRecord(StrEnum):
    PUBLISHED_REVISION = "published_revision"
    WORKFLOW_BACKING = "workflow_backing"
    RUN = "run"
    ARTIFACT = "artifact"
    RECEIPT = "receipt"
    CONTEXT_PACKAGE = "context_package"


@dataclass(frozen=True)
class DurableV3StartConflict:
    """Durable identity exists with different bytes or bindings."""

    record: V3StartRecord


type DurableV3StartWithReceiptResult = (
    DurableV3RunCreated
    | DurableV3RunExisting
    | DurableV3StartBindingInvalid
    | DurableV3StartConflict
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class DurableV3RunStarter(Protocol):
    """Persist one supervised V3 start and its terminal truth, or nothing.

    The port exists so a caller can reach the atomic start without reaching for
    the adapter that implements it: what makes the write trustworthy is the one
    transaction behind this method, and a caller proves that by depending on the
    method rather than on a store.
    """

    def start_v3_with_receipt(
        self, request: StartV3RunWithReceiptRequest
    ) -> DurableV3StartWithReceiptResult: ...


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
