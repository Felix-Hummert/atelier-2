"""The schemas the API answers with: health, revisions, runs, pages, problems."""

from __future__ import annotations

from enum import StrEnum
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
    MAXIMUM_PUBLIC_SOURCE_REFERENCE_CHARACTERS,
    MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS,
    MAXIMUM_RUN_AGENT_BINDINGS,
    MAXIMUM_RUN_ORDERS,
    PUBLIC_PROJECT_REFERENCE_PATTERN,
    PUBLIC_RUN_REFERENCE_PATTERN,
    PUBLIC_SOURCE_REFERENCE_PATTERN,
    REVISION_HASH_PATTERN,
    SHA256_HASH_PATTERN,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
)
from atelier2.contracts.agent_definitions import (
    MAXIMUM_AGENT_DEFINITION_DOCUMENT_CHARACTERS,
    MAXIMUM_AGENT_DEFINITION_TOOL_COUNT,
)
from atelier2.contracts.agent_transcripts import (
    MAXIMUM_TRANSCRIPT_STEP_CHARACTERS,
    TranscriptEventKind,
    TranscriptMomentOrigin,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    MAXIMUM_PROVIDER_ID_CHARACTERS,
    PROVIDER_ID_PATTERN,
)
from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.contracts.catalog_v3 import (
    MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS,
)
from atelier2.contracts.host_configuration import (
    EXACT_MODEL_ID_PATTERN,
    MAXIMUM_ACTIVE_PROJECT_SOURCES,
    MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
    MAXIMUM_MODEL_REGISTRY_ENTRIES,
    MAXIMUM_PROJECT_ID_CHARACTERS,
    MAXIMUM_PROJECT_MODEL_DEFAULTS,
    MAXIMUM_SERVED_PROJECTS,
    MAXIMUM_SOURCE_ADDRESS_CHARACTERS,
    MAXIMUM_SOURCE_KIND_CHARACTERS,
    SourceConnectionAuthMethod,
)
from atelier2.contracts.queue_projection import (
    MAXIMUM_QUEUE_ACTIVE_RUNS,
    MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS,
    MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS,
    MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS,
    MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
    QueueAutomationDisposition,
    QueueBlockerKind,
    QueueDecisionAuthority,
    QueueItemState,
)
from atelier2.contracts.run_forks import MAXIMUM_RUN_FORK_SUCCESSORS
from atelier2.contracts.run_projections import NodeState, PublicAgentAttemptState
from atelier2.contracts.when import RECORDED_AT_PATTERN


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


class AgentDefinitionRevisionResource(ApiModel):
    """The hash of one published agent-definition revision, and nothing else.

    Publication is bytes in, hash out, exactly as a schema's is. The document is
    not echoed: the caller already holds the exact authored bytes they posted,
    and the hash is their identity.
    """

    agent_definition_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class AgentDefinitionRevisionListItemResource(ApiModel):
    """One published agent definition, read back into what its author named it.

    Listing echoes the two authored fields publication does not, because a
    reader browsing the catalog holds no hash to recognise and needs the name.

    It stops there. An imported agent is provider-bound and passed through
    whole, so the rest of the file — its model, its tool declaration, its
    prompt — is one provider's runtime contract, not a capability this catalog
    may re-serve as if it were portable. Whoever must read those bytes reads
    the revision itself.
    """

    agent_definition_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)


class AgentDefinitionRevisionPageResource(ApiModel):
    items: tuple[AgentDefinitionRevisionListItemResource, ...]
    next_after_revision_hash: str | None = Field(pattern=REVISION_HASH_PATTERN)


