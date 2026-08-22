"""The schemas the API answers with: health, revisions, runs, pages, problems."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from atelier2.api.references import (
    EVENT_CURSOR_PATTERN,
    MAX_SIGNED_INT64,
    MAXIMUM_INVALID_FIELD_PATH_CHARACTERS,
    MAXIMUM_INVALID_FIELD_REASON_CHARACTERS,
    MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS,
    MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    MAXIMUM_RUN_AGENT_BINDINGS,
    PUBLIC_PROJECT_REFERENCE_PATTERN,
    PUBLIC_RUN_REFERENCE_PATTERN,
    REVISION_HASH_PATTERN,
    SHA256_HASH_PATTERN,
)
from atelier2.contracts.agent_attempts import (
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    AgentAttemptCancellationDisposition,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_PROVIDER_ID_CHARACTERS,
)
from atelier2.contracts.catalog_v3 import (
    MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS,
)
from atelier2.contracts.host_configuration import (
    MAXIMUM_OCCUPANCY_BINDINGS,
    MAXIMUM_PROJECT_ID_CHARACTERS,
    MAXIMUM_SERVED_PROJECTS,
)
from atelier2.contracts.run_projections import NodeState, PublicAgentAttemptState


class ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, serialize_by_alias=True
    )


class HealthResource(ApiModel):
    status: Literal["serving"]
    source_commit: str
    source_tree: str


class ArtifactResource(ApiModel):
    """The address of one published artifact, and nothing else.

    Publication is bytes in, address out. The content is not echoed: the caller
    already holds the exact bytes they posted, and the address is their identity.
    """

    artifact_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class SchemaRevisionResource(ApiModel):
    """The hash of one published schema revision, and nothing else.

    Publication is bytes in, hash out. The document is not echoed: the caller
    already holds the exact bytes they posted, and the hash is their identity.
    """

    schema_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class BudgetRevisionResource(ApiModel):
    """The hash of one published budget revision, and nothing else.

    Publication is bytes in, hash out, exactly as a schema's is. The bounds are
    not echoed: the caller already holds the exact bytes they posted, and the
    hash is what a node pins.
    """

    budget_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class ToolGrantRevisionResource(ApiModel):
    """The hash of one published tool-grant revision, and nothing else.

    Publication is bytes in, hash out, exactly as a schema's is. The document is
    not echoed: the caller already holds the exact bytes they posted, and the
    hash is what a node pins.
    """

    tool_grant_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class AdapterOperationRevisionResource(ApiModel):
    """The hash of one published adapter-operation revision, and nothing else.

    Publication is bytes in, hash out, exactly as a schema's is. The document is
    not echoed: the caller already holds the exact bytes they posted, and the
    hash is what an Action node pins.
    """

    adapter_operation_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


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
    requested_capability: Literal["headless", "headless_with_tools", "interactive"]
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class AgentConfigurationRevisionListItemResource(AgentConfigurationRevisionResource):
    """A listed configuration plus the host's current startability decision."""

    startable: bool
    not_startable_reason: Literal["agent-executor-binding-unavailable"] | None

    @model_validator(mode="after")
    def validates_startability_pair(self) -> AgentConfigurationRevisionListItemResource:
        if self.startable != (self.not_startable_reason is None):
            raise ValueError("agent configuration startability and reason disagree")
        return self


class AgentConfigurationRevisionPageResource(ApiModel):
    """One page of published agent configurations, in the item form already spoken.

    Publication and listing intentionally use distinct items: listing adds the
    host's current startability decision while POST keeps its immutable item.
    """

    items: tuple[AgentConfigurationRevisionListItemResource, ...]
    next_after_revision_hash: str | None = Field(pattern=REVISION_HASH_PATTERN)


class AuthProfileRevisionPageResource(ApiModel):
    """One page of published auth profiles, in the item form publication already speaks.

    The items are the existing revision resource: profile_id, revision, provider,
    auth mode, and hash. Secrets never entered that resource and do not enter
    the page.
    """

    items: tuple[AuthProfileRevisionResource, ...]
    next_after_revision_hash: str | None = Field(pattern=REVISION_HASH_PATTERN)


