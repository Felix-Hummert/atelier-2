"""The schemas the API answers with: health, revisions, runs, pages, problems."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from atelier2.api.references import (
    EVENT_CURSOR_PATTERN,
    MAX_SIGNED_INT64,
    MAXIMUM_RUN_AGENT_BINDINGS,
    PUBLIC_RUN_REFERENCE_PATTERN,
    REVISION_HASH_PATTERN,
    SHA256_HASH_PATTERN,
)
from atelier2.contracts.agent_attempts import REPLACEMENT_AGENT_ATTEMPT_ORDINAL
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_PROVIDER_ID_CHARACTERS,
)
from atelier2.contracts.catalog_v3 import (
    MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS,
)
from atelier2.contracts.run_projections import NodeState, PublicAgentAttemptState


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HealthResource(ApiModel):
    status: Literal["serving"]
    source_commit: str
    source_tree: str


class AuthProfileRevisionResource(ApiModel):
    profile_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    provider_id: str = Field(min_length=1, max_length=MAXIMUM_PROVIDER_ID_CHARACTERS)
    auth_mode: Literal["subscription", "api_key"]
    auth_profile_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class AgentConfigurationRevisionResource(ApiModel):
    model: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    auth_profile_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    executor_revision: str = Field(
        min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS
    )
    provider_id: str = Field(min_length=1, max_length=MAXIMUM_PROVIDER_ID_CHARACTERS)
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
    """A published V3 revision: its format, its size, and whether this build runs it.

    `executable` used to be the constant `false`, which was true while no runtime
    executed the format at all. It is derived now, from the one rule the start
    path applies, so a reader is told about this document rather than about
    version 3. `not_executable_reason` carries that rule's own words -- which node
    kind waits, which branch nothing chooses between, which authored form nothing
    binds -- because "not executable" alone leaves an author guessing at what to
    change.
    """

    format_version: Literal[3]
    executable: bool
    not_executable_reason: str | None
    node_count: int = Field(ge=1)
    name: str = Field(min_length=1)
    description: str | None

    @model_validator(mode="after")
    def validate_reason_shape(self) -> WorkflowGraphResourceV3:
        """A reason exists exactly when there is something to explain.

        The two fields are one answer, so they are checked as one: an executable
        revision carrying a reason, or a refused one carrying none, would each be
        a document the reader cannot act on.
        """
        if self.executable == (self.not_executable_reason is not None):
            raise ValueError(
                "a V3 revision names a reason exactly when it is not executable"
            )
        return self


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


class CatalogNameResolutionResource(ApiModel):
    """Which revision one catalog name resolves to, and nothing about running it."""

    display_name: str = Field(
        min_length=1, max_length=MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS
    )
    lineage_id: str = Field(pattern=REVISION_HASH_PATTERN)
    revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    revision_number: int = Field(ge=1)


class CatalogAdmissionResource(ApiModel):
    """Which lineage a revision now belongs to, and where in it it sits."""

    display_name: str = Field(
        min_length=1, max_length=MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS
    )
    lineage_id: str = Field(pattern=SHA256_HASH_PATTERN)
    revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    revision_number: int = Field(ge=1)


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
    not_executable_reason: str | None
    name: str | None
    description: str | None

    @model_validator(mode="after")
    def validate_reason_shape(self) -> WorkflowRevisionSummaryResourceV2:
        """The listing answers with the same rule the detail does, reason and all."""
        if self.executable == (self.not_executable_reason is not None):
            raise ValueError(
                "a listed revision names a reason exactly when it is not executable"
            )
        return self


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
    role: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    auth_profile_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    profile_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    provider_id: str = Field(min_length=1, max_length=MAXIMUM_PROVIDER_ID_CHARACTERS)
    auth_mode: Literal["subscription", "api_key"]
    model: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    executor_revision: str = Field(
        min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS
    )


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
    command_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
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
    agent_bindings: tuple[AgentBindingResourceV2, ...] = Field(
        max_length=MAXIMUM_RUN_AGENT_BINDINGS
    )
    state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    state: Literal["STARTED", "WAITING_RECONCILIATION", "WAITING_INPUT", "COMPLETED"]
    current_node: NodeResourceV2
    node_rail: tuple[NodeRailResource, ...] = Field(min_length=1)
    agent_attempts: tuple[AgentAttemptResourceV2, ...] = Field(
        max_length=REPLACEMENT_AGENT_ATTEMPT_ORDINAL
    )
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


class RunResourceV3(ApiModel):
    """One V3 run as it reads back: its own format, never a V2 one renumbered.

    It is a separate resource rather than a widened V2 one because the two
    disagree about what a state means. `RunResourceV2.validate_state_shape` ties
    COMPLETED to a subworkflow node and WAITING_INPUT to a Wait node; a V3 run
    ends on an **agent** sink, because #194 H1b lifted the terminal condition off
    the subworkflow node onto the run. Rendering a V3 run through those rules
    would have to call it something it is not.

    Two fields the V2 shape carries are absent on purpose rather than empty. A V3
    run surfaces no `agent_attempts`: the run projection fetches attempts only for
    a V2 agent node, so there is nothing to show, and a field that always answered
    the same would read as "no attempt ran". The rail's own `attempt` stays null
    for the same reason -- null already means "nothing to tell" in this vocabulary,
    not "no attempt exists". Surfacing V3 attempts is its own head with its own
    query change; it is a named gap here, not a claim.

    There is no `waiting` either: a V3 run cannot wait, because no runtime
    interprets a Wait node in this format yet, so the document that could reach
    that state is refused before it starts.
    """

    workflow_format_version: Literal[3]
    run_id: str = Field(min_length=1)
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    agent_binding_set_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    run_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    agent_bindings: tuple[AgentBindingResourceV2, ...] = Field(
        max_length=MAXIMUM_RUN_AGENT_BINDINGS
    )
    state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    state: Literal["STARTED", "COMPLETED"]
    current_node_id: str = Field(min_length=1)
    node_rail: tuple[NodeRailResource, ...] = Field(min_length=1)
    terminal_hash: str | None = Field(pattern=SHA256_HASH_PATTERN)
    latest_event_cursor: str | None = Field(pattern=EVENT_CURSOR_PATTERN)

    @model_validator(mode="after")
    def validate_state_shape(self) -> RunResourceV3:
        """A terminal hash exists exactly when the run has ended, and never before."""
        if (self.state == "COMPLETED") != (self.terminal_hash is not None):
            raise ValueError("V3 run state and terminal hash disagree")
        return self


AnyRunResource = RunResource | RunResourceV2 | RunResourceV3


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
