from __future__ import annotations

from typing import Annotated, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atelier2.api.references import (
    EVENT_CURSOR_PATTERN,
    MAX_SIGNED_INT64,
    PUBLIC_RUN_REFERENCE_PATTERN,
    REVISION_HASH_PATTERN,
    SHA256_HASH_PATTERN,
    encode_canonical_base64,
    encode_event_cursor,
    encode_public_run_reference,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AuthProfileRevision,
)
from atelier2.contracts.effects import (
    EffectReceipt,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    ReconcileCommand,
)
from atelier2.contracts.executions import RunEventKind, is_canonical_integer_bytes
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.run_projections import PublicAgentAttemptState
from atelier2.contracts.runs import RunState
from atelier2.contracts.workflows import (
    ActionNode,
    AgentNode,
    AgentNodeV2,
    SubworkflowNode,
    WaitNode,
    WorkflowGraph,
    WorkflowGraphV2,
    WorkflowNode,
    WorkflowNodeV2,
)
from atelier2.contracts.workflows_v3 import AnyWorkflowDocument, WorkflowGraphV3
from atelier2.ports.run_events import PersistedRunEvent
from atelier2.ports.run_queries import RunProjection
from atelier2.ports.workflow_revisions import WorkflowRevisionProjection


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HealthResource(ApiModel):
    status: Literal["serving"]
    source_commit: str
    source_tree: str


class PublishAuthProfileRevisionRequestResource(ApiModel):
    profile_id: str = Field(min_length=1, max_length=1_024)
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    provider_id: str = Field(min_length=1, max_length=64)
    auth_mode: Literal["subscription", "api_key"]


class AuthProfileRevisionResource(ApiModel):
    profile_id: str = Field(min_length=1, max_length=1_024)
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    provider_id: str = Field(min_length=1, max_length=64)
    auth_mode: Literal["subscription", "api_key"]
    auth_profile_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class PublishAgentConfigurationRevisionRequestResource(ApiModel):
    model: str = Field(min_length=1, max_length=1_024)
    auth_profile_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    executor_revision: str = Field(min_length=1, max_length=1_024)


class AgentConfigurationRevisionResource(ApiModel):
    model: str = Field(min_length=1, max_length=1_024)
    auth_profile_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    executor_revision: str = Field(min_length=1, max_length=1_024)
    provider_id: str = Field(min_length=1, max_length=64)
    auth_mode: Literal["subscription", "api_key"]
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


class WorkflowGraphResourceV3(ApiModel):
    """A published V3 revision: its format, its size, and that nothing runs it."""

    format_version: Literal[3]
    executable: Literal[False]
    node_count: int = Field(ge=1)


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


class RunEventBaseResource(ApiModel):
    cursor: str = Field(pattern=EVENT_CURSOR_PATTERN)
    sequence: int = Field(ge=1, le=MAX_SIGNED_INT64)
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    node_id: str = Field(min_length=1)
    node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)
    event_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class AgentCompletedEventResource(RunEventBaseResource):
    event: Literal["AGENT_COMPLETED"]
    output: str
    payload_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ActionReconciliationRequiredEventResource(RunEventBaseResource):
    event: Literal["ACTION_RECONCILIATION_REQUIRED"]
    request_base64: str
    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ActionReconciliationResolvedEventResource(RunEventBaseResource):
    event: Literal["ACTION_RECONCILIATION_RESOLVED"]
    receipt: EffectReceiptResource


class ActionCompletedEventResource(RunEventBaseResource):
    event: Literal["ACTION_COMPLETED"]
    receipt: EffectReceiptResource


class WaitingInputEventResource(RunEventBaseResource):
    event: Literal["WAITING_INPUT"]
    answer_type: Literal["integer"]


class WaitAnsweredEventResource(RunEventBaseResource):
    event: Literal["WAIT_ANSWERED"]
    answer: str
    answer_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class SubworkflowCompletedEventResource(RunEventBaseResource):
    event: Literal["SUBWORKFLOW_COMPLETED"]
    result: int
    result_hash: str = Field(pattern=SHA256_HASH_PATTERN)