class OccupancyBindingResource(ApiModel):
    role: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ProjectResource(ApiModel):
    public_project_reference: str = Field(
        pattern=PUBLIC_PROJECT_REFERENCE_PATTERN,
        max_length=MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    )


class ProjectListResource(ApiModel):
    items: tuple[ProjectResource, ...] = Field(
        max_length=MAXIMUM_SERVED_PROJECTS, strict=False
    )


class OccupancyRevisionResource(ApiModel):
    project_id: str = Field(min_length=1, max_length=MAXIMUM_PROJECT_ID_CHARACTERS)
    public_project_reference: str = Field(
        pattern=PUBLIC_PROJECT_REFERENCE_PATTERN,
        max_length=MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    )
    lineage_id: str = Field(pattern=SHA256_HASH_PATTERN)
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    occupancy_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    bindings: tuple[OccupancyBindingResource, ...] = Field(
        max_length=MAXIMUM_OCCUPANCY_BINDINGS, strict=False
    )


class AgentReceiptResource(ApiModel):
    """One agent receipt, every binding dimension its hash folds, and no secret."""

    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)
    run_id: str = Field(min_length=1)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    node_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    role: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    binding_set_hash: str = Field(pattern=SHA256_HASH_PATTERN)
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
    executor_operational_identity: str = Field(
        min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS
    )
    output_base64: str
    output_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    receipt_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class RunReceiptResource(ApiModel):
    """The agent receipts a run has written.

    A run writes one receipt per completed agent node. A started run with no
    completion yet is an empty list. A missing run is a named refusal, not this
    resource.
    """

    items: tuple[AgentReceiptResource, ...]


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
    workflow_format_version: Literal[1]
    start_node_id: str = Field(min_length=1)
    nodes: tuple[NodeResource, ...]


class WorkflowGraphResourceV2(ApiModel):
    workflow_format_version: Literal[2]
    start_node_id: str = Field(min_length=1)
    nodes: tuple[NodeResourceV2, ...]


class WorkflowNodePreviewResourceV3(ApiModel):
    """One node of a published V3 revision, as an excerpt, never as the node.

    Full nodes would republish instruction, inputs, outputs, tools, and the rest.
    This resource answers only what a picker or a graph has to show: which node,
    which kind, which role, a bounded start of the instruction, and the control
    edges the author wrote. A node that declares no role or no instruction
    answers those fields empty -- the node's own answer, not a named refusal and
    not a stand-in invented to fill the column. A wait prompt is not an
    instruction and is not projected here. An entry node answers `depends_on`
    empty -- that absence is the authored edge set, not a missing column.
    """

    id: str = Field(min_length=1)
    kind: Literal["agent", "deterministic", "wait", "subworkflow", "action"]
    role: str | None = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    instruction_start: str | None = Field(
        min_length=1, max_length=MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS
    )
    depends_on: tuple[str, ...]

    @model_validator(mode="after")
    def validate_preview_shape(self) -> WorkflowNodePreviewResourceV3:
        """An agent names its role and instruction start; every other kind names neither."""
        has_role = self.role is not None
        has_start = self.instruction_start is not None
        if self.kind == "agent":
            if not has_role or not has_start:
                raise ValueError(
                    "an agent preview names its role and instruction start"
                )
        elif has_role or has_start:
            raise ValueError(
                "a node without an authored instruction answers with no excerpt"
            )
        return self


class WorkflowDeclaredSchemaResourceV3(ApiModel):
    """The author's own schema hull, as the document writes it.

    Inside `schema:` there is only one `ref` and one `revision` they can mean.
    Flattening them to `schema_ref` / `schema_revision` forced a consumer that
    re-authors a document to rebuild the hull under different names.
    """

    ref: str = Field(min_length=1)
    revision: str = Field(min_length=1)


class WorkflowDeclaredOrderResourceV3(ApiModel):
    """One order this document demands at start: its name and the schema it pinned.

    `schema` is the author's own hull — `ref` and `revision` — not the schema
    document. A caller that wants those bytes already holds the published
    revision the reference names.
    """

    name: str = Field(min_length=1)
    # Python cannot call this field `schema`: BaseModel already owns that name.
    schema_reference: WorkflowDeclaredSchemaResourceV3 = Field(alias="schema")


