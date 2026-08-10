from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import sqlalchemy as sa

from atelier2.adapters.dbos.advancer import (
    DbosDurableRunAdvancer,
)
from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.advance_run import advance_run
from atelier2.application.reconcile_effect import reconcile_effect
from atelier2.application.start_run import start_run
from atelier2.contracts.effects import (
    AdapterRevision,
    CanonicalRequest,
    EffectAdapterBinding,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    EffectUnknownOutcome,
    LogicalEffectKey,
    OperatorAuthoritativeAbsence,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.runs import RunId, StartRunRequest, WorkflowRevision
from atelier2.ports.effects import EffectAdapter

CRASHED = 86


class HarnessEffectAdapter:
    def __init__(self, delegate: EffectAdapter, force_unknown_marker: Path) -> None:
        self._delegate = delegate
        self._force_unknown_marker = force_unknown_marker

    def readback(self, intent: EffectIntent) -> EffectReadback:
        if self._force_unknown_marker.exists():
            return EffectUnknownOutcome(intent.reference)
        return self._delegate.readback(intent)

    def execute(self, intent: EffectIntent) -> PerformedEffect:
        return self._delegate.execute(intent)

    def close(self) -> None:
        self._delegate.close()


class HarnessEffectAdapterFactory:
    def __init__(self, database: Path, force_unknown_marker: Path) -> None:
        self._delegate = LoopbackEffectAdapterFactory(
            database,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-crash-test"),
        )
        self._force_unknown_marker = force_unknown_marker

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    def open(self) -> HarnessEffectAdapter:
        return HarnessEffectAdapter(self._delegate.open(), self._force_unknown_marker)


def runtime(
    database: Path,
    external: Path,
    application_version: str,
    force_unknown_marker: Path,
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(database, application_version),
        HarnessEffectAdapterFactory(external, force_unknown_marker),
    )


def intent_for(
    runtime_lease: DbosRuntime, run_id: str, document: bytes
) -> EffectIntent:
    revision = WorkflowRevision(document)
    binding = runtime_lease.effect_adapter_binding
    return EffectIntent(
        EffectBinding(
            LogicalEffectKey(f"{run_id}/action-1"),
            RunId(run_id),
            revision.revision_hash,
            binding.adapter_revision,
            binding.destination,
            binding.operational_identity,
        ),
        CanonicalRequest(b"exact-request"),
    )


def load_intent(runtime_lease: DbosRuntime, run_id: str) -> EffectIntent:
    with runtime_lease.engine.connect() as connection:
        record = (
            connection.execute(
                sa.select(effect_intents).where(effect_intents.c.run_id == run_id)
            )
            .mappings()
            .one()
        )
    return intent_snapshot_from_record(record).intent


def initialize(
    database: Path, external: Path, version: str, unknown_marker: Path
) -> None:
    lease = runtime(database, external, version, unknown_marker)
    try:
        lease.initialize_storage()
    finally:
        lease.close()


def seed(
    database: Path,
    external: Path,
    version: str,
    unknown_marker: Path,
    run_id: str,
    document: bytes,
) -> None:
    lease = runtime(database, external, version, unknown_marker)
    try:
        lease.initialize_storage()
        intent = intent_for(lease, run_id, document)
        start_run(
            StartRunRequest(intent.binding.run_id, WorkflowRevision(document)),
            DbosDurableRunStarter(lease.engine, lease.settings),
        )
        advance_run(
            intent,
            DbosDurableRunAdvancer(
                lease.engine, lease.settings, lease.effect_adapter_binding
            ),
        )
    finally:
        lease.close()


def _crash_once(marker: Path, operation_name: str, timing: str) -> None:
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.write(descriptor, f"{operation_name}:{timing}".encode())
    os.close(descriptor)
    os._exit(CRASHED)


def install_crash(marker: Path, operation_name: str, timing: str) -> None:
    from dbos._sys_db import OperationResultInternal, SystemDatabase

    original = SystemDatabase.record_operation_result

    def injected(
        self: SystemDatabase,
        result: OperationResultInternal,
        *,
        completed_at_epoch_ms: int | None = None,
    ) -> None:
        if result.get("function_name") != operation_name:
            original(self, result, completed_at_epoch_ms=completed_at_epoch_ms)
            return
        if timing == "after-record":
            original(self, result, completed_at_epoch_ms=completed_at_epoch_ms)
            _crash_once(marker, operation_name, timing)
            return
        _crash_once(marker, operation_name, timing)
        original(self, result, completed_at_epoch_ms=completed_at_epoch_ms)

    SystemDatabase.record_operation_result = injected


def execute(
    database: Path,
    external: Path,
    version: str,
    unknown_marker: Path,
    run_id: str,
    crash_marker: Path | None,
    operation_name: str,
    timing: str,
    wait_seconds: float,
) -> None:
    lease = runtime(database, external, version, unknown_marker)
    try:
        if crash_marker is not None:
            install_crash(crash_marker, operation_name, timing)
        lease.launch()
        deadline = time.monotonic() + wait_seconds
        statuses: tuple[str, ...] = ()
        while time.monotonic() < deadline:
            with sqlite3.connect(database, timeout=30) as connection:
                statuses = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT status FROM workflow_status WHERE application_version=?",
                        (version,),
                    )
                )
            failed = set(statuses) & {"ERROR", "CANCELLED"}
            if failed:
                raise RuntimeError(f"durable workflow failed with {sorted(failed)!r}")
            if statuses and set(statuses) == {"SUCCESS"}:
                break
            time.sleep(0.025)
        else:
            raise TimeoutError(
                f"durable workflows did not finish within {wait_seconds}s: {statuses!r}"
            )
    finally:
        lease.close()


def submit_absence(
    database: Path,
    external: Path,
    version: str,
    unknown_marker: Path,
    run_id: str,
) -> None:
    lease = runtime(database, external, version, unknown_marker)
    try:
        intent = load_intent(lease, run_id)
        with lease.engine.connect() as connection:
            snapshot = intent_snapshot_from_record(
                connection.execute(
                    sa.select(effect_intents).where(effect_intents.c.run_id == run_id)
                )
                .mappings()
                .one()
            )
        command = ReconcileCommand(
            ReconcileCommandId(f"{run_id}/reconcile-1"),
            intent.reference,
            snapshot.state_version,
            ReconcileActor("operator"),
            "inspected the exact external destination",
            OperatorAuthoritativeAbsence(),
        )
        reconcile_effect(
            command, DbosEffectReconcileCommander(lease.engine, lease.settings)
        )
    finally:
        lease.close()


def main() -> None:
    (
        command,
        raw_database,
        raw_external,
        version,
        raw_unknown_marker,
        *arguments,
    ) = sys.argv[1:]
    database = Path(raw_database)
    external = Path(raw_external)
    unknown_marker = Path(raw_unknown_marker)
    if command == "initialize":
        initialize(database, external, version, unknown_marker)
    elif command == "seed":
        run_id, document_hex = arguments
        seed(
            database,
            external,
            version,
            unknown_marker,
            run_id,
            bytes.fromhex(document_hex),
        )
    elif command == "submit-absence":
        (run_id,) = arguments
        submit_absence(database, external, version, unknown_marker, run_id)
    else:
        run_id, raw_marker, operation_name, timing, raw_wait = arguments
        execute(
            database,
            external,
            version,
            unknown_marker,
            run_id,
            None if raw_marker == "NONE" else Path(raw_marker),
            operation_name,
            timing,
            float(raw_wait),
        )
    print(json.dumps({"command": command, "ok": True}))


if __name__ == "__main__":
    main()
