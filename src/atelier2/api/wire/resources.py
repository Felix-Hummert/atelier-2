"""The schemas the API answers with: health, revisions, runs, pages, problems."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atelier2.api.references import (
    EVENT_CURSOR_PATTERN,
    MAX_SIGNED_INT64,
    PUBLIC_RUN_REFERENCE_PATTERN,
    REVISION_HASH_PATTERN,
    SHA256_HASH_PATTERN,
)
from atelier2.contracts.run_projections import NodeState, PublicAgentAttemptState


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HealthResource(ApiModel):
    status: Literal["serving"]
    source_commit: str
    source_tree: str


class AuthProfileRevisionResource(ApiModel):
    profile_id: str = Field(min_length=1, max_length=1_024)
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    provider_id: str = Field(min_length=1, max_length=64)
    auth_mode: Literal["subscription", "api_key"]
    auth_profile_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class AgentConfigurationRevisionResource(ApiModel):
    model: str = Field(min_length=1, max_length=1_024)
    auth_profile_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    executor_revision: str = Field(min_length=1, max_length=1_024)
    provider_id: str = Field(min_length=1, max_length=64)
    auth_mode: Literal["subscription", "api_key"]
    requested_capability: Literal["headless", "interactive"]
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class AgentNodeResource(ApiModel):
    type: Literal["agent"]
    node_id: str = Field(min_length=1)
    job: str = Field(min_length=1)
    output: str = Field(min_length=1)
    next_node_id: str = Field(min_length=1)


class AgentNodeResourceV2(ApiModel):
    type: Literal["agent"]
    node_id: str = Field(min_length=1)
    role: str = Field(min_length=1)
    job: str = Field(min_length=1)
    next_node_id: str = Field(min_length=1)


class ActionNodeResource(ApiModel):
    type: Literal["action"]
    node_id: str = Field(min_length=1)
    next_node_id: str = Field(min_length=1)


class WaitNodeResource(ApiModel):
    type: Literal["wait"]
    node_id: str = Field(min_length=1)
    answer_type: Literal["integer"]
    next_node_id: str = Field(min_length=1)


class SubworkflowNodeResource(ApiModel):
    type: Literal["subworkflow"]
    node_id: str = Field(min_length=1)
    operation: Literal["add"]
    operands: tuple[int, int]
    next_node_id: None


NodeResource = Annotated[
    AgentNodeResource | ActionNodeResource | WaitNodeResource | SubworkflowNodeResource,
    Field(discriminator="type"),
]

NodeResourceV2 = Annotated[
    AgentNodeResourceV2
    | ActionNodeResource
    | WaitNodeResource
    | SubworkflowNodeResource,
    Field(discriminator="type"),
]


class WorkflowGraphResource(ApiModel):
    format_version: Literal[1]
    start_node_id: str = Field(min_length=1)
    nodes: tuple[NodeResource, ...]


class WorkflowGraphResourceV2(ApiModel):
    format_version: Literal[2]
    start_node_id: str = Field(min_length=1)
    nodes: tuple[NodeResourceV2, ...]


# A docstring here is published as this component's description, so the reason
# the two authored fields carry no column of their own stays a comment: ADR 0007
# decision 4 has them parsed out of the published bytes on the way to the wire,
# which is what keeps this resource able only to repeat what the author wrote.
class WorkflowGraphResourceV3(ApiModel):
    """A published V3 revision: its format, its size, and that nothing runs it."""

    format_version: Literal[3]
    executable: Literal[False]
    node_count: int = Field(ge=1)
    name: str = Field(min_length=1)
    description: str | None


AnyWorkflowGraphResource = Annotated[
    WorkflowGraphResource | WorkflowGraphResourceV2 | WorkflowGraphResourceV3,
    Field(discriminator="format_version"),
]


class WorkflowRevisionSummaryResource(ApiModel):
    revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class WorkflowRevisionDetailResource(ApiModel):
    revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    document_base64: str
    graph: AnyWorkflowGraphResource


class WorkflowRevisionPageResource(ApiModel):
    items: tuple[WorkflowRevisionSummaryResource, ...]
    next_after_revision_hash: str | None = Field(pattern=REVISION_HASH_PATTERN)


class WorkflowRevisionSummaryResourceV2(ApiModel):
    """A listed revision: its hash, its format, and what its own bytes call it.

    `name` and `description` are absent where the authoring format declares
    neither, which is the truthful answer for a V1 or V2 document rather than a
    line invented to fill the column.
    """

    revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    format_version: Literal[1, 2, 3]
    executable: bool
    name: str | None
    description: str | None


class VersionedWorkflowRevisionPageResource(ApiModel):
    """One page of listed revisions, ended by the caller's limit or by its budget.

    `next_after_revision_hash` is present in both cases, so a caller resumes the
    same way whichever bound stopped the page.
    """

    items: tuple[WorkflowRevisionSummaryResourceV2, ...]
    next_after_revision_hash: str | None = Field(pattern=REVISION_HASH_PATTERN)


AnyWorkflowRevisionPageResource = (
    WorkflowRevisionPageResource | VersionedWorkflowRevisionPageResource
)


class OperatorFoundDeterminationResource(ApiModel):
    type: Literal["operator_found"]
    effect_id: str = Field(min_length=1)
    result_base64: str


class OperatorAuthoritativeAbsenceDeterminationResource(ApiModel):
    type: Literal["operator_authoritative_absence"]


ReconciliationDeterminationResource = Annotated[
    OperatorFoundDeterminationResource
    | OperatorAuthoritativeAbsenceDeterminationResource,
    Field(discriminator="type"),
]


class ReconciliationCommandResource(ApiModel):
    command_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    state: Literal["PENDING"]
    determination: ReconciliationDeterminationResource


class NoWaitingResource(ApiModel):
    type: Literal["NONE"]


class WaitingInputResource(ApiModel):
    type: Literal["WAITING_INPUT"]
    node_id: str = Field(min_length=1)
    answer_type: Literal["integer"]


class WaitingReconciliationResource(ApiModel):
    type: Literal["WAITING_RECONCILIATION"]
    node_id: str = Field(min_length=1)
    logical_effect_key: str = Field(min_length=1)
    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    request_base64: str
    intent_state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    pending_command: ReconciliationCommandResource | None


WaitingResource = Annotated[
    NoWaitingResource | WaitingInputResource | WaitingReconciliationResource,
    Field(discriminator="type"),
]


class RunResource(ApiModel):
    run_id: str = Field(min_length=1)
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    state: Literal["STARTED", "WAITING_RECONCILIATION", "WAITING_INPUT", "COMPLETED"]
    current_node: NodeResource
    waiting: WaitingResource
    terminal_hash: str | None = Field(pattern=SHA256_HASH_PATTERN)
    latest_event_cursor: str | None = Field(pattern=EVENT_CURSOR_PATTERN)

    @model_validator(mode="after")
    def validate_state_shape(self) -> RunResource:
        if self.state == "STARTED":
            valid = (
                isinstance(self.waiting, NoWaitingResource)
                and self.terminal_hash is None
            )
        elif self.state == "WAITING_INPUT":
            valid = (
                isinstance(self.current_node, WaitNodeResource)
                and isinstance(self.waiting, WaitingInputResource)
                and self.waiting.node_id == self.current_node.node_id
                and self.terminal_hash is None
            )
        elif self.state == "WAITING_RECONCILIATION":
            valid = (
                isinstance(self.current_node, ActionNodeResource)
                and isinstance(self.waiting, WaitingReconciliationResource)
                and self.waiting.node_id == self.current_node.node_id
                and self.terminal_hash is None
            )
        else:
            valid = (
                isinstance(self.current_node, SubworkflowNodeResource)
                and isinstance(self.waiting, NoWaitingResource)
                and self.terminal_hash is not None
            )
        if not valid:
            raise ValueError(
                "run state, current node, waiting reason, and terminal hash disagree"
            )
        return self


class AgentBindingResourceV2(ApiModel):
    role: str = Field(min_length=1, max_length=1_024)
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    auth_profile_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    profile_id: str = Field(min_length=1, max_length=1_024)
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    provider_id: str = Field(min_length=1, max_length=64)
    auth_mode: Literal["subscription", "api_key"]
    model: str = Field(min_length=1, max_length=1_024)
    executor_revision: str = Field(min_length=1, max_length=1_024)


class NoWaitingResourceV2(ApiModel):
    type: Literal["NONE"]


class WaitingInputResourceV2(ApiModel):
    type: Literal["WAITING_INPUT"]
    node_id: str = Field(min_length=1)
    answer_type: Literal["integer"]


class WaitingReconciliationResourceV2(ApiModel):
    type: Literal["WAITING_RECONCILIATION"]
    node_id: str = Field(min_length=1)
    logical_effect_key: str = Field(min_length=1)
    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    request_base64: str
    intent_state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    pending_command: ReconciliationCommandResource | None


# The wire names the vocabulary as the closed union of its owner's members rather
# than as the owner's type, because the served document spells a union out where the
# field stands and puts an enum behind a component reference. Giving the vocabulary
# an owner is not allowed to move a byte of the document.
PublicAttemptStateName = Literal[
    PublicAgentAttemptState.PREPARED,
    PublicAgentAttemptState.POSSIBLY_RAN,
    PublicAgentAttemptState.CANCEL_REQUESTED,
    PublicAgentAttemptState.CANCELLED,
    PublicAgentAttemptState.INTERRUPTED,
    PublicAgentAttemptState.FAILED,
]


class AgentAttemptResourceV2(ApiModel):
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)
    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    state: PublicAttemptStateName
    failure_code: Literal["PROCESS_EXITED_UNSUCCESSFULLY"] | None
    cancellation: AgentAttemptCancellationResourceV2 | None

    @model_validator(mode="after")
    def validate_failure_shape(self) -> AgentAttemptResourceV2:
        if (self.state is PublicAgentAttemptState.FAILED) != (
            self.failure_code is not None
        ):
            raise ValueError("agent attempt state and failure code disagree")
        if (
            self.state
            in {
                PublicAgentAttemptState.CANCEL_REQUESTED,
                PublicAgentAttemptState.CANCELLED,
                PublicAgentAttemptState.INTERRUPTED,
            }
        ) != (self.cancellation is not None):
            raise ValueError("agent attempt state and cancellation disagree")
        return self


class AgentAttemptCancellationResourceV2(ApiModel):
    command_id: str = Field(min_length=1, max_length=1_024)
    replacement: Literal["NONE", "ONE"]
    redrive_state: Literal["PENDING", "OWNER_NOT_LOCAL", "CLEANUP_ATTESTED"]
    disposition: (
        Literal[
            "NEVER_LAUNCHED",
            "EXITED_BEFORE_SIGNAL",
            "REAPED_AFTER_TERM",
            "REAPED_AFTER_KILL",
            "OWNER_LOST_AFTER_PARENT_DEATH",
        ]
        | None
    )

    @model_validator(mode="after")
    def validate_attestation_shape(self) -> AgentAttemptCancellationResourceV2:
        if (self.redrive_state == "CLEANUP_ATTESTED") != (self.disposition is not None):
            raise ValueError("cleanup attestation and disposition disagree")
        return self


WaitingResourceV2 = Annotated[
    NoWaitingResourceV2 | WaitingInputResourceV2 | WaitingReconciliationResourceV2,
    Field(discriminator="type"),
]

# Spelled out as the closed union of its owner's members, like the attempt
# vocabulary above: the served document then names every state where the field
# stands, in one form, instead of holding one enum inline and another behind a
# component reference.
NodeStateName = Literal[
    NodeState.QUEUED,
    NodeState.WORKING,
    NodeState.NEEDS_YOU,
    NodeState.SUCCEEDED,
    NodeState.FAILED,
    NodeState.CANCELLED,
    NodeState.INTERRUPTED,
]


class NodeRailAttemptResource(ApiModel):
    """The agent attempt one node tells a reader about.

    A succeeded attempt carries no state: the public vocabulary has no word for
    success, because the same transition that records it moves the run past the
    attempt. The node's own `succeeded` is what says the work is done — which is
    why no reader has to invent a word for it any more.
    """

    ordinal: Literal[1, 2]
    state: PublicAttemptStateName | None


class NodeRailResource(ApiModel):
    """Where one node of a run stands, said by the server rather than guessed."""

    node_id: str = Field(min_length=1)
    state: NodeStateName
    attempt: NodeRailAttemptResource | None


class RunResourceV2(ApiModel):
    workflow_format_version: Literal[2]
    run_id: str = Field(min_length=1)
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    agent_binding_set_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    agent_bindings: tuple[AgentBindingResourceV2, ...] = Field(max_length=100)
    state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    state: Literal["STARTED", "WAITING_RECONCILIATION", "WAITING_INPUT", "COMPLETED"]
    current_node: NodeResourceV2
    node_rail: tuple[NodeRailResource, ...] = Field(min_length=1)
    agent_attempts: tuple[AgentAttemptResourceV2, ...] = Field(max_length=2)
    waiting: WaitingResourceV2
    terminal_hash: str | None = Field(pattern=SHA256_HASH_PATTERN)
    latest_event_cursor: str | None = Field(pattern=EVENT_CURSOR_PATTERN)

    @model_validator(mode="after")
    def validate_state_shape(self) -> RunResourceV2:
        if self.state == "STARTED":
            valid = (
                isinstance(self.waiting, NoWaitingResourceV2)
                and self.terminal_hash is None
            )
        elif self.state == "WAITING_INPUT":
            valid = (
                isinstance(self.current_node, WaitNodeResource)
                and isinstance(self.waiting, WaitingInputResourceV2)
                and self.waiting.node_id == self.current_node.node_id
                and self.terminal_hash is None
            )
        elif self.state == "WAITING_RECONCILIATION":
            valid = (
                isinstance(self.current_node, ActionNodeResource)
                and isinstance(self.waiting, WaitingReconciliationResourceV2)
                and self.waiting.node_id == self.current_node.node_id
                and self.terminal_hash is None
            )
        else:
            valid = (
                isinstance(self.current_node, SubworkflowNodeResource)
                and isinstance(self.waiting, NoWaitingResourceV2)
                and self.terminal_hash is not None
            )
        if not valid:
            raise ValueError(
                "V2 run state, current node, waiting reason, and terminal hash disagree"
            )
        return self


AnyRunResource = RunResource | RunResourceV2


# No handler builds this: /runs returns VersionedRunPageResource. It stays
# because ADR 0003 freezes the preexisting V1 named OpenAPI components, and this
# one reaches the served document only through AnyRunPageResource below. A
# docstring here would be published as the component's description and break
# that freeze.
class RunPageResource(ApiModel):
    items: tuple[RunResource, ...]
    next_after: str | None = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)


class VersionedRunPageResource(ApiModel):
    items: tuple[AnyRunResource, ...]
    next_after: str | None = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)


AnyRunPageResource = RunPageResource | VersionedRunPageResource


class EffectReceiptResource(ApiModel):
    logical_effect_key: str = Field(min_length=1)
    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    effect_id: str = Field(min_length=1)
    result_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    result_base64: str
    confirmation_source: Literal[
        "ADAPTER_READBACK",
        "ADAPTER_EXECUTION",
        "OPERATOR_FOUND",
        "OPERATOR_AUTHORIZED_EXECUTION",
    ]
    reconcile_command_id: str | None


class ProblemResource(ApiModel):
    type: str
    title: str
    status: int
    detail: str


class StreamFailureResource(ApiModel):
    """The terminal event-stream frame: this stream ended because it failed.

    It carries the same problem body the REST surface would answer, so an
    operator and a machine consumer read one problem vocabulary whether the
    failure was decided before the response headers or after them.
    """

    event: Literal["STREAM_FAILED"] = "STREAM_FAILED"
    problem: ProblemResource