RunEventResource = Annotated[
    AgentCompletedEventResource
    | ActionReconciliationRequiredEventResource
    | ActionReconciliationResolvedEventResource
    | ActionCompletedEventResource
    | WaitingInputEventResource
    | WaitAnsweredEventResource
    | SubworkflowCompletedEventResource,
    Field(discriminator="event"),
]


class RunEventBaseResourceV2(ApiModel):
    workflow_format_version: Literal[2]
    cursor: str = Field(pattern=EVENT_CURSOR_PATTERN)
    sequence: int = Field(ge=1, le=MAX_SIGNED_INT64)
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    node_id: str = Field(min_length=1)
    node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)
    event_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class AgentCompletedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["AGENT_COMPLETED"]
    output_base64: str
    output_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]


class AgentFailedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["AGENT_FAILED"]
    failure_code: Literal["PROCESS_EXITED_UNSUCCESSFULLY"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]


class AgentCancelRequestedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["AGENT_CANCEL_REQUESTED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=1_024)
    replacement: Literal["NONE", "ONE"]


class AgentCancelledEventResourceV2(RunEventBaseResourceV2):
    event: Literal["AGENT_CANCELLED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=1_024)
    replacement: Literal["NONE", "ONE"]
    disposition: Literal[
        "NEVER_LAUNCHED",
        "EXITED_BEFORE_SIGNAL",
        "REAPED_AFTER_TERM",
        "REAPED_AFTER_KILL",
        "OWNER_LOST_AFTER_PARENT_DEATH",
    ]
    replacement_attempt_id: str | None = Field(pattern=SHA256_HASH_PATTERN)


class AgentInterruptedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["AGENT_INTERRUPTED"]
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    command_id: str = Field(min_length=1, max_length=1_024)
    replacement: Literal["NONE", "ONE"]
    disposition: Literal[
        "NEVER_LAUNCHED",
        "EXITED_BEFORE_SIGNAL",
        "REAPED_AFTER_TERM",
        "REAPED_AFTER_KILL",
        "OWNER_LOST_AFTER_PARENT_DEATH",
    ]
    replacement_attempt_id: str | None = Field(pattern=SHA256_HASH_PATTERN)


class ActionReconciliationRequiredEventResourceV2(RunEventBaseResourceV2):
    event: Literal["ACTION_RECONCILIATION_REQUIRED"]
    request_base64: str
    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ActionReconciliationResolvedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["ACTION_RECONCILIATION_RESOLVED"]
    receipt: EffectReceiptResource


class ActionCompletedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["ACTION_COMPLETED"]
    receipt: EffectReceiptResource


class WaitingInputEventResourceV2(RunEventBaseResourceV2):
    event: Literal["WAITING_INPUT"]
    answer_type: Literal["integer"]


class WaitAnsweredEventResourceV2(RunEventBaseResourceV2):
    event: Literal["WAIT_ANSWERED"]
    answer: str
    answer_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class SubworkflowCompletedEventResourceV2(RunEventBaseResourceV2):
    event: Literal["SUBWORKFLOW_COMPLETED"]
    result: int
    result_hash: str = Field(pattern=SHA256_HASH_PATTERN)


RunEventResourceV2 = Annotated[
    AgentCompletedEventResourceV2
    | AgentFailedEventResourceV2
    | AgentCancelRequestedEventResourceV2
    | AgentCancelledEventResourceV2
    | AgentInterruptedEventResourceV2
    | ActionReconciliationRequiredEventResourceV2
    | ActionReconciliationResolvedEventResourceV2
    | ActionCompletedEventResourceV2
    | WaitingInputEventResourceV2
    | WaitAnsweredEventResourceV2
    | SubworkflowCompletedEventResourceV2,
    Field(discriminator="event"),
]

AnyRunEventResource = RunEventResource | RunEventResourceV2


class StartRunRequestResource(ApiModel):
    run_id: str = Field(min_length=1)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class StartRunAgentBindingResourceV2(ApiModel):
    role: str = Field(min_length=1, max_length=1_024)
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class StartRunRequestResourceV2(ApiModel):
    workflow_format_version: Literal[2]
    run_id: str = Field(min_length=1)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    agent_bindings: tuple[StartRunAgentBindingResourceV2, ...] = Field(
        max_length=100, strict=False
    )


AnyStartRunRequestResource = StartRunRequestResource | StartRunRequestResourceV2


class AnswerWaitRequestResource(ApiModel):
    revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    node_id: str = Field(min_length=1)
    answer_base64: str = Field(min_length=1)


class ReconcileRunRequestResource(ApiModel):
    command_id: str = Field(min_length=1)
    expected_intent_state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    actor: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    determination: ReconciliationDeterminationResource


class CancelAgentAttemptRequestResource(ApiModel):
    command_id: str = Field(min_length=1, max_length=1_024)
    expected_attempt_state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    replacement: Literal["NONE", "ONE"]


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


def auth_profile_revision_resource(
    revision: AuthProfileRevision,
) -> AuthProfileRevisionResource:
    return AuthProfileRevisionResource(
        profile_id=revision.profile_id,
        revision_number=revision.revision_number,
        provider_id=revision.provider_id.value,
        auth_mode=revision.auth_mode.value,
        auth_profile_revision_hash=revision.revision_hash.value,
    )


def agent_configuration_revision_resource(
    revision: AgentConfigurationRevision,
    auth_profile: AuthProfileRevision,
) -> AgentConfigurationRevisionResource:
    return AgentConfigurationRevisionResource(
        model=revision.model,
        auth_profile_revision_hash=revision.auth_profile_revision_hash.value,
        executor_revision=revision.executor_revision.value,
        provider_id=auth_profile.provider_id.value,
        auth_mode=auth_profile.auth_mode.value,
        agent_configuration_revision_hash=revision.revision_hash.value,
    )


def node_resource(node: WorkflowNode | WorkflowNodeV2) -> NodeResource | NodeResourceV2:
    if isinstance(node, AgentNode):
        return AgentNodeResource(
            type="agent",
            node_id=node.id,
            job=node.job,
            output=node.output,
            next_node_id=node.next,
        )
    if isinstance(node, AgentNodeV2):
        return AgentNodeResourceV2(
            type="agent",
            node_id=node.id,
            role=node.role,
            job=node.job,
            next_node_id=node.next,
        )
    if isinstance(node, ActionNode):
        return ActionNodeResource(
            type="action", node_id=node.id, next_node_id=node.next
        )
    if isinstance(node, WaitNode):
        return WaitNodeResource(
            type="wait",
            node_id=node.id,
            answer_type=node.answer_type,
            next_node_id=node.next,
        )
    if isinstance(node, SubworkflowNode):
        return SubworkflowNodeResource(
            type="subworkflow",
            node_id=node.id,
            operation=node.operation,
            operands=node.operands,
            next_node_id=None,
        )
    raise AssertionError("closed workflow node union was extended without API mapping")


def graph_resource(
    graph: AnyWorkflowDocument,
) -> WorkflowGraphResource | WorkflowGraphResourceV2 | WorkflowGraphResourceV3:
    if isinstance(graph, WorkflowGraphV3):
        return WorkflowGraphResourceV3(
            format_version=3, executable=False, node_count=len(graph.nodes)
        )
    ordered = sorted(graph.nodes, key=lambda item: item.id.encode("utf-8"))
    if isinstance(graph, WorkflowGraph):
        return WorkflowGraphResource(
            format_version=1,
            start_node_id=graph.start,
            nodes=cast(
                tuple[NodeResource, ...], tuple(node_resource(item) for item in ordered)
            ),
        )
    return WorkflowGraphResourceV2(
        format_version=2,
        start_node_id=graph.start,
        nodes=cast(
            tuple[NodeResourceV2, ...], tuple(node_resource(item) for item in ordered)
        ),
    )


def workflow_revision_detail_resource(
    projection: WorkflowRevisionProjection,
) -> WorkflowRevisionDetailResource:
    return WorkflowRevisionDetailResource(
        revision_hash=projection.revision.revision_hash.value,
        document_base64=encode_canonical_base64(projection.revision.document),
        graph=graph_resource(projection.graph),
    )


def determination_resource(
    determination: OperatorFoundEffect | OperatorAuthoritativeAbsence,
) -> ReconciliationDeterminationResource:
    if isinstance(determination, OperatorFoundEffect):
        return OperatorFoundDeterminationResource(
            type="operator_found",
            effect_id=determination.effect_id.value,
            result_base64=encode_canonical_base64(determination.result.payload),
        )
    return OperatorAuthoritativeAbsenceDeterminationResource(
        type="operator_authoritative_absence"
    )


def command_resource(command: ReconcileCommand) -> ReconciliationCommandResource:
    return ReconciliationCommandResource(
        command_id=command.command_id.value,
        actor=command.actor.value,
        evidence=command.evidence,
        state="PENDING",
        determination=determination_resource(command.determination),
    )


def run_resource(projection: RunProjection) -> AnyRunResource:
    run = projection.run
    node = projection.graph.node(run.current_node_id)
    if isinstance(run, RunV2):
        return _run_resource_v2(projection, run, node)
    if run.state is RunState.WAITING_INPUT:
        if not isinstance(node, WaitNode):
            raise ValueError("waiting input run does not name a Wait node")
        waiting: WaitingResource = WaitingInputResource(
            type="WAITING_INPUT", node_id=node.id, answer_type=node.answer_type
        )
    elif run.state is RunState.WAITING_RECONCILIATION:
        reconciliation = projection.reconciliation
        if reconciliation is None:
            raise ValueError("waiting reconciliation run has no intent projection")
        intent = reconciliation.intent
        waiting = WaitingReconciliationResource(
            type="WAITING_RECONCILIATION",
            node_id=node.id,
            logical_effect_key=intent.intent.binding.logical_key.value,
            request_hash=intent.intent.request.request_hash.value,
            request_base64=encode_canonical_base64(intent.intent.request.payload),
            intent_state_version=intent.state_version.value,
            pending_command=(
                None
                if reconciliation.pending_command is None
                else command_resource(reconciliation.pending_command.command)
            ),
        )
    else:
        waiting = NoWaitingResource(type="NONE")
    return RunResource(
        run_id=run.run_id.value,
        public_run_reference=encode_public_run_reference(run.run_id),
        workflow_revision_hash=run.revision_hash.value,
        state_version=run.state_version,
        state=run.state.value,
        current_node=cast(NodeResource, node_resource(node)),
        waiting=waiting,
        terminal_hash=None if run.terminal_hash is None else run.terminal_hash.value,
        latest_event_cursor=(
            None
            if run.last_event_sequence == 0
            else encode_event_cursor(run.run_id, run.last_event_sequence)
        ),
    )


def _run_resource_v2(
    projection: RunProjection,
    run: RunV2,
    node: WorkflowNode | WorkflowNodeV2,
) -> RunResourceV2:
    if not isinstance(projection.graph, WorkflowGraphV2):
        raise TypeError("V2 run projection has a V1 workflow graph")
    if run.state is RunState.WAITING_INPUT:
        if not isinstance(node, WaitNode):
            raise ValueError("waiting input V2 run does not name a Wait node")
        waiting: WaitingResourceV2 = WaitingInputResourceV2(
            type="WAITING_INPUT", node_id=node.id, answer_type=node.answer_type
        )
    elif run.state is RunState.WAITING_RECONCILIATION:
        reconciliation = projection.reconciliation
        if reconciliation is None:
            raise ValueError("waiting reconciliation V2 run has no intent projection")
        intent = reconciliation.intent
        waiting = WaitingReconciliationResourceV2(
            type="WAITING_RECONCILIATION",
            node_id=node.id,
            logical_effect_key=intent.intent.binding.logical_key.value,
            request_hash=intent.intent.request.request_hash.value,
            request_base64=encode_canonical_base64(intent.intent.request.payload),
            intent_state_version=intent.state_version.value,
            pending_command=(
                None
                if reconciliation.pending_command is None
                else command_resource(reconciliation.pending_command.command)
            ),
        )
    else:
        waiting = NoWaitingResourceV2(type="NONE")
    return RunResourceV2(
        workflow_format_version=2,
        run_id=run.run_id.value,
        public_run_reference=encode_public_run_reference(run.run_id),
        workflow_revision_hash=run.revision_hash.value,
        agent_binding_set_hash=run.binding_set_hash.value,
        agent_bindings=tuple(
            AgentBindingResourceV2(
                role=binding.role.value,
                agent_configuration_revision_hash=(
                    binding.configuration.revision_hash.value
                ),
                auth_profile_revision_hash=binding.auth_profile.revision_hash.value,
                profile_id=binding.auth_profile.profile_id,
                revision_number=binding.auth_profile.revision_number,
                provider_id=binding.auth_profile.provider_id.value,
                auth_mode=binding.auth_profile.auth_mode.value,
                model=binding.configuration.model,
                executor_revision=binding.configuration.executor_revision.value,
            )
            for binding in run.agent_bindings
        ),
        state_version=run.state_version,
        state=run.state.value,
        current_node=cast(NodeResourceV2, node_resource(node)),
        agent_attempts=tuple(
            AgentAttemptResourceV2(
                attempt_id=attempt.attempt_id.value,
                node_execution_id=attempt.node_execution_id.value,
                request_hash=attempt.request_hash.value,
                attempt_ordinal=cast(Literal[1, 2], attempt.attempt_ordinal),
                state=cast(PublicAttemptStateName, attempt.state),
                failure_code=(
                    None if attempt.failure_code is None else attempt.failure_code.value
                ),
                cancellation=(
                    None
                    if attempt.cancellation is None
                    else AgentAttemptCancellationResourceV2(
                        command_id=attempt.cancellation.command_id,
                        replacement=attempt.cancellation.replacement.value,
                        redrive_state=attempt.cancellation.redrive_state.value,
                        disposition=(
                            None
                            if attempt.cancellation.disposition is None
                            else attempt.cancellation.disposition.value
                        ),
                    )
                ),
            )
            for attempt in projection.agent_attempts
        ),
        waiting=waiting,
        terminal_hash=None if run.terminal_hash is None else run.terminal_hash.value,
        latest_event_cursor=(
            None
            if run.last_event_sequence == 0
            else encode_event_cursor(run.run_id, run.last_event_sequence)
        ),
    )


def receipt_resource(receipt: EffectReceipt) -> EffectReceiptResource:
    return EffectReceiptResource(
        logical_effect_key=receipt.intent.binding.logical_key.value,
        request_hash=receipt.intent.request.request_hash.value,
        effect_id=receipt.effect_id.value,
        result_hash=receipt.result.payload_hash.value,
        result_base64=encode_canonical_base64(receipt.result.payload),
        confirmation_source=receipt.confirmation_source.value,
        reconcile_command_id=(
            None
            if receipt.reconcile_command_id is None
            else receipt.reconcile_command_id.value
        ),
    )


def run_event_resource(projection: PersistedRunEvent) -> AnyRunEventResource:
    if projection.workflow_format_version == 2:
        return _run_event_resource_v2(projection)
    event = projection.event
    if event.event_kind is RunEventKind.AGENT_FAILED:
        raise ValueError("a V1 run cannot carry AGENT_FAILED")
    common = {
        "cursor": encode_event_cursor(event.run_id, event.event_sequence),
        "sequence": event.event_sequence,
        "public_run_reference": encode_public_run_reference(event.run_id),
        "workflow_revision_hash": event.revision_hash.value,
        "node_id": event.node_id,
        "node_execution_id": event.node_execution_id.value,
        "event_hash": event.event_hash.value,
    }
    if event.event_kind is RunEventKind.AGENT_COMPLETED:
        return AgentCompletedEventResource(
            event=event.event_kind.value,
            output=event.payload.decode("utf-8"),
            payload_hash=event.payload_hash.value,
            **common,
        )
    if event.event_kind is RunEventKind.ACTION_RECONCILIATION_REQUIRED:
        return ActionReconciliationRequiredEventResource(
            event=event.event_kind.value,
            request_base64=encode_canonical_base64(event.payload),
            request_hash=event.payload_hash.value,
            **common,
        )
    if event.event_kind is RunEventKind.ACTION_RECONCILIATION_RESOLVED:
        if projection.receipt is None:
            raise ValueError("resolved event has no receipt")
        return ActionReconciliationResolvedEventResource(
            event=event.event_kind.value,
            receipt=receipt_resource(projection.receipt),
            **common,
        )
    if event.event_kind is RunEventKind.ACTION_COMPLETED:
        if projection.receipt is None:
            raise ValueError("completed Action event has no receipt")
        return ActionCompletedEventResource(
            event=event.event_kind.value,
            receipt=receipt_resource(projection.receipt),
            **common,
        )
    if event.event_kind is RunEventKind.WAITING_INPUT:
        return WaitingInputEventResource(
            event=event.event_kind.value, answer_type="integer", **common
        )
    if event.event_kind is RunEventKind.WAIT_ANSWERED:
        if not is_canonical_integer_bytes(event.payload):
            raise ValueError("durable wait answer is not canonical integer text")
        answer = event.payload.decode("ascii")
        return WaitAnsweredEventResource(
            event=event.event_kind.value,
            answer=answer,
            answer_hash=event.payload_hash.value,
            **common,
        )
    if event.event_kind is RunEventKind.SUBWORKFLOW_COMPLETED:
        if not is_canonical_integer_bytes(event.payload):
            raise ValueError("durable subworkflow result is not canonical integer text")
        return SubworkflowCompletedEventResource(
            event=event.event_kind.value,
            result=int(event.payload.decode("ascii")),
            result_hash=event.payload_hash.value,
            **common,
        )
    raise AssertionError("closed event union was extended without API mapping")


def _run_event_resource_v2(projection: PersistedRunEvent) -> RunEventResourceV2:
    event = projection.event
    common = {
        "workflow_format_version": 2,
        "cursor": encode_event_cursor(event.run_id, event.event_sequence),
        "sequence": event.event_sequence,
        "public_run_reference": encode_public_run_reference(event.run_id),
        "workflow_revision_hash": event.revision_hash.value,
        "node_id": event.node_id,
        "node_execution_id": event.node_execution_id.value,
        "event_hash": event.event_hash.value,
    }
    if event.event_kind is RunEventKind.AGENT_COMPLETED:
        if event.agent_attempt_id is None or event.attempt_ordinal is None:
            raise ValueError("V2 agent completion has no exact attempt binding")
        return AgentCompletedEventResourceV2(
            event=event.event_kind.value,
            output_base64=encode_canonical_base64(event.payload),
            output_hash=event.payload_hash.value,
            attempt_id=event.agent_attempt_id,
            attempt_ordinal=cast(Literal[1, 2], event.attempt_ordinal),
            **common,
        )
    if event.event_kind is RunEventKind.AGENT_FAILED:
        failure_code = event.payload.decode("ascii")
        if failure_code != "PROCESS_EXITED_UNSUCCESSFULLY":
            raise ValueError("durable agent failure payload is not canonical")
        if event.agent_attempt_id is None or event.attempt_ordinal is None:
            raise ValueError("V2 agent failure has no exact attempt binding")
        return AgentFailedEventResourceV2(
            event=event.event_kind.value,
            failure_code="PROCESS_EXITED_UNSUCCESSFULLY",
            attempt_id=event.agent_attempt_id,
            attempt_ordinal=cast(Literal[1, 2], event.attempt_ordinal),
            **common,
        )
    if event.event_kind is RunEventKind.AGENT_CANCEL_REQUESTED:
        if (
            event.agent_attempt_id is None
            or event.attempt_ordinal is None
            or event.cancellation_command_id is None
            or event.replacement is None
        ):
            raise ValueError("V2 cancellation request has no exact binding")
        return AgentCancelRequestedEventResourceV2(
            event=event.event_kind.value,
            attempt_id=event.agent_attempt_id,
            attempt_ordinal=cast(Literal[1, 2], event.attempt_ordinal),
            command_id=event.cancellation_command_id,
            replacement=cast(Literal["NONE", "ONE"], event.replacement),
            **common,
        )
    if event.event_kind in {
        RunEventKind.AGENT_CANCELLED,
        RunEventKind.AGENT_INTERRUPTED,
    }:
        if (
            event.agent_attempt_id is None
            or event.attempt_ordinal is None
            or event.cancellation_command_id is None
            or event.replacement is None
            or event.cancellation_disposition is None
        ):
            raise ValueError("V2 cancellation terminal has no exact binding")
        terminal_common = {
            "attempt_id": event.agent_attempt_id,
            "attempt_ordinal": cast(Literal[1, 2], event.attempt_ordinal),
            "command_id": event.cancellation_command_id,
            "replacement": cast(Literal["NONE", "ONE"], event.replacement),
            "disposition": cast(
                Literal[
                    "NEVER_LAUNCHED",
                    "EXITED_BEFORE_SIGNAL",
                    "REAPED_AFTER_TERM",
                    "REAPED_AFTER_KILL",
                    "OWNER_LOST_AFTER_PARENT_DEATH",
                ],
                event.cancellation_disposition,
            ),
            "replacement_attempt_id": event.replacement_attempt_id,
        }
        if event.event_kind is RunEventKind.AGENT_CANCELLED:
            return AgentCancelledEventResourceV2(
                event=event.event_kind.value, **terminal_common, **common
            )
        return AgentInterruptedEventResourceV2(
            event="AGENT_INTERRUPTED", **terminal_common, **common
        )
    if event.event_kind is RunEventKind.ACTION_RECONCILIATION_REQUIRED:
        return ActionReconciliationRequiredEventResourceV2(
            event=event.event_kind.value,
            request_base64=encode_canonical_base64(event.payload),
            request_hash=event.payload_hash.value,
            **common,
        )
    if event.event_kind is RunEventKind.ACTION_RECONCILIATION_RESOLVED:
        if projection.receipt is None:
            raise ValueError("resolved V2 event has no receipt")
        return ActionReconciliationResolvedEventResourceV2(
            event=event.event_kind.value,
            receipt=receipt_resource(projection.receipt),
            **common,
        )
    if event.event_kind is RunEventKind.ACTION_COMPLETED:
        if projection.receipt is None:
            raise ValueError("completed V2 Action event has no receipt")
        return ActionCompletedEventResourceV2(
            event=event.event_kind.value,
            receipt=receipt_resource(projection.receipt),
            **common,
        )
    if event.event_kind is RunEventKind.WAITING_INPUT:
        return WaitingInputEventResourceV2(
            event=event.event_kind.value, answer_type="integer", **common
        )
    if event.event_kind is RunEventKind.WAIT_ANSWERED:
        if not is_canonical_integer_bytes(event.payload):
            raise ValueError("durable V2 wait answer is not canonical integer text")
        return WaitAnsweredEventResourceV2(
            event=event.event_kind.value,
            answer=event.payload.decode("ascii"),
            answer_hash=event.payload_hash.value,
            **common,
        )
    if event.event_kind is RunEventKind.SUBWORKFLOW_COMPLETED:
        if not is_canonical_integer_bytes(event.payload):
            raise ValueError(
                "durable V2 subworkflow result is not canonical integer text"
            )
        return SubworkflowCompletedEventResourceV2(
            event=event.event_kind.value,
            result=int(event.payload.decode("ascii")),
            result_hash=event.payload_hash.value,
            **common,
        )
    raise AssertionError("closed V2 event union was extended without API mapping")
