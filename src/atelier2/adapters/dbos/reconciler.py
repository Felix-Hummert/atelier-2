from __future__ import annotations

import hashlib

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.effect_store import (
    command_snapshot_from_record,
    intent_snapshot_from_record,
)
from atelier2.adapters.dbos.runtime import (
    DbosRuntimeSettings,
    canonical_write_transaction,
)
from atelier2.adapters.dbos.schema import effect_intents, reconcile_commands
from atelier2.adapters.dbos.workflow import QUEUE_NAME, RECONCILE_WORKFLOW_NAME
from atelier2.contracts.effects import (
    EffectIntentState,
    OperatorFoundEffect,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)

RECONCILE_WORKFLOW_ID_PREFIX = "atelier2-reconcile-"


class ReconcileCommandIdentityConflict(RuntimeError):
    """A command identifier was retried with different immutable input."""


def reconcile_workflow_id_for(command_id: ReconcileCommandId) -> str:
    return (
        RECONCILE_WORKFLOW_ID_PREFIX
        + hashlib.sha256(command_id.value.encode()).hexdigest()
    )


class DbosEffectReconcileCommander:
    def __init__(self, engine: Engine, settings: DbosRuntimeSettings) -> None:
        self._engine = engine
        self._settings = settings

    def submit(self, command: ReconcileCommand) -> ReconcileCommandSnapshot:
        client = DBOSClient(
            system_database_engine=self._engine, use_listen_notify=False
        )
        try:
            with canonical_write_transaction(self._engine) as connection:
                existing_record = (
                    connection.execute(
                        sa.select(reconcile_commands).where(
                            reconcile_commands.c.command_id == command.command_id.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing_record is not None:
                    intent_record = (
                        connection.execute(
                            sa.select(effect_intents).where(
                                effect_intents.c.logical_key
                                == existing_record["logical_key"]
                            )
                        )
                        .mappings()
                        .one()
                    )
                    snapshot = command_snapshot_from_record(
                        existing_record,
                        intent_snapshot_from_record(intent_record).intent,
                    )
                    if snapshot.command != command:
                        raise ReconcileCommandIdentityConflict(
                            "command identifier already belongs to another decision"
                        )
                    return snapshot

                intent_record = (
                    connection.execute(
                        sa.select(effect_intents).where(
                            effect_intents.c.logical_key
                            == command.intent_reference.binding.logical_key.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if intent_record is None:
                    raise ReconcileCommandIdentityConflict(
                        "command names no prepared durable intent"
                    )
                intent_snapshot = intent_snapshot_from_record(intent_record)
                if intent_snapshot.intent.reference != command.intent_reference:
                    raise ReconcileCommandIdentityConflict(
                        "command does not name the exact durable intent"
                    )

                accepted = (
                    intent_snapshot.state is EffectIntentState.WAITING_RECONCILIATION
                    and intent_snapshot.state_version
                    == command.expected_intent_state_version
                )
                state = (
                    ReconcileCommandState.PENDING
                    if accepted
                    else ReconcileCommandState.REJECTED_CONFLICT
                )
                if accepted:
                    intent_snapshot.intent.resolve_reconciliation(
                        command, intent_snapshot.state_version
                    )
                self._insert_command(connection, command, state)
                if not accepted:
                    return ReconcileCommandSnapshot(command, state)

                updated = connection.execute(
                    effect_intents.update()
                    .where(
                        effect_intents.c.logical_key
                        == command.intent_reference.binding.logical_key.value,
                        effect_intents.c.state
                        == EffectIntentState.WAITING_RECONCILIATION.value,
                        effect_intents.c.state_version
                        == command.expected_intent_state_version.value,
                    )
                    .values(
                        state=EffectIntentState.RECONCILING.value,
                        state_version=command.expected_intent_state_version.value + 1,
                        reconciliation_owner_command_id=command.command_id.value,
                    )
                )
                if updated.rowcount != 1:
                    raise RuntimeError(
                        "serialized reconciliation CAS did not own one intent"
                    )
                workflow_id = reconcile_workflow_id_for(command.command_id)
                options: EnqueueOptions = {
                    "workflow_name": RECONCILE_WORKFLOW_NAME,
                    "queue_name": QUEUE_NAME,
                    "workflow_id": workflow_id,
                    "app_version": self._settings.application_version,
                }
                client.enqueue_in_transaction(
                    connection,
                    options,
                    command.command_id.value,
                    command.intent_reference.binding.workflow_revision_hash.value,
                )
                return ReconcileCommandSnapshot(command, state)
        finally:
            client.destroy()

    @staticmethod
    def _insert_command(
        connection: sa.Connection,
        command: ReconcileCommand,
        state: ReconcileCommandState,
    ) -> None:
        determination = command.determination
        found = isinstance(determination, OperatorFoundEffect)
        connection.execute(
            reconcile_commands.insert().values(
                command_id=command.command_id.value,
                logical_key=command.intent_reference.binding.logical_key.value,
                expected_intent_version=command.expected_intent_state_version.value,
                determination="FOUND" if found else "AUTHORITATIVE_NOT_FOUND",
                actor=command.actor.value,
                evidence=command.evidence,
                found_effect_id=determination.effect_id.value if found else None,
                found_result=determination.result.payload if found else None,
                found_result_hash=(
                    determination.result.payload_hash.value if found else None
                ),
                state=state.value,
            )
        )