class AgentDefinitionRevisionDetailResource(ApiModel):
    """One published agent definition, parsed into every field its author wrote.

    Where the list item stops at name and description
    (`AgentDefinitionRevisionListItemResource`), a caller holding the hash asked
    to read the revision itself, so this answers the whole authored file: the
    provider mark the author proposed, the system prompt, and the declared
    tools. `model` is absent exactly where the file proposed none -- the
    deployment's own model decides then, not this door. `tools` is absent
    exactly where the file declared none -- every tool the executor offers --
    and the declared names otherwise.

    `system_prompt` and each tool name are bounded by
    `MAXIMUM_AGENT_DEFINITION_DOCUMENT_CHARACTERS`, the whole-document ceiling
    the publish door already enforces (`parse_agent_definition`): neither can
    outgrow the document it was parsed from. `tools` itself is bounded by
    `MAXIMUM_AGENT_DEFINITION_TOOL_COUNT`, the same count `DeclaredTools`
    refuses past.
    """

    agent_definition_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    model: str | None = Field(default=None, min_length=1)
    system_prompt: str = Field(
        min_length=1, max_length=MAXIMUM_AGENT_DEFINITION_DOCUMENT_CHARACTERS
    )
    tools: (
        tuple[
            Annotated[
                str,
                Field(
                    min_length=1,
                    max_length=MAXIMUM_AGENT_DEFINITION_DOCUMENT_CHARACTERS,
                ),
            ],
            ...,
        ]
        | None
    ) = Field(default=None, max_length=MAXIMUM_AGENT_DEFINITION_TOOL_COUNT)


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
    """A listed configuration plus the host's two independent startability answers.

    `startable` is the receipt-gated answer: an operator's own Start control
    gates on it, and it is the same answer a real start door acts on.
    `structurally_startable` asks only whether a factory is registered,
    available, and declares the capability, with no live evidence asked at
    all -- the live provider canary's own discovery reads that one, because it
    exists to produce the evidence `startable` is missing and could never find
    a vector to probe through a question that already requires the evidence.
    `startable` cannot hold without `structurally_startable`.
    """

    startable: bool
    structurally_startable: bool
    not_startable_reason: (
        Literal["agent-executor-binding-unavailable", "provider-probe-receipt-missing"]
        | None
    )

    @model_validator(mode="after")
    def validates_startability_pair(self) -> AgentConfigurationRevisionListItemResource:
        if self.startable and not self.structurally_startable:
            raise ValueError(
                "agent configuration startability cannot hold without its own "
                "structural startability"
            )
        expected_reason = (
            None
            if self.startable
            else "agent-executor-binding-unavailable"
            if not self.structurally_startable
            else "provider-probe-receipt-missing"
        )
        if self.not_startable_reason != expected_reason:
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


class ProjectResource(ApiModel):
    public_project_reference: str = Field(
        pattern=PUBLIC_PROJECT_REFERENCE_PATTERN,
        max_length=MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    )


class ProjectListResource(ApiModel):
    items: tuple[ProjectResource, ...] = Field(
        max_length=MAXIMUM_SERVED_PROJECTS, strict=False
    )


class ProjectSourceConnectionRevisionResource(ApiModel):
    public_project_reference: str = Field(
        pattern=PUBLIC_PROJECT_REFERENCE_PATTERN,
        max_length=MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    )
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    source_kind: str = Field(min_length=1, max_length=MAXIMUM_SOURCE_KIND_CHARACTERS)
    source_address: str = Field(
        min_length=1, max_length=MAXIMUM_SOURCE_ADDRESS_CHARACTERS
    )
    auth_method: SourceConnectionAuthMethod
    project_source_connection_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ProjectSourceResource(ApiModel):
    public_source_reference: str = Field(
        pattern=PUBLIC_SOURCE_REFERENCE_PATTERN,
        max_length=MAXIMUM_PUBLIC_SOURCE_REFERENCE_CHARACTERS,
    )
    kind: str = Field(min_length=1, max_length=MAXIMUM_SOURCE_KIND_CHARACTERS)
    address: str = Field(min_length=1, max_length=MAXIMUM_SOURCE_ADDRESS_CHARACTERS)
    scope: Literal["issues"] = "issues"
    connected_at: str | None = Field(default=None, pattern=RECORDED_AT_PATTERN)
    revision: int = Field(ge=1, le=MAX_SIGNED_INT64)
    auth_method: SourceConnectionAuthMethod


class ProjectSourceListResource(ApiModel):
    items: tuple[ProjectSourceResource, ...] = Field(
        max_length=MAXIMUM_ACTIVE_PROJECT_SOURCES, strict=False
    )


class ModelRegistryEntryResource(ApiModel):
    model_id: str = Field(
        min_length=1,
        max_length=MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
        pattern=EXACT_MODEL_ID_PATTERN,
    )
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    source: Literal["discovered", "operator"]
    provider_check: Literal["not-checked", "checked", "unknown-at-provider"]