class WorkflowLoopVerdictResourceV3(ApiModel):
    """The node and verdict that close a round early, when the document names one.

    A loop the document declares may still repeat only on its round bound,
    with no earlier exit at all — that document carries no resource of this
    shape, rather than one with an invented verdict. Where one is declared,
    this is the node that closes the round and the one token, of the closed
    verdict vocabulary, whose answer sends the loop around again.
    """

    node: str = Field(min_length=1)
    verdict: Literal["accepted", "revise"]


class WorkflowLoopResourceV3(ApiModel):
    """One declared loop of a published V3 revision: its body and its bound.

    `member_node_ids` is the loop's one-line body, in the order the document
    walks it — the same node ids `node_previews` already names, never the
    nodes republished a second time. `maximum_rounds` is the bound that always
    holds; `repeat_while` is the earlier exit a verdict may grant, or absent
    where the document declares none.
    """

    id: str = Field(min_length=1)
    member_node_ids: tuple[str, ...] = Field(min_length=1)
    maximum_rounds: int = Field(ge=1, le=MAX_SIGNED_INT64)
    repeat_while: WorkflowLoopVerdictResourceV3 | None


# A docstring here is published as this component's description, so the reason
# the two authored fields carry no column of their own stays a comment: ADR 0007
# decision 4 has them parsed out of the published bytes on the way to the wire,
# which is what keeps this resource able only to repeat what the author wrote.
class WorkflowGraphResourceV3(ApiModel):
    """A published V3 revision: its format, its size, whether this build runs it, and an excerpt of each node.

    `executable` used to be the constant `false`, which was true while no runtime
    executed the format at all. It is derived now, from the one rule the start
    path applies, so a reader is told about this document rather than about
    version 3. `not_executable_reason` carries that rule's own words -- which node
    kind waits, which branch nothing chooses between, which authored form nothing
    binds -- because "not executable" alone leaves an author guessing at what to
    change.

    `node_previews` is that excerpt, not the authored document a second time.
    Full nodes stay refused: this resource has no column for instruction, inputs,
    outputs, or tools. A caller that wants the document already holds
    `document_base64`.
    """

    workflow_format_version: Literal[3]
    executable: bool
    not_executable_reason: str | None
    node_count: int = Field(ge=1)
    agent_roles: tuple[str, ...] = Field(max_length=MAXIMUM_RUN_AGENT_BINDINGS)
    """Every agent role this document binds, once each, in a stable order.

    A caller that wants to start this revision has to say which agent answers
    each role, and until now it could not learn the roles from the API at all --
    only by reading the document itself. The roles are the smallest thing that
    answers that: not the full nodes, which would put the whole authored
    document on the wire a second time, and not the count, which says nothing
    about what to bind. The excerpts sit beside this bind list.
    """

    orders: tuple[WorkflowDeclaredOrderResourceV3, ...]
    """Every order this document demands at start, in the order the author wrote.

    A caller that wants to start this revision has to supply each one, and until
    now it could not learn the names from the API at all -- only by reading the
    document itself. The names and the schema they pin are the smallest thing
    that answers that: not the schema bytes, which the published revision already
    holds, and not an empty list dressed as a placeholder.
    """

    node_previews: tuple[WorkflowNodePreviewResourceV3, ...] = Field(min_length=1)
    loops: tuple[WorkflowLoopResourceV3, ...]
    """Every loop this document declares, in the order the author wrote them.

    A graph with no declared loop answers empty, the document's own answer and
    not a gap this projection fills in. Each loop names its members by the ids
    `node_previews` already carries, so a renderer draws the box those ids
    outline without holding two copies of the same node.
    """

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
        if self.node_count != len(self.node_previews):
            raise ValueError("every published node has one preview")
        preview_roles = tuple(
            sorted({node.role for node in self.node_previews if node.role is not None})
        )
        if preview_roles != self.agent_roles:
            raise ValueError("agent roles and node excerpts disagree")
        names = tuple(order.name for order in self.orders)
        if len(set(names)) != len(names):
            raise ValueError("each declared order has one name")
        preview_ids = {node.id for node in self.node_previews}
        if any(
            dependency not in preview_ids
            for node in self.node_previews
            for dependency in node.depends_on
        ):
            raise ValueError("every depends_on names a published preview")
        loop_ids = tuple(loop.id for loop in self.loops)
        if len(set(loop_ids)) != len(loop_ids):
            raise ValueError("each declared loop has one id")
        for loop in self.loops:
            if any(member not in preview_ids for member in loop.member_node_ids):
                raise ValueError("every loop member names a published preview")
            if (
                loop.repeat_while is not None
                and loop.repeat_while.node not in loop.member_node_ids
            ):
                raise ValueError("a loop's verdict names one of its own members")
        return self


