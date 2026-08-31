"""Projection of workflow documents, determinations, and receipts onto the wire."""

from __future__ import annotations

from atelier2.api.references import (
    MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS,
    encode_canonical_base64,
)
from atelier2.api.wire.resources import (
    EffectReceiptResource,
    VersionedWorkflowRevisionPageResource,
    WaitAnswerSchemaResourceV3,
    WorkflowDeclaredOrderResourceV3,
    WorkflowDeclaredSchemaResourceV3,
    WorkflowGraphResourceV3,
    WorkflowLoopResourceV3,
    WorkflowLoopVerdictResourceV3,
    WorkflowNodePreviewResourceV3,
    WorkflowRevisionDetailResource,
    WorkflowRevisionSummaryResourceV2,
)
from atelier2.application.read_workflow_revisions import (
    DescribedWorkflowRevision,
    WaitAnswerClassification,
    WorkflowRevisionRead,
    WorkflowRevisionsDescribed,
)
from atelier2.contracts.effects import (
    EffectReceipt,
)
from atelier2.contracts.workflows_v3 import (
    AgentNodeV3,
    LoopDeclaration,
    WaitNodeV3,
    WorkflowGraphV3,
    WorkflowNodeV3,
)


class UnservedWorkflowFormat(ValueError):
    """Durable state names a workflow format this API has no wire shape for.

    A `ValueError` because that is what the stream and the command routes already
    treat as corrupt durable state; naming it keeps the reason readable where it
    is raised and where it is caught.
    """


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


def _wait_answer_schema(
    node: WaitNodeV3, classification: WaitAnswerClassification | None
) -> WaitAnswerSchemaResourceV3:
    """One waiting node's answer schema: its hull, plus a classification if one arrived.

    `orders` already echoes its own schema hull unresolved, and the hull half
    of this excerpt does the same: the wait node's own `ref`/`revision` pin,
    published exactly as the document wrote it. The `kind`/`values` half is
    not this projection's own read -- reading a schema's bytes to say
    `boolean` or `enum` needs a `PublishedRevisionResolver`, and the API layer
    may hold a port but may not match the record kind a port answers with
    (`api-port-record-problems`, `scripts/check_architecture.py`) -- so that
    read happens once, in the application layer that already resolves
    references at run-bind time (`atelier2.application.read_workflow_revisions`
    beside `resolve_references`), and arrives here as `classification`. A
    caller with nothing resolved (no resolver at hand, or this node simply
    absent from what was resolved) passes `None`, and this excerpt answers the
    honest little a hull alone supports: `free`, never a guess.
    """
    output = node.outputs[0]
    hull = WorkflowDeclaredSchemaResourceV3(
        ref=output.schema_reference.ref, revision=output.schema_reference.revision
    )
    if classification is None:
        return WaitAnswerSchemaResourceV3(
            node_id=node.id, schema=hull, kind="free", values=None
        )
    return WaitAnswerSchemaResourceV3(
        node_id=node.id,
        schema=hull,
        kind=classification.kind,
        values=classification.values,
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
    graph: WorkflowGraphV3,
    not_executable_reason: str | None,
    wait_answer_classifications: tuple[WaitAnswerClassification, ...] = (),
) -> WorkflowGraphResourceV3:
    """The graph on the wire; executability is the application's verdict, carried.

    The API derives and never decides which documents run: the reason arrives
    from the one rule the start path applies, so this projection cannot drift
    from the starter the first time either moves.
    """
    classified_by_node_id = {
        classification.node_id: classification
        for classification in wait_answer_classifications
    }
    return WorkflowGraphResourceV3(
        workflow_format_version=3,
        executable=not_executable_reason is None,
        not_executable_reason=not_executable_reason,
        node_count=len(graph.nodes),
        agent_roles=tuple(
            sorted({node.role for node in graph.nodes if isinstance(node, AgentNodeV3)})
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
        wait_answer_schemas=tuple(
            _wait_answer_schema(node, classified_by_node_id.get(node.id))
            for node in graph.nodes
            if isinstance(node, WaitNodeV3)
        ),
        node_previews=tuple(_node_preview(node) for node in graph.nodes),
        loops=tuple(_loop_resource(loop) for loop in graph.loops),
        name=graph.name,
        description=graph.description,
    )


def workflow_revision_summary_resource(
    described: DescribedWorkflowRevision,
) -> WorkflowRevisionSummaryResourceV2:
    """What a listing may say about one revision, and no more than that."""

    graph = described.projection.graph
    return WorkflowRevisionSummaryResourceV2(
        workflow_revision_hash=described.projection.revision.revision_hash.value,
        workflow_format_version=graph.format_version,
        executable=described.not_executable_reason is None,
        not_executable_reason=described.not_executable_reason,
        name=graph.name,
        description=graph.description,
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
    read: WorkflowRevisionRead,
) -> WorkflowRevisionDetailResource:
    projection = read.projection
    return WorkflowRevisionDetailResource(
        workflow_revision_hash=projection.revision.revision_hash.value,
        document_base64=encode_canonical_base64(projection.revision.document),
        graph=graph_resource(
            projection.graph,
            read.not_executable_reason,
            read.wait_answer_classifications,
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
