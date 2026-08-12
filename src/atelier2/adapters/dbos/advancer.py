from __future__ import annotations

import hashlib
from typing import Any

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.run_store import load_graph, load_run
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents, run_events
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow import EFFECT_WORKFLOW_NAME, QUEUE_NAME
from atelier2.contracts.effects import (
    CanonicalRequest,
    EffectAdapterBinding,
    EffectBinding,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
    LogicalEffectKey,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    logical_effect_key_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflows import ActionNode, AgentNode, AgentNodeV2

EFFECT_WORKFLOW_ID_PREFIX = "atelier2-effect-"


class EffectIntentIdentityConflict(RuntimeError):
    """A logical effect key was retried with different immutable input."""


class RunEffectConflict(RuntimeError):
    """A V1 run cannot prepare this effect against its durable run binding."""


def effect_workflow_id_for(logical_key: LogicalEffectKey) -> str:
    return (
        EFFECT_WORKFLOW_ID_PREFIX
        + hashlib.sha256(logical_key.value.encode()).hexdigest()
    )


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
        or not isinstance(action, ActionNode)
    ):
        raise RunEffectConflict("effect requires the current STARTED Action")
    predecessor = graph.predecessor(action.id)
    if not isinstance(predecessor, (AgentNode, AgentNodeV2)):
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
    snapshot, _created = _prepare_intent(
        session,
        graph_action_intent(session, run_id, revision_hash, effect_adapter_binding),
        effect_adapter_binding,
    )
    return snapshot


def _prepare_intent(
    session: Any,
    intent: EffectIntent,
    effect_adapter_binding: EffectAdapterBinding,
) -> tuple[EffectIntentSnapshot, bool]:
    if intent.binding.adapter_binding != effect_adapter_binding:
        raise EffectIntentIdentityConflict(
            "intent does not match the runtime effect adapter binding"
        )
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
        return snapshot, False
    expected_intent = graph_action_intent(
        session,
        intent.binding.run_id,
        intent.binding.workflow_revision_hash,
        effect_adapter_binding,
    )
    if intent != expected_intent:
        raise RunEffectConflict("effect differs from the graph-derived Action intent")
    current = load_run(session, intent.binding.run_id)
    if (
        current.revision_hash != intent.binding.workflow_revision_hash
        or current.state is not RunState.STARTED
    ):
        raise RunEffectConflict("effect requires its exact revision-bound STARTED run")
    if session.scalar(
        sa.select(sa.func.count())
        .select_from(effect_intents)
        .where(effect_intents.c.run_id == intent.binding.run_id.value)
    ):
        raise RunEffectConflict("V1 permits exactly one effect per run")
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
    return (
        EffectIntentSnapshot(
            intent,
            EffectIntentState.PREPARED,
            EffectIntentStateVersion(0),
        ),
        True,
    )


class DbosDurableRunAdvancer:
    def __init__(
        self,
        engine: Engine,
        settings: DbosRuntimeSettings,
        effect_adapter_binding: EffectAdapterBinding,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._effect_adapter_binding = effect_adapter_binding

    def advance(self, intent: EffectIntent) -> EffectIntentSnapshot:
        client = DBOSClient(
            system_database_engine=self._engine, use_listen_notify=False
        )
        try:
            with canonical_write_transaction(self._engine) as connection:
                snapshot, created = _prepare_intent(
                    connection, intent, self._effect_adapter_binding
                )
                if not created:
                    return snapshot
                workflow_id = effect_workflow_id_for(intent.binding.logical_key)
                options: EnqueueOptions = {
                    "workflow_name": EFFECT_WORKFLOW_NAME,
                    "queue_name": QUEUE_NAME,
                    "workflow_id": workflow_id,
                    "app_version": self._settings.application_version,
                }
                client.enqueue_in_transaction(
                    connection,
                    options,
                    intent.binding.logical_key.value,
                    intent.binding.workflow_revision_hash.value,
                )
                return snapshot
        finally:
            client.destroy()
