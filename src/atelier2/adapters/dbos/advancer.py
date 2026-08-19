from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.run_transitions import load_graph, load_run
from atelier2.adapters.dbos.schema import effect_intents, run_events
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
    logical_effect_key_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflows import ActionNode, AgentNode, AgentNodeV2
from atelier2.contracts.workflows_v3 import (
    ANY_ACTION_NODE_KINDS,
    ActionNodeV3,
    AgentNodeV3,
    AnyWorkflowDocument,
    WorkflowGraphV3,
)


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
    execution_id = NodeExecutionId.for_node(run_id, revision_hash, action.id)
    binding = EffectBinding(
        logical_effect_key_for(execution_id),
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
