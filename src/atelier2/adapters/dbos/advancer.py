from __future__ import annotations

import hashlib

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.runtime import (
    DbosRuntimeSettings,
    canonical_write_transaction,
)
from atelier2.adapters.dbos.schema import effect_intents, runs
from atelier2.adapters.dbos.workflow import EFFECT_WORKFLOW_NAME, QUEUE_NAME
from atelier2.contracts.effects import (
    EffectAdapterBinding,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
    LogicalEffectKey,
)
from atelier2.contracts.runs import RunState

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
                if intent.binding.adapter_binding != self._effect_adapter_binding:
                    raise EffectIntentIdentityConflict(
                        "intent does not match the runtime effect adapter binding"
                    )
                existing_record = (
                    connection.execute(
                        sa.select(effect_intents).where(
                            effect_intents.c.logical_key
                            == intent.binding.logical_key.value
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

                run_record = connection.execute(
                    sa.select(runs.c.revision_hash, runs.c.state).where(
                        runs.c.run_id == intent.binding.run_id.value
                    )
                ).one_or_none()
                if run_record is None or tuple(run_record) != (
                    intent.binding.workflow_revision_hash.value,
                    RunState.STARTED.value,
                ):
                    raise RunEffectConflict(
                        "effect requires its exact revision-bound STARTED run"
                    )
                if connection.scalar(
                    sa.select(sa.func.count())
                    .select_from(effect_intents)
                    .where(effect_intents.c.run_id == intent.binding.run_id.value)
                ):
                    raise RunEffectConflict("V1 permits exactly one effect per run")

                connection.execute(
                    effect_intents.insert().values(
                        logical_key=intent.binding.logical_key.value,
                        run_id=intent.binding.run_id.value,
                        canonical_request=intent.request.payload,
                        request_hash=intent.request.request_hash.value,
                        workflow_revision_hash=(
                            intent.binding.workflow_revision_hash.value
                        ),
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
                return EffectIntentSnapshot(
                    intent,
                    EffectIntentState.PREPARED,
                    EffectIntentStateVersion(0),
                )
        finally:
            client.destroy()