class ModelRegistryRevisionResource(ApiModel):
    provider_id: str = Field(
        min_length=1,
        max_length=MAXIMUM_PROVIDER_ID_CHARACTERS,
        pattern=PROVIDER_ID_PATTERN,
    )
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    model_registry_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    entries: tuple[ModelRegistryEntryResource, ...] = Field(
        max_length=MAXIMUM_MODEL_REGISTRY_ENTRIES, strict=False
    )


class ProjectModelDefaultResource(ApiModel):
    difficulty: Literal[1, 2, 3]
    model_registry_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    provider_id: str = Field(
        min_length=1,
        max_length=MAXIMUM_PROVIDER_ID_CHARACTERS,
        pattern=PROVIDER_ID_PATTERN,
    )
    model_id: str = Field(
        min_length=1,
        max_length=MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
        pattern=EXACT_MODEL_ID_PATTERN,
    )
    agent_configuration_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class ProjectModelDefaultsRevisionResource(ApiModel):
    project_id: str = Field(min_length=1, max_length=MAXIMUM_PROJECT_ID_CHARACTERS)
    public_project_reference: str = Field(
        pattern=PUBLIC_PROJECT_REFERENCE_PATTERN,
        max_length=MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    )
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    project_model_defaults_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    defaults: tuple[ProjectModelDefaultResource, ...] = Field(
        max_length=MAXIMUM_PROJECT_MODEL_DEFAULTS, strict=False
    )


class RoleModelResolutionResource(ApiModel):
    role: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    agent_configuration_revision_hash: str | None = Field(
        ..., pattern=SHA256_HASH_PATTERN
    )
    source: Literal["chosen-now", "pinned-in-workflow", "from-project", "uncast"]
    model_id: str | None = Field(
        ...,
        min_length=1,
        max_length=MAXIMUM_EXACT_MODEL_ID_CHARACTERS,
        pattern=EXACT_MODEL_ID_PATTERN,
    )
    declared_difficulty: Literal[1, 2, 3]
    default_difficulty: Literal[1, 2, 3] | None = Field(...)
    uncast_reason: (
        Literal[
            "override-not-registered",
            "workflow-model-not-registered",
            "workflow-model-ambiguous",
            "no-project-default",
            "family-difference-unavailable",
        ]
        | None
    ) = Field(...)
    family_differs_from: str | None = Field(
        ...,
        min_length=1,
        max_length=MAXIMUM_AGENT_FIELD_CHARACTERS,
    )


class ProjectModelResolutionResource(ApiModel):
    project_id: str = Field(min_length=1, max_length=MAXIMUM_PROJECT_ID_CHARACTERS)
    public_project_reference: str = Field(
        pattern=PUBLIC_PROJECT_REFERENCE_PATTERN,
        max_length=MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    )
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    resolutions: tuple[RoleModelResolutionResource, ...] = Field(
        max_length=MAXIMUM_RUN_AGENT_BINDINGS, strict=False
    )


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