AnyWorkflowGraphResource = Annotated[
    WorkflowGraphResource | WorkflowGraphResourceV2 | WorkflowGraphResourceV3,
    Field(discriminator="workflow_format_version"),
]


class WorkflowRevisionSummaryResource(ApiModel):
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class WorkflowRevisionDetailResource(ApiModel):
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    document_base64: str
    graph: AnyWorkflowGraphResource


class CatalogNameResolutionResource(ApiModel):
    """Which revision one catalog name resolves to, and nothing about running it."""

    display_name: str = Field(
        min_length=1, max_length=MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS
    )
    lineage_id: str = Field(pattern=REVISION_HASH_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    revision_number: int = Field(ge=1)


class CatalogAdmissionResource(ApiModel):
    """Which lineage a revision now belongs to, and where in it it sits."""

    display_name: str = Field(
        min_length=1, max_length=MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS
    )
    lineage_id: str = Field(pattern=SHA256_HASH_PATTERN)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
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

    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    workflow_format_version: Literal[1, 2, 3]
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

# Same closed-union form: the served document keeps the five tokens inline
# instead of moving them behind an enum component. The members come from the
# owner; restating the strings here would let query and SSE drift apart.
CancellationDispositionName = Literal[
    AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
    AgentAttemptCancellationDisposition.EXITED_BEFORE_SIGNAL,
    AgentAttemptCancellationDisposition.REAPED_AFTER_TERM,
    AgentAttemptCancellationDisposition.REAPED_AFTER_KILL,
    AgentAttemptCancellationDisposition.OWNER_LOST_AFTER_PARENT_DEATH,
]


class AgentAttemptResourceV2(ApiModel):
    attempt_id: str = Field(pattern=SHA256_HASH_PATTERN)
    node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)
    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    attempt_ordinal: Literal[1, 2]
    state: PublicAttemptStateName
    failure_code: (
        Literal[
            "PROCESS_EXITED_UNSUCCESSFULLY",
            "PROCESS_OUTPUT_LIMIT_EXCEEDED",
            "PROCESS_SUPERVISION_FAILED",
        ]
        | None
    )
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
    disposition: CancellationDispositionName | None

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


