"""Projection of workflow documents, determinations, and receipts onto the wire."""

from __future__ import annotations

from typing import cast

from atelier2.api.references import (
    MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS,
    encode_canonical_base64,
)
from atelier2.api.wire.resources import (
    ActionNodeResource,
    AgentNodeResource,
    AgentNodeResourceV2,
    EffectReceiptResource,
    NodeResource,
    NodeResourceV2,
    OperatorAuthoritativeAbsenceDeterminationResource,
    OperatorFoundDeterminationResource,
    ReconciliationCommandResource,
    ReconciliationDeterminationResource,
    SubworkflowNodeResource,
    VersionedWorkflowRevisionPageResource,
    WaitNodeResource,
    WorkflowDeclaredOrderResourceV3,
    WorkflowDeclaredSchemaResourceV3,
    WorkflowGraphResource,
    WorkflowGraphResourceV2,
    WorkflowGraphResourceV3,
    WorkflowLoopResourceV3,
    WorkflowLoopVerdictResourceV3,
    WorkflowNodePreviewResourceV3,
    WorkflowRevisionDetailResource,
    WorkflowRevisionSummaryResourceV2,
)
from atelier2.application.read_workflow_revisions import WorkflowRevisionsDescribed
from atelier2.contracts.effects import (
    EffectReceipt,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    ReconcileCommand,
)
from atelier2.contracts.workflow_projections import (
    WorkflowRevisionProjection,
)
from atelier2.contracts.workflows import (
    ActionNode,
    AgentNode,
    AgentNodeV2,
    SubworkflowNode,
    WaitNode,
    WorkflowGraph,
    WorkflowNode,
    WorkflowNodeV2,
)
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    AnyWorkflowDocument,
    LoopDeclaration,
    WorkflowGraphV3,
    WorkflowNodeV3,
    what_a_v3_document_still_waits_for,
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


def _executability_of(graph: AnyWorkflowDocument) -> tuple[bool, str | None]:
    """Whether this build interprets every form the document declares, and why not.

    One owner answers it: the same rule the executable parse applies before a run
    is admitted. The API derives, and never decides for itself which formats run --
    a second list here would drift from the starter's the first time either moved,
    and the reader would have no way to tell which one was lying.

    A V1 or V2 document has no waiting list at all: the rule exists because V3
    declares forms nothing binds yet, and those two formats are executed whole.
    """
    if not isinstance(graph, WorkflowGraphV3):
        return True, None
    waiting = what_a_v3_document_still_waits_for(graph)
    return waiting is None, waiting


def _node_preview(node: WorkflowNodeV3) -> WorkflowNodePreviewResourceV3:
    """The excerpt one published node may show, never the node itself.

    Only an agent declares a role and an instruction. A wait prompt is a
    different authored field and is not projected as one: empty here is the
    node's own answer, not a refusal and not a stand-in.
    """
    if isinstance(node, AgentNodeV3):
        return WorkflowNodePreviewResourceV3(
            id=node.id,
            kind="agent",
            role=node.role,
            instruction_start=node.instruction[
                :MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS
            ],
            depends_on=node.depends_on,
        )
    return WorkflowNodePreviewResourceV3(
        id=node.id,
        kind=node.type,
        role=None,
        instruction_start=None,
        depends_on=node.depends_on,
    )


def _loop_resource(loop: LoopDeclaration) -> WorkflowLoopResourceV3:
    return WorkflowLoopResourceV3(
        id=loop.id,
        member_node_ids=loop.body,
        maximum_rounds=loop.maximum_rounds,
        repeat_while=(
            None
            if loop.repeat_while is None
            else WorkflowLoopVerdictResourceV3(
                node=loop.repeat_while.node,
                verdict=loop.repeat_while.verdict.value,
            )
        ),
    )


def graph_resource(
    graph: AnyWorkflowDocument,
) -> WorkflowGraphResource | WorkflowGraphResourceV2 | WorkflowGraphResourceV3:
    if isinstance(graph, WorkflowGraphV3):
        executable, not_executable_reason = _executability_of(graph)
        return WorkflowGraphResourceV3(
            workflow_format_version=3,
            executable=executable,
            not_executable_reason=not_executable_reason,
            node_count=len(graph.nodes),
            agent_roles=tuple(
                sorted(
                    {node.role for node in graph.nodes if isinstance(node, AgentNodeV3)}
                )
            ),
            orders=tuple(
                WorkflowDeclaredOrderResourceV3(
                    name=entry.name,
                    schema=WorkflowDeclaredSchemaResourceV3(
                        ref=entry.schema_reference.ref,
                        revision=entry.schema_reference.revision,
                    ),
                )
                for entry in graph.graph_inputs
            ),
            node_previews=tuple(_node_preview(node) for node in graph.nodes),
            loops=tuple(_loop_resource(loop) for loop in graph.loops),
            name=graph.name,
            description=graph.description,
        )
    ordered = sorted(graph.nodes, key=lambda item: item.id.encode("utf-8"))
    if isinstance(graph, WorkflowGraph):
        return WorkflowGraphResource(
            workflow_format_version=1,
            start_node_id=graph.start,
            nodes=cast(
                tuple[NodeResource, ...], tuple(node_resource(item) for item in ordered)
            ),
        )
    return WorkflowGraphResourceV2(
        workflow_format_version=2,
        start_node_id=graph.start,
        nodes=cast(
            tuple[NodeResourceV2, ...], tuple(node_resource(item) for item in ordered)
        ),
    )


def workflow_revision_summary_resource(
    projection: WorkflowRevisionProjection,
) -> WorkflowRevisionSummaryResourceV2:
    """What a listing may say about one revision, and no more than that.

    Only a V3 document declares a name or a description, so for every other
    format both are absent here. That absence is the record's own answer, not a
    gap this projection fills in (ADR 0007 decision 4).
    """

    graph = projection.graph
    described = isinstance(graph, WorkflowGraphV3)
    executable, not_executable_reason = _executability_of(graph)
    return WorkflowRevisionSummaryResourceV2(
        workflow_revision_hash=projection.revision.revision_hash.value,
        workflow_format_version=graph.format_version,
        executable=executable,
        not_executable_reason=not_executable_reason,
        name=graph.name if described else None,
        description=graph.description if described else None,
    )


def workflow_revision_page_resource(
    page: WorkflowRevisionsDescribed,
) -> VersionedWorkflowRevisionPageResource:
    return VersionedWorkflowRevisionPageResource(
        items=tuple(workflow_revision_summary_resource(item) for item in page.items),
        next_after_revision_hash=(
            None if page.next_after is None else page.next_after.value
        ),
    )


def workflow_revision_detail_resource(
    projection: WorkflowRevisionProjection,
) -> WorkflowRevisionDetailResource:
    return WorkflowRevisionDetailResource(
        workflow_revision_hash=projection.revision.revision_hash.value,
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