class WaitAnswerSchemaResourceV3(ApiModel):
    """One waiting node's answer schema, classified as far as an excerpt may.

    This is an excerpt of the schema's own top level, never a second evaluator:
    the real judgement of a submitted answer against this schema stays
    `schemas_v3`'s alone (`atelier2.api.projection.workflows` reads only enough
    of the top level to classify it). `kind` names only what that top level
    itself says -- `boolean` where it names `type: boolean`, `enum` where it
    names `enum` (`values` then carries the author's own members, each the
    exact JSON text that member already is) -- and `free` for every other
    shape this excerpt declines to guess at, including one it cannot resolve
    or read at all. A schema a document names but this build cannot yet see
    is not durable corruption: a document may name a schema published after
    itself, exactly as `WorkflowDeclaredOrderResourceV3` echoes its own hull
    unresolved, so an unreadable schema classifies `free` rather than refusing
    the whole graph over a reference nothing has bound yet.
    """

    node_id: str = Field(min_length=1)
    # Python cannot call this field `schema`: BaseModel already owns that name.
    schema_reference: WorkflowDeclaredSchemaResourceV3 = Field(alias="schema")
    kind: Literal["boolean", "enum", "free"]
    values: tuple[str, ...] | None = None
    """The author's own `enum` members, present exactly when `kind` is `enum`."""

    @model_validator(mode="after")
    def validate_values_shape(self) -> WaitAnswerSchemaResourceV3:
        if (self.kind == "enum") != (self.values is not None):
            raise ValueError("values names the enum's own members, and only those")
        return self


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

    wait_answer_schemas: tuple[WaitAnswerSchemaResourceV3, ...]
    """One entry per wait node, naming its answer schema and this excerpt's own
    classification of it.

    A caller that wants to render a Wait node's answer as clickable decision
    buttons instead of a free-form field has to know its schema's shape, and
    until now this resource named no wait node's answer schema at all -- only
    its prompt, which lives on the run rather than the graph. This is the same
    class of answer `orders` already is for the material a start must supply:
    the schema hull the document pinned, never the schema's own bytes.
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
        wait_ids = tuple(node.id for node in self.node_previews if node.kind == "wait")
        schema_ids = tuple(entry.node_id for entry in self.wait_answer_schemas)
        if sorted(schema_ids) != sorted(wait_ids):
            raise ValueError("every wait node preview names exactly one answer schema")
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


class WorkflowRevisionSummaryResource(ApiModel):
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class WorkflowRevisionDetailResource(ApiModel):
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    document_base64: str
    graph: WorkflowGraphResourceV3


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


class QueuePriorityRankResource(ApiModel):
    rank: int = Field(ge=1, le=MAX_SIGNED_INT64)


class QueueProposalResource(ApiModel):
    revision: int = Field(ge=1, le=MAX_SIGNED_INT64)
    priority: QueuePriorityRankResource
    workflow_lineage_id: str = Field(pattern=SHA256_HASH_PATTERN)
    prerequisite_item_ids: tuple[str, ...]
    automation_disposition: QueueAutomationDisposition
    policy_revision: int | None = Field(default=None, ge=1, le=MAX_SIGNED_INT64)


class QueueAdmissionResource(ApiModel):
    proposal_revision: int | None = Field(default=None, ge=1, le=MAX_SIGNED_INT64)
    authority: QueueDecisionAuthority | None
    rationale: str = Field(
        min_length=1, max_length=MAXIMUM_QUEUE_ADMISSION_RATIONALE_CHARACTERS
    )


class QueueLaunchBindingResource(ApiModel):
    proposal_revision: int = Field(ge=1, le=MAX_SIGNED_INT64)
    run_id: str = Field(min_length=1)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class QueueItemResource(ApiModel):
    """One exhaustive durable queue row; tracker display enrichment is explicit."""

    project_id: str = Field(min_length=1, max_length=MAXIMUM_PROJECT_ID_CHARACTERS)
    tracker_item_reference: str = Field(
        min_length=1, max_length=MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS
    )
    item_id: str = Field(pattern=SHA256_HASH_PATTERN)
    state: QueueItemState
    revision: int = Field(ge=0, le=MAX_SIGNED_INT64)
    proposal: QueueProposalResource | None
    admission: QueueAdmissionResource | None
    launch_binding: QueueLaunchBindingResource | None
    blockers: tuple[QueueBlockerKind, ...]
    tracker_enrichment: Literal["ENRICHMENT_UNAVAILABLE"]
    title: str | None = Field(
        min_length=1,
        max_length=MAXIMUM_QUEUE_ITEM_TITLE_CHARACTERS,
        description=(
            "The tracker title last observed at title_observed_at; an observation, "
            "not current truth."
        ),
    )
    title_observed_at: str | None = Field(
        pattern=RECORDED_AT_PATTERN,
        description="When the tracker title was last observed.",
    )
    retired_at: str | None = Field(
        pattern=RECORDED_AT_PATTERN,
        description="When import observed the item absent from the tracker's open set.",
    )


class QueueItemPageResource(ApiModel):
    items: tuple[QueueItemResource, ...]
    next_after: str | None = Field(pattern=SHA256_HASH_PATTERN)


class QueueProjectPolicyResource(ApiModel):
    project_id: str = Field(min_length=1, max_length=MAXIMUM_PROJECT_ID_CHARACTERS)
    revision_number: int = Field(ge=1, le=MAX_SIGNED_INT64)
    maximum_active_runs: int = Field(ge=1, le=MAXIMUM_QUEUE_ACTIVE_RUNS)
    automation_label: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAXIMUM_QUEUE_AUTOMATION_LABEL_CHARACTERS,
    )


class QueueProposalDecisionResource(ApiModel):
    item_id: str = Field(pattern=SHA256_HASH_PATTERN)
    state: Literal[QueueItemState.PROPOSED]
    revision: int = Field(ge=1, le=MAX_SIGNED_INT64)
    proposal: QueueProposalResource


class QueueAdmissionDecisionResource(ApiModel):
    item_id: str = Field(pattern=SHA256_HASH_PATTERN)
    state: Literal[QueueItemState.ADMITTED]
    revision: int = Field(ge=1, le=MAX_SIGNED_INT64)
    admission: QueueAdmissionResource


class ProjectSourceImportResource(ApiModel):
    """What one import observed: the open items seen, and how many were new."""

    observed: int = Field(ge=0)
    newly_observed: int = Field(ge=0)


class WorkflowRevisionPageResource(ApiModel):
    items: tuple[WorkflowRevisionSummaryResource, ...]
    next_after_revision_hash: str | None = Field(pattern=REVISION_HASH_PATTERN)


class WorkflowRevisionSummaryResourceV2(ApiModel):
    """A listed revision: its hash, its format, and what its own bytes call it.

    A format-3 document always declares a name, so `name` is never absent here.
    `description` is absent where the document declares none, which is the
    truthful answer rather than a line invented to fill the column.
    """

    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    workflow_format_version: Literal[3]
    executable: bool
    not_executable_reason: str | None
    name: str
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
    reused_from_run_reference: str | None = Field(
        default=None,
        pattern=PUBLIC_RUN_REFERENCE_PATTERN,
        exclude_if=lambda value: value is None,
    )
    source_event_hash: str | None = Field(
        default=None,
        pattern=SHA256_HASH_PATTERN,
        exclude_if=lambda value: value is None,
    )
    source_receipt_hash: str | None = Field(
        default=None,
        pattern=SHA256_HASH_PATTERN,
        exclude_if=lambda value: value is None,
    )
    source_declared_context_package_hash: str | None = Field(
        default=None,
        pattern=SHA256_HASH_PATTERN,
        exclude_if=lambda value: value is None,
    )

    @model_validator(mode="after")
    def validate_reuse_shape(self) -> NodeRailResource:
        values = (
            self.reused_from_run_reference,
            self.source_event_hash,
            self.source_receipt_hash,
            self.source_declared_context_package_hash,
        )
        if any(value is None for value in values) and not all(
            value is None for value in values
        ):
            raise ValueError("a reused rail node names its complete source evidence")
        if self.reused_from_run_reference is not None and self.state != "succeeded":
            raise ValueError("only a succeeded rail node can be reused")
        return self


class NodeAnswerResource(ApiModel):
    """What one node wrote, as the run kept it."""

    value_base64: str
    value_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class NodeRefusalOutputResource(ApiModel):
    """The redacted presentation of a schema-refused V3 agent node's own output.

    Not `NodeAnswerResource`, on purpose, and not just for the field name
    `answer` already carries: `answer` is served for every answer-bearing node
    kind (#238) -- Agent, Wait, Action, Subworkflow -- whose payloads have no
    one shared byte bound, so `NodeAnswerResource.value_base64` stays
    unbounded. `refusal_output` is served for exactly one narrower case: a V3
    agent node's own execution, judged and refused (#664). Every executor
    adapter already refuses to hand the domain more than
    `MAXIMUM_AGENT_OUTPUT_BYTES_V2` bytes before that judgment runs, so this
    field's own bound (`MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS`) rests on
    that already-held cap rather than inventing a new one.

    `value_base64` is also not the exact judged bytes: `contracts.secret_redaction`
    runs over them before this resource is built, and any credential-shaped span
    is replaced (#664). Replacing a short credential with the longer
    `REDACTION_MARKER` can make the text longer than what was judged, never
    shorter than the cap alone would suggest -- so `MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS`
    is derived through the same redaction owner's own `maximum_redacted_length`,
    not from the agent output cap directly, and a value at the cap with the
    shortest credential shape redacted out of it still fits. `value_hash`, by
    contrast, is untouched -- it is the receipt's own hash of the original,
    unredacted bytes, so it still proves what the schema owner judged even
    though the text shown beside it is a presentation of that judgment, not a
    preimage of the hash. A reader who rehashes `value_base64` to check it
    against `value_hash` is comparing two different things on purpose.
    """

    value_base64: str = Field(max_length=MAXIMUM_REFUSED_OUTPUT_BASE64_CHARACTERS)
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


class TranscriptRecordedMomentResource(ApiModel):
    """The instant an event entered the transcript and the source of that instant."""

    recorded_at: str = Field(pattern=RECORDED_AT_PATTERN)
    origin: Literal[TranscriptMomentOrigin.RECORDED]


class TranscriptBeforeMomentsOrigin(StrEnum):
    """The only explicit marker for a transcript that predates event moments."""

    V1 = "v1-before-moments"


class TranscriptBeforeMomentsResource(ApiModel):
    """An event read from a v1 transcript, before event moments existed."""

    origin: Literal[TranscriptBeforeMomentsOrigin.V1]


TranscriptMomentResource = Annotated[
    TranscriptRecordedMomentResource | TranscriptBeforeMomentsResource,
    Field(discriminator="origin"),
]


class ToolCalledEventResource(ApiModel):
    event: Literal[TranscriptEventKind.TOOL_CALLED]
    name: str = Field(max_length=MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
    arguments: str = Field(max_length=MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
    redacted: bool
    moment: TranscriptMomentResource


class ToolReturnedEventResource(ApiModel):
    event: Literal[TranscriptEventKind.TOOL_RETURNED]
    name: str = Field(max_length=MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
    result: str = Field(max_length=MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
    redacted: bool
    moment: TranscriptMomentResource


class AssistantTurnEventResource(ApiModel):
    event: Literal[TranscriptEventKind.ASSISTANT_TURN]
    text: str = Field(max_length=MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
    redacted: bool
    moment: TranscriptMomentResource


class UsageEventResource(ApiModel):
    event: Literal[TranscriptEventKind.USAGE]
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(ge=0)
    cache_creation_input_tokens: int = Field(ge=0)
    moment: TranscriptMomentResource


class ProviderTerminalRefusalEventResource(ApiModel):
    event: Literal[TranscriptEventKind.PROVIDER_TERMINAL_REFUSAL]
    terminal_reason: str = Field(max_length=MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
    api_error_status: str = Field(max_length=MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
    text: str = Field(max_length=MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
    redacted: bool
    moment: TranscriptMomentResource


class UnrecognisedProviderOutputEventResource(ApiModel):
    event: Literal[TranscriptEventKind.UNRECOGNISED_PROVIDER_OUTPUT]
    text: str = Field(max_length=MAXIMUM_TRANSCRIPT_STEP_CHARACTERS)
    redacted: bool
    moment: TranscriptMomentResource


class TranscriptTruncatedEventResource(ApiModel):
    event: Literal[TranscriptEventKind.TRANSCRIPT_TRUNCATED]
    dropped_events: int = Field(ge=1)
    moment: TranscriptMomentResource


AttemptTranscriptEventResource = Annotated[
    ToolCalledEventResource
    | ToolReturnedEventResource
    | AssistantTurnEventResource
    | UsageEventResource
    | ProviderTerminalRefusalEventResource
    | UnrecognisedProviderOutputEventResource
    | TranscriptTruncatedEventResource,
    Field(discriminator="event"),
]


class AttemptTranscriptResource(ApiModel):
    """The decoded, already-redacted steps of one attempt, and nothing else.

    The document kind and the stored bytes stay off the wire: a reader of this
    resource is looking at the events, not at how they were kept.
    """

    events: tuple[AttemptTranscriptEventResource, ...] = Field(min_length=1)


class NodeDetailResource(ApiModel):
    """One node of a run, answered the way an operator asks about it.

    Five answers, each allowed to be absent, because absence is itself the
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

    `refusal_output` is deliberately not `answer`: `answer` is the value the run
    accepted, and a schema-refused value never was that (#664). It carries a
    redacted presentation of the bytes a schema owner judged and refused, read
    back from the content-addressed artifact the failure transaction kept, its
    credential shapes replaced before this resource is built -- present only
    where such a judgment happened and something to keep survived it; every
    other refusal, including one this receipt family predates, answers with no
    such field rather than a guess. Its own type, `NodeRefusalOutputResource`,
    names the redaction and the bound this field alone carries.

    `transcript` is the decoded events of the attempt that named one. The key
    is omitted when no attempt of this execution did -- never `"transcript":
    null` -- so a node that stored nothing does not grow a new column. The
    stored document kind and raw bytes stay off this resource.
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
    refusal_output: NodeRefusalOutputResource | None = None
    started_at: str | None = None
    ended_at: str | None = None
    transcript: AttemptTranscriptResource | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )


# The reason vocabulary the wire spells is `RunCancellationRefusal`'s closed set
# (its owner in `contracts/run_projections.py`), written out here because the
# wire schema may name no contract enum inline; `api/projection/runs.py` casts
# the enum's value into it, past pyright, so
# `test_the_wire_reason_literal_and_the_refusal_enum_cannot_drift` pins the two
# spellings to set equality and fails the moment either side drifts.
RunNotCancellableReasonName = Literal[
    "between-nodes",
    "waiting-for-you",
    "node-runs-no-agent",
    "already-cancelling",
    "already-ended",
    "answer-in-flight",
]


class RunCancellabilityResource(ApiModel):
    """Whether this V3 run can be operator-cancelled right now, said by the server.

    #439 D3 makes the server the owner of this predicate rather than letting the
    cockpit guess it from the rail. A cancellable run carries the
    `target_node_execution_id` the client fences its command on; a run that
    cannot be cancelled carries the closed reason token that names why, so the
    cockpit shows the operator sentence instead of a grey nothing.
    """

    cancellable: bool
    reason: RunNotCancellableReasonName | None
    target_node_execution_id: str | None = Field(pattern=SHA256_HASH_PATTERN)

    @model_validator(mode="after")
    def validate_predicate_shape(self) -> RunCancellabilityResource:
        if self.cancellable != (self.target_node_execution_id is not None):
            raise ValueError("a cancellable run names its target node execution")
        if self.cancellable != (self.reason is None):
            raise ValueError("a non-cancellable run names exactly one reason")
        return self


class RunOrderResource(ApiModel):
    """One order a V3 run was started with, told safely -- never its own bytes.

    An order's material can be a secret a caller pasted by mistake, or an
    artifact up to `MAXIMUM_ARTIFACT_BYTES`, and this resource is served on
    every listed run -- so it never echoes the order's bytes at all.

    `bytes` is how large the order's material is. `schema_revision_hash` is
    the schema the order satisfies -- the workflow revision's own
    `WorkflowDeclaredOrderResourceV3` already names the `ref` a caller reads,
    so this is only the identity a caller compares it against.

    No text preview travels here yet: a redacted glance at an order's material
    needs the redaction owner #666 is building, and shipping a second one here
    ahead of it would be a parallel copy of a decision that head has not made
    yet. #738's own body carries that as its named next step.
    """

    name: str = Field(min_length=1)
    bytes: int = Field(ge=0, le=MAXIMUM_ARTIFACT_BYTES)
    schema_revision_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class RunForkOriginResource(ApiModel):
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    terminal_hash: str = Field(pattern=SHA256_HASH_PATTERN)
    restart_from_node_id: str = Field(min_length=1)
    fork_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class RunForkSuccessorResource(ApiModel):
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    restart_from_node_id: str = Field(min_length=1)
    fork_hash: str = Field(pattern=SHA256_HASH_PATTERN)


class RunResourceV3(ApiModel):
    """One run as it reads back, ended on the sink its author declared.

    A run ends on an **agent** sink, because #194 H1b lifted the terminal
    condition off the subworkflow node onto the run; `validate_state_shape`
    therefore ties the terminal hash to the run's own state and to no node kind.

    Two fields are absent on purpose rather than empty. There is no
    `agent_attempts` array listing every attempt on the current node: repeating
    it next to the rail would be a second owner of the same fact. The rail's
    `attempt` is the one the reader is told -- including on a failed terminal
    node, where the snapshot keeps the failed attempt so a list read names the
    same ending the event stream names. Surfacing an `agent_attempts` array is
    still its own head; it is a named gap here, not a claim.

    A run does reach WAITING_INPUT, and `current_node_id` with the rail is what
    says so: the rail marks that node as the one owing a person a move. What is
    deliberately absent is a `waiting` block restating the question, because a
    Wait node's prompt and answer schema are the document's and are read from
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
    orders: tuple[RunOrderResource, ...] = Field(max_length=MAXIMUM_RUN_ORDERS)
    fork_origin: RunForkOriginResource | None = None
    fork_successors: tuple[RunForkSuccessorResource, ...] = Field(
        default=(), max_length=MAXIMUM_RUN_FORK_SUCCESSORS
    )
    """Every order this run was started with, in the order the store returns them.

    A run's purpose is what these were, not a guess parsed back out of one
    agent node's composed job text (PR #736 review, RESLICE): these come from
    `run_inputs_v3` untouched by any node's job, so a reader learns why a run
    started without reading a node, without parsing anything, and without the
    order's own bytes ever reaching this resource (review 25.08.: RunResourceV3
    is served on every listed run, and an order can be a secret or up to an
    artifact's own size).
    """
    state_version: int = Field(ge=0, le=MAX_SIGNED_INT64)
    state: Literal[
        "STARTED",
        "WAITING_RECONCILIATION",
        "WAITING_INPUT",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
    ]
    current_node_id: str = Field(min_length=1)
    current_node_execution_id: str = Field(pattern=SHA256_HASH_PATTERN)
    node_rail: tuple[NodeRailResource, ...] = Field(min_length=1)
    cancellation: RunCancellabilityResource
    terminal_hash: str | None = Field(pattern=SHA256_HASH_PATTERN)
    latest_event_cursor: str | None = Field(pattern=EVENT_CURSOR_PATTERN)
    started_at: str | None = None
    ended_at: str | None = None

    @model_validator(mode="after")
    def validate_state_shape(self) -> RunResourceV3:
        """A terminal hash exists exactly when the run has ended, and never before."""
        if (self.state in {"COMPLETED", "FAILED", "CANCELLED"}) != (
            self.terminal_hash is not None
        ):
            raise ValueError("V3 run state and terminal hash disagree")
        return self


class VersionedRunPageResource(ApiModel):
    items: tuple[RunResourceV3, ...]
    next_after: str | None = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)


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
        "FORK_REFERENCE",
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


class UncastRoleResource(ApiModel):
    role: str = Field(min_length=1, max_length=MAXIMUM_AGENT_FIELD_CHARACTERS)
    reason: Literal[
        "override-not-registered",
        "workflow-model-not-registered",
        "workflow-model-ambiguous",
        "no-project-default",
        "family-difference-unavailable",
    ]
    family_differs_from: str | None = Field(
        default=None,
        min_length=1,
        max_length=MAXIMUM_AGENT_FIELD_CHARACTERS,
    )


class ProblemResource(ApiModel):
    type: str
    title: str
    status: int
    detail: str
    invalid_fields: tuple[InvalidFieldResource, ...] | None = None
    uncast_roles: tuple[UncastRoleResource, ...] | None = None

    @model_serializer(mode="wrap")
    def omit_absent_field_pointers(
        self, serializer: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        dumped = serializer(self)
        if dumped.get("invalid_fields") is None:
            dumped.pop("invalid_fields", None)
        if dumped.get("uncast_roles") is None:
            dumped.pop("uncast_roles", None)
        return dumped


class DurableStateCorruptProblemResource(ApiModel):
    """The durable-state-corrupt problem named on one unprojectable attention run."""

    type: Literal["urn:atelier2:problem:v1:durable-state-corrupt"]
    title: Literal["Durable state is corrupt"]
    status: Literal[500]
    detail: str


class StreamFailureResource(ApiModel):
    """The terminal event-stream frame: this stream ended because it failed.

    It carries the same problem body the REST surface would answer, so an
    operator and a machine consumer read one problem vocabulary whether the
    failure was decided before the response headers or after them.
    """

    event: Literal["STREAM_FAILED"] = "STREAM_FAILED"
    problem: ProblemResource


class RunProjectionCorruptResource(ApiModel):
    """One run the attention feed cannot project, named without ending the feed.

    The per-run stream still ends that run with STREAM_FAILED. This frame is
    the feed's own word: the named run is corrupt, and other runs continue.
    """

    event: Literal["RUN_PROJECTION_CORRUPT"] = "RUN_PROJECTION_CORRUPT"
    public_run_reference: str = Field(pattern=PUBLIC_RUN_REFERENCE_PATTERN)
    problem: DurableStateCorruptProblemResource