class NodeAnswerResource(ApiModel):
    """What one node wrote, as the run kept it."""

    value_base64: str
    value_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class NodeProvenanceResource(ApiModel):
    """Which agent produced a node's answer, as its receipt recorded it.

    Usage is absent because no receipt holds it: this answer can prove what ran
    and what came out, and cannot say what it cost. How long it took is recorded
    beside the attempt, not on this receipt.
    """

    role: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    provider_id: str = Field(min_length=1, max_length=MAXIMUM_PROVIDER_ID_CHARACTERS)
    model: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    executor_revision: str = Field(
        min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS
    )
    executor_operational_identity: str = Field(
        min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS
    )
    auth_mode: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    profile_id: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    request_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    receipt_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class NodeDetailResource(ApiModel):
    """One node of a run, answered the way an operator asks about it.

    Four answers, each allowed to be absent, because absence is itself the
    answer. `job` is what the run really handed this node's provider, recomposed
    through the one owner that composed it; `job_hash` is the hash of exactly
    those bytes and nothing more. It is **not** the receipt's `request_hash`,
    which frames the execution identity, the revision, the binding and the
    operational identity around the job -- a reader comparing the two would
    reject a job that is right. `provenance.request_hash` is the field that
    meets the receipt.

    `answer` is what the node wrote, with the hash its own completion event
    kept. `provenance` is who did it. `refusal` is what stops the run here, and
    only that: a node whose predecessor has simply not written yet carries no
    job and no refusal, because nothing has judged anything. Anything that would
    mean the store disagrees with itself -- a payload that no longer matches its
    hash, a pinned schema revision that is gone -- is not softened into a
    refusal here; it leaves as durable corruption, loudly.
    """

    run_id: str = Field(min_length=1)
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    node_id: str = Field(min_length=1)
    state: NodeStateName
    job_base64: str | None
    job_hash: str | None = Field(pattern=SHA256_HASH_PATTERN)
    answer: NodeAnswerResource | None
    provenance: NodeProvenanceResource | None
    refusal: str | None
    started_at: str | None = None
    ended_at: str | None = None


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
    state: Literal[
        "STARTED", "WAITING_RECONCILIATION", "WAITING_INPUT", "COMPLETED", "FAILED"
    ]
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
        elif self.state == "FAILED":
            valid = (
                isinstance(self.current_node, AgentNodeResourceV2)
                and isinstance(self.waiting, NoWaitingResourceV2)
                and self.terminal_hash is not None
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
    run surfaces no `agent_attempts` array: that field is the V2 listing of every
    attempt on the current node, and repeating it next to the rail would be a
    second owner of the same fact. The rail's `attempt` is the one the reader is
    told -- including on a failed terminal node, where the snapshot keeps the
    failed attempt so a list read names the same ending the event stream names.
    Surfacing a V3 `agent_attempts` array is still its own head; it is a named
    gap here, not a claim.

    A V3 run does reach WAITING_INPUT, and `current_node_id` with the rail is
    what says so: the rail marks that node as the one owing a person a move. What
    is deliberately absent is a `waiting` block restating the question, because a
    V3 Wait node's prompt and answer schema are the document's and are read from
    the workflow revision this run names, not copied onto the run. Projecting the
    question onto the run resource is a named gap, not a claim.
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
    state: Literal[
        "STARTED", "WAITING_RECONCILIATION", "WAITING_INPUT", "COMPLETED", "FAILED"
    ]
    current_node_id: str = Field(min_length=1)
    node_rail: tuple[NodeRailResource, ...] = Field(min_length=1)
    terminal_hash: str | None = Field(pattern=SHA256_HASH_PATTERN)
    latest_event_cursor: str | None = Field(pattern=EVENT_CURSOR_PATTERN)
    started_at: str | None = None
    ended_at: str | None = None

    @model_validator(mode="after")
    def validate_state_shape(self) -> RunResourceV3:
        """A terminal hash exists exactly when the run has ended, and never before."""
        if (self.state in {"COMPLETED", "FAILED"}) != (self.terminal_hash is not None):
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


class InvalidFieldResource(ApiModel):
    """One request field a validation refusal can name.

    `path` is the loc the framework already walked, joined; `reason` is that
    error's own message. The pair is a pointer, not a second problem type.
    """

    path: str = Field(min_length=1, max_length=MAXIMUM_INVALID_FIELD_PATH_CHARACTERS)
    reason: str = Field(
        min_length=1, max_length=MAXIMUM_INVALID_FIELD_REASON_CHARACTERS
    )


class ProblemResource(ApiModel):
    type: str
    title: str
    status: int
    detail: str
    invalid_fields: tuple[InvalidFieldResource, ...] | None = None

    @model_serializer(mode="wrap")
    def omit_absent_field_pointers(
        self, serializer: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        dumped = serializer(self)
        if dumped.get("invalid_fields") is None:
            dumped.pop("invalid_fields", None)
        return dumped


class StreamFailureResource(ApiModel):
    """The terminal event-stream frame: this stream ended because it failed.

    It carries the same problem body the REST surface would answer, so an
    operator and a machine consumer read one problem vocabulary whether the
    failure was decided before the response headers or after them.
    """

    event: Literal["STREAM_FAILED"] = "STREAM_FAILED"
    problem: ProblemResource
