from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from atelier2.adapters.dbos.agent_effect_grants import (
    agent_node_redeems_platform_effect,
    open_pr_capability_for,
    push_atelier_commit_capability_for,
)
from atelier2.adapters.dbos.agent_effect_grants import (
    read_pinned_tool_grant as read_agent_pinned_tool_grant,
)
from atelier2.adapters.dbos.effect_store import (
    commit_resolution,
    encode_readback,
    fork_fenced_resolution,
    intent_snapshot_from_record,
    load_intent,
    receipt_from_record,
)
from atelier2.adapters.dbos.run_store import load_node_output_payload
from atelier2.adapters.dbos.run_transitions import load_graph, load_run
from atelier2.adapters.dbos.schema import (
    agent_receipts_v2,
    attempt_instants,
    effect_intents,
    effect_receipts,
    published_revisions,
    run_events,
    run_inputs_v3,
    runs,
)
from atelier2.contracts.adapter_operations_v3 import (
    AdapterOperationAccepted,
    AdapterOperationName,
    read_adapter_operation_document,
)
from atelier2.contracts.effect_requests import (
    HeadBranch,
    OpenPullRequest,
    PushAtelierCommit,
    PushAtelierCommitReceipt,
    head_branch_for_queue_item,
)
from atelier2.contracts.effects import (
    CanonicalRequest,
    EffectAdapterBinding,
    EffectBinding,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    logical_effect_key_for_node,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import WorkItemReference
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.tool_grants_v3 import DeclaredToolGrant
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_REVISION,
    WorkItemKind,
    read_work_item_order_document,
)
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.contracts.workflows import ActionNode, AgentNode, AgentNodeV2
from atelier2.contracts.workflows_v3 import (
    ANY_ACTION_NODE_KINDS,
    ActionNodeV3,
    AgentNodeV3,
    AnyWorkflowDocument,
    AnyWorkflowDocumentNode,
    WorkflowGraphV3,
)
from atelier2.ports.agent_tool_effects import (
    AgentToolEffectPending,
    redeem_prepared_tool_effect,
)
from atelier2.ports.effects import EffectAdapter


class EffectIntentIdentityConflict(RuntimeError):
    """A logical effect key was retried with different immutable input."""


class RunEffectConflict(RuntimeError):
    """A V1 run cannot prepare this effect against its durable run binding."""


def graph_action_intent(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    effect_adapter_bindings: EffectAdapterBinding | tuple[EffectAdapterBinding, ...],
    project_id: ProjectId | None = None,
) -> EffectIntent:
    run = load_run(session, run_id)
    graph = load_graph(session, revision_hash)
    action = graph.node(run.current_node_id)
    if (
        run.revision_hash != revision_hash
        or run.state is not RunState.STARTED
        or not isinstance(action, ANY_ACTION_NODE_KINDS)
    ):
        raise RunEffectConflict("effect requires the current STARTED Action")
    predecessor = _action_predecessor(graph, action)
    if not isinstance(predecessor, (AgentNode, AgentNodeV2, AgentNodeV3)):
        raise RunEffectConflict("Action predecessor is not an Agent")
    record = None
    if isinstance(graph, WorkflowGraphV3):
        payload = load_node_output_payload(
            session,
            run_id,
            revision_hash,
            graph,
            predecessor.id,
            run.current_round_ordinal,
        )
    else:
        record = session.execute(
            sa.select(run_events.c.payload, run_events.c.payload_hash).where(
                run_events.c.run_id == run_id.value,
                run_events.c.revision_hash == revision_hash.value,
                run_events.c.node_id == predecessor.id,
                run_events.c.event_kind == RunEventKind.AGENT_COMPLETED.value,
            )
        ).one_or_none()
        if record is None:
            raise RunEffectConflict("Action has no durable Agent output")
        payload = bytes(record.payload)
    expected_output = (
        predecessor.output.encode("utf-8")
        if isinstance(predecessor, AgentNode)
        else payload
    )
    if (
        record is not None and Sha256Hash.of(payload).value != record.payload_hash
    ) or payload != expected_output:
        raise RunEffectConflict("Action predecessor output binding changed")
    operation = (
        _operation_for(session, action.operation)
        if isinstance(action, ActionNodeV3)
        else AdapterOperationAccepted(AdapterOperationName.OPEN_PR)
    )
    effect_adapter_binding = _binding_for(effect_adapter_bindings, operation.operation)
    request = CanonicalRequest(payload)
    if (
        isinstance(action, ActionNodeV3)
        and operation.operation is AdapterOperationName.OPEN_PR
        and project_id is not None
    ):
        if not isinstance(predecessor, AgentNodeV3):
            raise RunEffectConflict("V3 open-pr Action predecessor is not a V3 Agent")
        head_branch = _confirmed_push_branch(
            session,
            run_id,
            revision_hash,
            predecessor,
            run.current_round_ordinal,
            project_id,
        )
        request = CanonicalRequest(
            OpenPullRequest(
                payload.decode("utf-8"),
                head_branch,
            ).canonical_bytes()
        )
    binding = EffectBinding(
        logical_effect_key_for_node(
            run_id, revision_hash, action.id, run.current_round_ordinal
        ),
        run_id,
        revision_hash,
        effect_adapter_binding.adapter_revision,
        effect_adapter_binding.destination,
        effect_adapter_binding.operational_identity,
        operation.operation,
    )
    return EffectIntent(binding, request)


