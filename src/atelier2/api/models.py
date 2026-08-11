from __future__ import annotations

from typing import Annotated, Literal

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
from atelier2.contracts.effects import (
    EffectReceipt,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    ReconcileCommand,
)
from atelier2.contracts.executions import RunEventKind, is_canonical_integer_bytes
from atelier2.contracts.runs import RunState
from atelier2.contracts.workflows import (
    ActionNode,
    AgentNode,
    SubworkflowNode,
    WaitNode,
    WorkflowGraph,
    WorkflowNode,
)
from atelier2.ports.run_events import PersistedRunEvent
from atelier2.ports.run_queries import RunProjection
from atelier2.ports.workflow_revisions import WorkflowRevisionProjection


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class HealthResource(ApiModel):
    status: Literal["serving"]
    source_commit: str
    source_tree: str


class AgentNodeResource(ApiModel):
    type: Literal["agent"]
    node_id: str = Field(min_length=1)
    job: str = Field(min_length=1)
    output: str = Field(min_length=1)
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


class WorkflowGraphResource(ApiModel):
    format_version: Literal[1]
    start_node_id: str = Field(min_length=1)
    nodes: tuple[NodeResource, ...]


class WorkflowRevisionSummaryResource(ApiModel):
    revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


class WorkflowRevisionDetailResource(ApiModel):
    revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)
    document_base64: str
    graph: WorkflowGraphResource


class WorkflowRevisionPageResource(ApiModel):
    items: tuple[WorkflowRevisionSummaryResource, ...]
    next_after_revision_hash: str | None = Field(pattern=REVISION_HASH_PATTERN)


class OperatorFoundDeterminationResource(ApiModel):
    type: Literal["operator_found"]
    effect_id: str = Field(min_length=1)
    result_base64: str = Field(min_length=1)


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


class RunPageResource(ApiModel):
    items: tuple[RunResource, ...]
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


class StartRunRequestResource(ApiModel):
    run_id: str = Field(min_length=1)
    workflow_revision_hash: str = Field(pattern=REVISION_HASH_PATTERN)


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


class ProblemResource(ApiModel):
    type: str
    title: str
    status: int
    detail: str


def node_resource(node: WorkflowNode) -> NodeResource:
    if isinstance(node, AgentNode):
        return AgentNodeResource(
            type="agent",
            node_id=node.id,
            job=node.job,
            output=node.output,
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


def graph_resource(graph: WorkflowGraph) -> WorkflowGraphResource:
    return WorkflowGraphResource(
        format_version=graph.format_version,
        start_node_id=graph.start,
        nodes=tuple(
            node_resource(node)
            for node in sorted(graph.nodes, key=lambda item: item.id.encode("utf-8"))
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


def run_resource(projection: RunProjection) -> RunResource:
    run = projection.run
    node = projection.graph.node(run.current_node_id)
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
        current_node=node_resource(node),
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


def run_event_resource(projection: PersistedRunEvent) -> RunEventResource:
    event = projection.event
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
