from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from atelier2.adapters.dbos.agent_effect_grants import (
    open_pr_capability_for,
)
from atelier2.adapters.dbos.agent_effect_grants import (
    read_pinned_tool_grant as read_agent_pinned_tool_grant,
)
from atelier2.adapters.dbos.effect_store import (
    commit_resolution,
    encode_readback,
    intent_snapshot_from_record,
    load_intent,
)
from atelier2.adapters.dbos.run_transitions import load_graph, load_run
from atelier2.adapters.dbos.schema import (
    agent_receipts_v2,
    effect_intents,
    effect_receipts,
    run_events,
    runs,
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
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevisionHash,
)
from atelier2.contracts.tool_grants_v3 import (
    DeclaredToolGrant,
    ToolGrantCapability,
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
    effect_adapter_binding: EffectAdapterBinding,
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
        Sha256Hash.of(payload).value != record.payload_hash
        or payload != expected_output
    ):
        raise RunEffectConflict("Action predecessor output binding changed")
    binding = EffectBinding(
        logical_effect_key_for_node(
            run_id, revision_hash, action.id, run.current_round_ordinal
        ),
        run_id,
        revision_hash,
        effect_adapter_binding.adapter_revision,
        effect_adapter_binding.destination,
        effect_adapter_binding.operational_identity,
    )
    return EffectIntent(binding, CanonicalRequest(payload))


def prepare_graph_action(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    effect_adapter_binding: EffectAdapterBinding,
) -> EffectIntentSnapshot:
    intent = graph_action_intent(session, run_id, revision_hash, effect_adapter_binding)
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


def legacy_agent_open_pr_runs_without_receipt(engine: sa.Engine) -> tuple[RunId, ...]:
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
                and _agent_node_redeems_platform_effect(connection, current_node)
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
                    or not _agent_node_redeems_platform_effect(connection, node)
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


def _agent_node_redeems_platform_effect(session: Any, node: AgentNodeV3) -> bool:
    return open_pr_capability_for(read_pinned_tool_grant(session, node)) is not None


def _effect_receipt_exists(connection: Any, logical_key: str) -> bool:
    return (
        connection.scalar(
            sa.select(effect_receipts.c.logical_key).where(
                effect_receipts.c.logical_key == logical_key
            )
        )
        is not None
    )


def _effect_shaped_capability_to_open_pr(
    grant: DeclaredToolGrant | None,
) -> ToolGrantCapability | None:
    """The effect-shaped capability this preparation opens a pull request for, or none.

    A missing grant, or one `redeems_as_platform_effect` classifies as
    exec-shaped, prepares no platform effect here -- the exec-shaped grant is
    redeemed inside the attempt's lease instead. An effect-shaped grant that is
    not `open-pr` has no redeemer this preparation performs, so it is refused by
    name here rather than returned as `None` and silently left unprepared: a new
    effect capability then fails loud where its intent would be prepared instead
    of completing a run that opened nothing, exactly as the exec redeemer refuses
    a capability it does not perform. `redeems_as_platform_effect` is the one
    owner of the exec-versus-effect split both redemption paths read.
    """
    return open_pr_capability_for(grant)


def graph_agent_open_pr_intent(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    effect_adapter_binding: EffectAdapterBinding,
) -> EffectIntent | None:
    """The pull-request this agent node's own grant opens, or nothing where none does.

    Which grants open a pull request here is `_effect_shaped_capability_to_open_pr`'s
    decision: a node with no grant or an exec-shaped one prepares nothing, and an
    effect-shaped grant this preparation does not perform is refused by name
    rather than silently left unprepared.

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
    if _effect_shaped_capability_to_open_pr(grant) is None:
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
    )
    return EffectIntent(binding, CanonicalRequest(_agent_output(session, execution_id)))


def prepare_graph_agent_open_pr(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    round_ordinal: int,
    effect_adapter_binding: EffectAdapterBinding,
) -> str | None:
    """Prepare this agent node's own pull-request intent, or nothing where none is.

    The logical key it returns is what the redemption step then loads: the two
    are separate durable steps so a PREPARED intent is committed before any
    adapter is asked, exactly as the effect-shaped redemption port requires.
    """
    intent = graph_agent_open_pr_intent(
        session, run_id, revision_hash, node_id, round_ordinal, effect_adapter_binding
    )
    if intent is None:
        return None
    return prepared_effect_intent(session, intent).intent.binding.logical_key.value


def redeem_agent_open_pr(
    session: Any,
    adapter: EffectAdapter,
    logical_key: str,
    revision_hash: str,
) -> str:
    """Redeem one PREPARED agent pull-request intent through the shared adapter.

    Readback runs before create, so a redemption retried after the pull request
    already exists is recognized rather than opened twice. The receipt reaches
    the same `effect_receipts` row an Action's confirmation writes, through the
    same `commit_resolution`, so what an operator reads back is one effect
    whichever authorization opened it. An `UNKNOWN` readback instead durably
    moves the run and its intent to reconciliation; it never guesses or lets
    the agent complete before a receipt exists.
    """
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
        raise RunEffectConflict("agent open-pr grant has no durable agent receipt")
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