def prepare_graph_action(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    effect_adapter_bindings: EffectAdapterBinding | tuple[EffectAdapterBinding, ...],
    project_id: ProjectId | None = None,
) -> EffectIntentSnapshot:
    intent = graph_action_intent(
        session, run_id, revision_hash, effect_adapter_bindings, project_id
    )
    return prepared_effect_intent(session, intent)


def prepared_effect_intent(session: Any, intent: EffectIntent) -> EffectIntentSnapshot:
    """Record this intent PREPARED, or return the one already durably prepared.

    The logical key is the effect's durable identity: a retry that derives the
    same key from the same immutable node must find the same intent, and a key
    that already belongs to a different intent is a contradiction rather than a
    second preparation. Recording it before any adapter is asked is what lets a
    redemption that never reaches its destination leave a named PREPARED intent
    behind instead of an effect nobody durably asked for.
    """
    existing_record = (
        session.execute(
            sa.select(effect_intents).where(
                effect_intents.c.logical_key == intent.binding.logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if existing_record is not None:
        snapshot = intent_snapshot_from_record(existing_record)
        if snapshot.intent != intent:
            raise EffectIntentIdentityConflict(
                "logical effect key already belongs to another exact intent"
            )
        return snapshot
    session.execute(
        effect_intents.insert().values(
            logical_key=intent.binding.logical_key.value,
            run_id=intent.binding.run_id.value,
            canonical_request=intent.request.payload,
            request_hash=intent.request.request_hash.value,
            workflow_revision_hash=intent.binding.workflow_revision_hash.value,
            adapter_revision=intent.binding.adapter_revision.value,
            destination_identity=intent.binding.destination.value,
            adapter_operational_identity=(
                intent.binding.adapter_operational_identity.value
            ),
            operation_name=intent.binding.operation_name.value,
            state=EffectIntentState.PREPARED.value,
            state_version=0,
            reconciliation_owner_command_id=None,
        )
    )
    return EffectIntentSnapshot(
        intent,
        EffectIntentState.PREPARED,
        EffectIntentStateVersion(0),
    )


def read_pinned_tool_grant(
    session: Any, node: AnyWorkflowDocumentNode
) -> DeclaredToolGrant | None:
    """The grant this node pinned, read from the revision the document pins by hash.

    A V3 `tools` entry pins its published revision by that revision's own hash,
    so reading the registry under it is reading exactly what the run
    configuration froze rather than resolving a second time. The bytes are
    immutable and were already read as a grant when the run was bound; a
    registry that cannot answer for them now contradicts a run that has already
    started. Both redemption paths read the grant here -- the binding to carry
    an exec-shaped one, the effect preparation to open a pull request for an
    effect-shaped one -- so the two never read it two different ways.
    """
    if not isinstance(node, AgentNodeV3):
        return None
    return read_agent_pinned_tool_grant(session, node)


def legacy_agent_effect_runs_without_receipt(engine: sa.Engine) -> tuple[RunId, ...]:
    """Find persisted pre-reconciliation agent-effect checkpoints.

    Before agent effects entered the shared continuation, an agent could advance
    its run and then redeem its grant. Current runs remain on that agent until
    a receipt exists, or wait there for reconciliation. A completed or advanced
    agent execution without the receipt that now authorizes its advance is
    therefore a persisted pre-change shape, not recoverable current work.
    """

    with engine.connect() as connection:
        records = connection.execute(
            sa.select(runs).where(
                runs.c.workflow_format_version == int(WorkflowFormatVersion.V3)
            )
        ).mappings()
        blocking: set[RunId] = set()
        for record in records:
            run_id = RunId(str(record["run_id"]))
            revision_hash = WorkflowRevisionHash(str(record["revision_hash"]))
            graph = load_graph(connection, revision_hash)
            current_node_id = str(record["current_node_id"])
            current_round_ordinal = int(record["current_round_ordinal"])
            current_node = graph.node(current_node_id)
            if (
                isinstance(current_node, AgentNodeV3)
                and agent_node_redeems_platform_effect(connection, current_node)
                and RunState(str(record["state"])) is RunState.COMPLETED
                and not _effect_receipt_exists(
                    connection,
                    logical_effect_key_for_node(
                        run_id,
                        revision_hash,
                        current_node_id,
                        current_round_ordinal,
                    ).value,
                )
            ):
                blocking.add(run_id)
                continue
            for event in connection.execute(
                sa.select(run_events.c.node_id, run_events.c.round_ordinal).where(
                    run_events.c.run_id == run_id.value,
                    run_events.c.event_kind == RunEventKind.AGENT_COMPLETED.value,
                )
            ).mappings():
                node_id = str(event["node_id"])
                round_ordinal = int(event["round_ordinal"])
                node = graph.node(node_id)
                if (
                    not isinstance(node, AgentNodeV3)
                    or not agent_node_redeems_platform_effect(connection, node)
                    or _effect_receipt_exists(
                        connection,
                        logical_effect_key_for_node(
                            run_id, revision_hash, node_id, round_ordinal
                        ).value,
                    )
                    or (
                        current_node_id == node_id
                        and current_round_ordinal == round_ordinal
                    )
                ):
                    continue
                blocking.add(run_id)
        return tuple(sorted(blocking, key=lambda run: run.value))


def _effect_receipt_exists(connection: Any, logical_key: str) -> bool:
    return (
        connection.scalar(
            sa.select(effect_receipts.c.logical_key).where(
                effect_receipts.c.logical_key == logical_key
            )
        )
        is not None
    )


def graph_agent_open_pr_intent(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    effect_adapter_binding: EffectAdapterBinding,
    project_id: ProjectId | None = None,
) -> EffectIntent | None:
    """The pull-request this agent node's own grant opens, or nothing where none does.

    A node with no grant, an exec-shaped grant, or the push grant handled by the
    sibling preparation creates no open-PR intent here. A future effect-shaped
    capability still fails loud in the shared grant classifier.

    The request bytes are the node's own durable receipt output rather than
    trusted from memory, because the same provider bytes the run kept are what
    the pull request must carry. The
    effect binds to the connected repo the adapter names -- not to a
    `project_source` tree-pin -- because a pull request targets a repository,
    not a tree state, so an `open-pr` grant needs no pinned source at all. The
    logical key is derived from this node's own execution identity, which makes
    it deterministic, collision-free, and distinct from the key an Action of the
    same operation would derive from its own node.
    """
    grant = read_pinned_tool_grant(
        session, load_graph(session, revision_hash).node(node_id)
    )
    if open_pr_capability_for(grant) is None:
        return None
    execution_id = NodeExecutionId.for_node(
        run_id, revision_hash, node_id, round_ordinal
    )
    binding = EffectBinding(
        logical_effect_key_for_node(run_id, revision_hash, node_id, round_ordinal),
        run_id,
        revision_hash,
        effect_adapter_binding.adapter_revision,
        effect_adapter_binding.destination,
        effect_adapter_binding.operational_identity,
        AdapterOperationName.OPEN_PR,
    )
    payload = _agent_output(session, execution_id)
    if project_id is None:
        return EffectIntent(binding, CanonicalRequest(payload))
    return EffectIntent(
        binding,
        CanonicalRequest(
            OpenPullRequest(
                payload.decode("utf-8"), _head_branch(session, run_id, project_id)
            ).canonical_bytes()
        ),
    )


def prepare_graph_agent_open_pr(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    effect_adapter_bindings: EffectAdapterBinding | tuple[EffectAdapterBinding, ...],
    project_id: ProjectId | None = None,
) -> str | None:
    """Prepare this agent node's own pull-request intent, or nothing where none is.

    The logical key it returns is what the redemption step then loads: the two
    are separate durable steps so a PREPARED intent is committed before any
    adapter is asked, exactly as the effect-shaped redemption port requires.
    """
    intent = graph_agent_open_pr_intent(
        session,
        run_id,
        revision_hash,
        node_id,
        round_ordinal,
        _binding_for(effect_adapter_bindings, AdapterOperationName.OPEN_PR),
        project_id,
    )
    if intent is None:
        return None
    return prepared_effect_intent(session, intent).intent.binding.logical_key.value


def prepare_graph_agent_push(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    attempt_id: str,
    candidate_tree: str,
    base_commit: str,
    effect_adapter_bindings: EffectAdapterBinding | tuple[EffectAdapterBinding, ...],
    project_id: ProjectId,
) -> str | None:
    """Prepare the exact candidate publication earned by a pinned push grant."""

    node = load_graph(session, revision_hash).node(node_id)
    grant = read_pinned_tool_grant(session, node)
    if push_atelier_commit_capability_for(grant) is None:
        return None
    assert grant is not None and grant.operation is not None
    operation = _operation_for(session, grant.operation)
    if (
        operation.operation is not AdapterOperationName.PUSH_ATELIER_COMMIT
        or operation.author is None
        or operation.committer is None
    ):
        raise RunEffectConflict(
            "pinned push grant does not resolve to a push operation"
        )
    binding_owner = _binding_for(effect_adapter_bindings, operation.operation)
    completed_at = session.scalar(
        sa.select(attempt_instants.c.ended_at).where(
            attempt_instants.c.attempt_id == attempt_id
        )
    )
    if not isinstance(completed_at, str):
        raise RunEffectConflict("successful push attempt has no completion instant")
    request = PushAtelierCommit(
        attempt_id,
        candidate_tree,
        base_commit,
        _head_branch(session, run_id, project_id),
        operation.author,
        operation.committer,
        completed_at,
    )
    binding = EffectBinding(
        logical_effect_key_for_node(run_id, revision_hash, node_id, round_ordinal),
        run_id,
        revision_hash,
        binding_owner.adapter_revision,
        binding_owner.destination,
        binding_owner.operational_identity,
        operation.operation,
    )
    return prepared_effect_intent(
        session, EffectIntent(binding, CanonicalRequest(request.canonical_bytes()))
    ).intent.binding.logical_key.value


def redeem_agent_effect(
    session: Any,
    adapter: EffectAdapter,
    logical_key: str,
    revision_hash: str,
) -> str:
    """Redeem one PREPARED agent effect intent through its selected adapter.

    Readback runs before create, so a redemption retried after the pull request
    already exists is recognized rather than opened twice. The receipt reaches
    the same `effect_receipts` row an Action's confirmation writes, through the
    same `commit_resolution`, so what an operator reads back is one effect
    whichever authorization opened it. An `UNKNOWN` readback instead durably
    moves the run and its intent to reconciliation; it never guesses or lets
    the agent complete before a receipt exists.
    """
    fenced = fork_fenced_resolution(session, logical_key, revision_hash)
    if fenced is not None:
        return commit_resolution(session, logical_key, revision_hash, fenced).value
    intent = load_intent(session, logical_key, revision_hash)
    redemption = redeem_prepared_tool_effect(intent, adapter)
    if isinstance(redemption, AgentToolEffectPending):
        return commit_resolution(
            session,
            logical_key,
            revision_hash,
            encode_readback(redemption.unknown),
        ).value
    return commit_resolution(
        session, logical_key, revision_hash, encode_readback(redemption.receipt)
    ).value


def _agent_output(session: Any, execution_id: NodeExecutionId) -> bytes:
    record = session.execute(
        sa.select(
            agent_receipts_v2.c.output_bytes, agent_receipts_v2.c.output_hash
        ).where(
            agent_receipts_v2.c.node_execution_id == execution_id.value,
        )
    ).one_or_none()
    if record is None:
        raise RunEffectConflict("agent effect grant has no durable agent receipt")
    payload = bytes(record.output_bytes)
    if Sha256Hash.of(payload).value != record.output_hash:
        raise RunEffectConflict("agent output binding changed")
    return payload


def _action_predecessor(
    graph: AnyWorkflowDocument, action: ActionNode | ActionNodeV3
) -> object:
    if isinstance(graph, WorkflowGraphV3):
        if not isinstance(action, ActionNodeV3) or len(action.depends_on) != 1:
            raise RunEffectConflict("Action predecessor is not an Agent")
        predecessor = graph.node(action.depends_on[0])
        if not isinstance(predecessor, AgentNodeV3):
            raise RunEffectConflict("Action predecessor is not an Agent")
        return predecessor
    return graph.predecessor(action.id)


def _confirmed_push_branch(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    predecessor: AgentNodeV3,
    round_ordinal: int,
    project_id: ProjectId,
) -> HeadBranch:
    logical_key = logical_effect_key_for_node(
        run_id, revision_hash, predecessor.id, round_ordinal
    )
    record = (
        session.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key == logical_key.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RunEffectConflict(
            "project open-pr Action requires its predecessor's confirmed push receipt"
        )
    try:
        receipt = receipt_from_record(record)
        request = PushAtelierCommit.from_canonical_bytes(receipt.intent.request.payload)
        result = PushAtelierCommitReceipt.from_result_bytes(receipt.result.payload)
        result_branch = HeadBranch(result.branch)
    except (TypeError, ValueError) as error:
        raise RunEffectConflict("confirmed push receipt is corrupt") from error
    expected_commit = request.expected_commit_oid(
        receipt.intent.request.request_hash.value
    )
    if (
        receipt.intent.binding.operation_name
        is not AdapterOperationName.PUSH_ATELIER_COMMIT
        or receipt.intent.binding.run_id != run_id
        or receipt.intent.binding.workflow_revision_hash != revision_hash
        or result.remote_identity
        != receipt.intent.binding.adapter_operational_identity.value
        or result.commit_oid != receipt.effect_id.value
        or result.commit_oid != expected_commit
        or result.full_ref != request.head_branch.full_ref
        or result.parent != request.base_commit
        or result.candidate_tree != request.candidate_tree
        or result_branch != request.head_branch
        or result.author != request.author
        or result.committer != request.committer
        or result_branch != _head_branch(session, run_id, project_id)
    ):
        raise RunEffectConflict(
            "confirmed push receipt disagrees with the open-pr head"
        )
    return result_branch


def _operation_for(session: Any, reference: Any) -> AdapterOperationAccepted:
    document = session.scalar(
        sa.select(published_revisions.c.document).where(
            published_revisions.c.kind == RevisionKind.ADAPTER_OPERATION.value,
            published_revisions.c.revision_hash == reference.revision,
        )
    )
    if document is None:
        raise RunEffectConflict("pinned adapter operation left the registry")
    verdict = read_adapter_operation_document(bytes(document))
    if not isinstance(verdict, AdapterOperationAccepted):
        raise RunEffectConflict("pinned adapter operation is corrupt")
    return verdict


def _binding_for(
    bindings: EffectAdapterBinding | tuple[EffectAdapterBinding, ...],
    operation: AdapterOperationName,
) -> EffectAdapterBinding:
    candidates = (bindings,) if isinstance(bindings, EffectAdapterBinding) else bindings
    matching = tuple(
        binding for binding in candidates if binding.operation_name is operation
    )
    if len(matching) != 1:
        raise RunEffectConflict(
            f"operation {operation.value!r} does not have exactly one adapter"
        )
    return matching[0]


def _head_branch(session: Any, run_id: RunId, project_id: ProjectId):
    rows = session.execute(
        sa.select(
            run_inputs_v3.c.schema_revision_hash,
            run_inputs_v3.c.value,
            run_inputs_v3.c.value_hash,
        ).where(run_inputs_v3.c.run_id == run_id.value)
    ).all()
    orders = []
    for schema_revision, value, value_hash in rows:
        raw = bytes(value)
        if Sha256Hash.of(raw).value != str(value_hash):
            raise RunEffectConflict("run input bytes differ from their durable hash")
        if str(schema_revision) != WORK_ITEM_ORDER_SCHEMA_REVISION.value:
            continue
        order = read_work_item_order_document(raw)
        if order is None or order.kind is not WorkItemKind.ISSUE:
            raise RunEffectConflict("push requires one valid issue work-item order")
        orders.append(order)
    if len(orders) != 1:
        raise RunEffectConflict("push requires exactly one issue work-item order")
    return head_branch_for_queue_item(
        WorkItemReference(project_id, orders[0].reference).item_id
    )
