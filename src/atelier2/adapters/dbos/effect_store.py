from __future__ import annotations

from collections.abc import Mapping
from typing import Any, assert_never

import sqlalchemy as sa

from atelier2.adapters.dbos.run_transitions import (
    commit_reconciliation_required,
    commit_reconciliation_resolved,
    load_run,
)
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    reconcile_commands,
    runs,
)
from atelier2.contracts.effects import (
    EFFECT_INTENT_VERSION_CONFIRMED_INITIAL,
    EFFECT_INTENT_VERSION_CONFIRMED_RECONCILED,
    EFFECT_INTENT_VERSION_INITIAL,
    EFFECT_INTENT_VERSION_RECONCILING,
    EFFECT_INTENT_VERSION_WAITING,
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    ConfirmationSource,
    EffectAbsence,
    EffectBinding,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectReceipt,
    EffectRequestHash,
    EffectResult,
    EffectUnknownOutcome,
    LogicalEffectKey,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.ports.effects import EffectAdapter

type EncodedEffectResolution = dict[str, str | None]


class DurableEffectConflict(RuntimeError):
    """Durable rows do not form the exact effect transition expected."""


def intent_from_record(record: Mapping[Any, Any]) -> EffectIntent:
    request = CanonicalRequest.from_durable_record(
        bytes(record["canonical_request"]),
        EffectRequestHash(str(record["request_hash"])),
    )
    return EffectIntent(
        EffectBinding(
            LogicalEffectKey(str(record["logical_key"])),
            RunId(str(record["run_id"])),
            WorkflowRevisionHash(str(record["workflow_revision_hash"])),
            AdapterRevision(str(record["adapter_revision"])),
            EffectDestination(str(record["destination_identity"])),
            AdapterOperationalIdentity(str(record["adapter_operational_identity"])),
        ),
        request,
    )


def intent_snapshot_from_record(record: Mapping[Any, Any]) -> EffectIntentSnapshot:
    return EffectIntentSnapshot(
        intent_from_record(record),
        EffectIntentState(str(record["state"])),
        EffectIntentStateVersion(int(record["state_version"])),
    )


def receipt_from_record(record: Mapping[Any, Any]) -> EffectReceipt:
    command_id = record["reconcile_command_id"]
    return EffectReceipt(
        intent=intent_from_record(record),
        effect_id=EffectId(str(record["effect_id"])),
        result=EffectResult.from_durable_record(
            bytes(record["result"]), Sha256Hash(str(record["result_hash"]))
        ),
        confirmation_source=ConfirmationSource(str(record["confirmation_source"])),
        reconcile_command_id=(
            None if command_id is None else ReconcileCommandId(str(command_id))
        ),
    )


def command_from_record(
    record: Mapping[Any, Any], intent: EffectIntent
) -> ReconcileCommand:
    found_effect_id = record["found_effect_id"]
    if found_effect_id is None:
        determination = OperatorAuthoritativeAbsence()
    else:
        determination = OperatorFoundEffect(
            EffectId(str(found_effect_id)),
            EffectResult.from_durable_record(
                bytes(record["found_result"]),
                Sha256Hash(str(record["found_result_hash"])),
            ),
        )
    return ReconcileCommand(
        ReconcileCommandId(str(record["command_id"])),
        intent.reference,
        EffectIntentStateVersion(int(record["expected_intent_version"])),
        ReconcileActor(str(record["actor"])),
        str(record["evidence"]),
        determination,
    )


def command_snapshot_from_record(
    record: Mapping[Any, Any], intent: EffectIntent
) -> ReconcileCommandSnapshot:
    return ReconcileCommandSnapshot(
        command_from_record(record, intent),
        ReconcileCommandState(str(record["state"])),
    )


def load_intent(session: Any, logical_key: str, revision_hash: str) -> EffectIntent:
    intent_record = (
        session.execute(
            sa.select(effect_intents).where(effect_intents.c.logical_key == logical_key)
        )
        .mappings()
        .one_or_none()
    )
    if intent_record is None:
        raise DurableEffectConflict("durable effect workflow has no prepared intent")
    snapshot = intent_snapshot_from_record(intent_record)
    if snapshot.intent.binding.workflow_revision_hash.value != revision_hash:
        raise DurableEffectConflict("effect workflow revision binding changed")
    run_revision = session.scalar(
        sa.select(runs.c.revision_hash).where(
            runs.c.run_id == snapshot.intent.binding.run_id.value
        )
    )
    if run_revision != revision_hash:
        raise DurableEffectConflict("run and effect intent revisions disagree")
    return snapshot.intent


def encode_found(
    performed: PerformedEffect,
    source: ConfirmationSource,
    command_id: ReconcileCommandId | None = None,
) -> EncodedEffectResolution:
    return {
        "outcome": "FOUND",
        "effect_id": performed.effect_id.value,
        "result": performed.result.payload.hex(),
        "result_hash": performed.result.payload_hash.value,
        "confirmation_source": source.value,
        "reconcile_command_id": None if command_id is None else command_id.value,
    }


def encode_readback(
    readback: EffectReceipt | EffectAbsence | EffectUnknownOutcome,
) -> EncodedEffectResolution:
    if isinstance(readback, EffectReceipt):
        return encode_found(
            PerformedEffect(readback.effect_id, readback.result),
            readback.confirmation_source,
            readback.reconcile_command_id,
        )
    return {"outcome": readback.outcome.value}


def decode_found(intent: EffectIntent, encoded: Mapping[str, Any]) -> EffectReceipt:
    result = EffectResult.from_durable_record(
        bytes.fromhex(str(encoded["result"])),
        Sha256Hash(str(encoded["result_hash"])),
    )
    command_id = encoded.get("reconcile_command_id")
    return EffectReceipt(
        intent,
        effect_id=EffectId(str(encoded["effect_id"])),
        result=result,
        confirmation_source=ConfirmationSource(str(encoded["confirmation_source"])),
        reconcile_command_id=(
            None if command_id is None else ReconcileCommandId(str(command_id))
        ),
    )


def observe_adapter(
    session: Any,
    adapter: EffectAdapter,
    logical_key: str,
    revision_hash: str,
) -> EncodedEffectResolution:
    intent = load_intent(session, logical_key, revision_hash)
    readback = adapter.readback(intent)
    intent.authorize_adapter_readback(readback)
    return encode_readback(readback)


def resolve_observation(
    session: Any,
    adapter: EffectAdapter,
    logical_key: str,
    revision_hash: str,
    observed: Mapping[str, Any],
    authorized_command_id: ReconcileCommandId | None = None,
) -> EncodedEffectResolution:
    intent = load_intent(session, logical_key, revision_hash)
    if observed["outcome"] == "FOUND":
        if authorized_command_id is None:
            return dict(observed)
        found = decode_found(intent, observed)
        return encode_found(
            PerformedEffect(found.effect_id, found.result),
            ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION,
            authorized_command_id,
        )
    if observed["outcome"] == "UNKNOWN" and authorized_command_id is None:
        return {"outcome": "UNKNOWN"}
    performed = adapter.execute(intent)
    return encode_found(
        performed,
        (
            ConfirmationSource.ADAPTER_EXECUTION
            if authorized_command_id is None
            else ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION
        ),
        authorized_command_id,
    )


def observe_reconcile_command(
    session: Any,
    adapter: EffectAdapter,
    command_id: str,
    revision_hash: str,
) -> EncodedEffectResolution:
    command_record = (
        session.execute(
            sa.select(reconcile_commands).where(
                reconcile_commands.c.command_id == command_id
            )
        )
        .mappings()
        .one_or_none()
    )
    if command_record is None:
        raise DurableEffectConflict("reconcile workflow has no durable command")
    intent = load_intent(session, str(command_record["logical_key"]), revision_hash)
    command_snapshot = command_snapshot_from_record(command_record, intent)
    intent_record = (
        session.execute(
            sa.select(effect_intents).where(
                effect_intents.c.logical_key == intent.binding.logical_key.value
            )
        )
        .mappings()
        .one()
    )
    intent_snapshot = intent_snapshot_from_record(intent_record)
    if (
        command_snapshot.state is not ReconcileCommandState.PENDING
        or intent_snapshot.state is not EffectIntentState.RECONCILING
        or intent_snapshot.state_version != EFFECT_INTENT_VERSION_RECONCILING
        or intent_record["reconciliation_owner_command_id"] != command_id
    ):
        raise DurableEffectConflict(
            "reconcile workflow does not own its PENDING command and intent"
        )
    determination = command_snapshot.command.determination
    if isinstance(determination, OperatorFoundEffect):
        observed = encode_found(
            PerformedEffect(determination.effect_id, determination.result),
            ConfirmationSource.OPERATOR_FOUND,
            command_snapshot.command.command_id,
        )
    elif isinstance(determination, OperatorAuthoritativeAbsence):
        readback = adapter.readback(intent)
        intent.authorize_adapter_readback(readback)
        observed = encode_readback(readback)
        observed["operator_authorized"] = command_id
    else:
        assert_never(determination)
    observed["logical_key"] = intent.binding.logical_key.value
    return observed


def _receipt_values(receipt: EffectReceipt) -> dict[str, object]:
    binding = receipt.intent.binding
    return {
        "logical_key": binding.logical_key.value,
        "run_id": binding.run_id.value,
        "canonical_request": receipt.intent.request.payload,
        "request_hash": receipt.intent.request.request_hash.value,
        "workflow_revision_hash": binding.workflow_revision_hash.value,
        "adapter_revision": binding.adapter_revision.value,
        "destination_identity": binding.destination.value,
        "adapter_operational_identity": binding.adapter_operational_identity.value,
        "effect_id": receipt.effect_id.value,
        "result": receipt.result.payload,
        "result_hash": receipt.result.payload_hash.value,
        "confirmation_source": receipt.confirmation_source.value,
        "reconcile_command_id": (
            None
            if receipt.reconcile_command_id is None
            else receipt.reconcile_command_id.value
        ),
    }


def commit_resolution(
    session: Any,
    logical_key: str,
    revision_hash: str,
    resolved: Mapping[str, Any],
    command_id: ReconcileCommandId | None = None,
) -> RunState:
    intent = load_intent(session, logical_key, revision_hash)
    if resolved["outcome"] == "UNKNOWN":
        if command_id is not None:
            raise DurableEffectConflict(
                "an authorized reconciliation may not return UNKNOWN"
            )
        intent_update = session.execute(
            effect_intents.update()
            .where(
                effect_intents.c.logical_key == logical_key,
                effect_intents.c.state == EffectIntentState.PREPARED.value,
                effect_intents.c.state_version == EFFECT_INTENT_VERSION_INITIAL.value,
            )
            .values(
                state=EffectIntentState.WAITING_RECONCILIATION.value,
                state_version=EFFECT_INTENT_VERSION_WAITING.value,
            )
        )
        current = load_run(session, intent.binding.run_id)
        commit_reconciliation_required(
            session,
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            current.current_node_id,
            intent.request.payload,
        )
        if intent_update.rowcount != 1:
            raise DurableEffectConflict("UNKNOWN must advance one prepared run")
        return RunState.WAITING_RECONCILIATION

    receipt = decode_found(intent, resolved)
    if command_id is None:
        if (
            receipt.confirmation_source
            not in {
                ConfirmationSource.ADAPTER_READBACK,
                ConfirmationSource.ADAPTER_EXECUTION,
            }
            or receipt.reconcile_command_id is not None
        ):
            raise DurableEffectConflict("initial confirmation has invalid provenance")
    elif (
        receipt.confirmation_source
        not in {
            ConfirmationSource.OPERATOR_FOUND,
            ConfirmationSource.OPERATOR_AUTHORIZED_EXECUTION,
        }
        or receipt.reconcile_command_id != command_id
    ):
        raise DurableEffectConflict(
            "reconciliation confirmation has invalid provenance"
        )

    session.execute(
        effect_receipts.insert()
        .prefix_with("OR IGNORE")
        .values(_receipt_values(receipt))
    )
    durable_receipt_record = (
        session.execute(
            sa.select(effect_receipts).where(
                effect_receipts.c.logical_key == logical_key
            )
        )
        .mappings()
        .one()
    )
    if receipt_from_record(durable_receipt_record) != receipt:
        raise DurableEffectConflict("durable receipt differs from exact resolution")

    expected_state = (
        EffectIntentState.PREPARED
        if command_id is None
        else EffectIntentState.RECONCILING
    )
    expected_version = (
        EFFECT_INTENT_VERSION_INITIAL
        if command_id is None
        else EFFECT_INTENT_VERSION_RECONCILING
    )
    confirmed_version = (
        EFFECT_INTENT_VERSION_CONFIRMED_INITIAL
        if command_id is None
        else EFFECT_INTENT_VERSION_CONFIRMED_RECONCILED
    )
    intent_update = session.execute(
        effect_intents.update()
        .where(
            effect_intents.c.logical_key == logical_key,
            effect_intents.c.workflow_revision_hash == revision_hash,
            effect_intents.c.state == expected_state.value,
            effect_intents.c.state_version == expected_version.value,
            effect_intents.c.reconciliation_owner_command_id
            == (None if command_id is None else command_id.value),
        )
        .values(
            state=EffectIntentState.CONFIRMED.value,
            state_version=confirmed_version.value,
            reconciliation_owner_command_id=None,
        )
    )
    if intent_update.rowcount != 1:
        raise DurableEffectConflict(
            "effect confirmation requires one exact run binding"
        )
    if command_id is not None:
        command_update = session.execute(
            reconcile_commands.update()
            .where(
                reconcile_commands.c.command_id == command_id.value,
                reconcile_commands.c.logical_key == logical_key,
                reconcile_commands.c.state == ReconcileCommandState.PENDING.value,
            )
            .values(state=ReconcileCommandState.APPLIED.value)
        )
        if command_update.rowcount != 1:
            raise DurableEffectConflict("confirmation must apply its owning command")
        current = load_run(session, intent.binding.run_id)
        commit_reconciliation_resolved(
            session,
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            current.current_node_id,
            intent.binding.logical_key,
            receipt.result.payload,
            receipt.result.payload_hash,
        )
    return RunState.STARTED
