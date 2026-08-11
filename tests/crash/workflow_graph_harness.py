from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import sqlalchemy as sa

from atelier2.adapters.dbos.effect_store import intent_snapshot_from_record
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import effect_intents
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.answer_wait import answer_wait
from atelier2.application.reconcile_effect import reconcile_effect
from atelier2.application.start_run import start_run
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    EffectUnknownOutcome,
    OperatorAuthoritativeAbsence,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.executions import SubmitWaitAnswerRequest
from atelier2.contracts.runs import (
    RunId,
    StartRunRequest,
    WorkflowRevision,
    WorkflowRevisionHash,
)
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
    database: Path, external: Path, version: str, force_unknown_marker: Path
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(database, version),
        HarnessEffectAdapterFactory(external, force_unknown_marker),
    )


def initialize(
    database: Path, external: Path, version: str, force_unknown_marker: Path
) -> None:
    lease = runtime(database, external, version, force_unknown_marker)
    try:
        lease.initialize_storage()
    finally:
        lease.close()


def seed(
    database: Path,
    external: Path,
    version: str,
    force_unknown_marker: Path,
    run_id: str,
    document: bytes,
) -> None:
    lease = runtime(database, external, version, force_unknown_marker)
    try:
        lease.initialize_storage()
        start_run(
            StartRunRequest(RunId(run_id), WorkflowRevision(document)),
            DbosDurableRunStarter(lease.engine, lease.settings),
        )
    finally:
        lease.close()


def submit_answer(
    database: Path,
    external: Path,
    version: str,
    force_unknown_marker: Path,
    run_id: str,
    node_id: str,
    answer: str,
) -> None:
    lease = runtime(database, external, version, force_unknown_marker)
    try:
        lease.initialize_storage()
        with sqlite3.connect(database, timeout=30) as connection:
            revision_hash = str(
                connection.execute(
                    "SELECT revision_hash FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
        answer_wait(
            SubmitWaitAnswerRequest(
                RunId(run_id),
                WorkflowRevisionHash(revision_hash),
                node_id,
                answer.encode("utf-8"),
            ),
            DbosWaitAnswerer(lease.engine, lease.settings.application_version),
        )
    finally:
        lease.close()


def _crash_once(marker: Path, operation_name: str) -> None:
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.write(descriptor, f"{operation_name}:before-record".encode())
    os.close(descriptor)
    os._exit(CRASHED)


def install_crash(marker: Path, operation_name: str) -> None:
    from dbos._sys_db import OperationResultInternal, SystemDatabase

    original = SystemDatabase.record_operation_result

    def injected(
        self: SystemDatabase,
        result: OperationResultInternal,
        *,
        completed_at_epoch_ms: int | None = None,
    ) -> None:
        if result.get("function_name") == operation_name:
            _crash_once(marker, operation_name)
        original(self, result, completed_at_epoch_ms=completed_at_epoch_ms)

    SystemDatabase.record_operation_result = injected


def execute_until(
    database: Path,
    external: Path,
    version: str,
    force_unknown_marker: Path,
    run_id: str,
    target_state: str,
    marker: Path | None,
    operation_name: str | None,
) -> None:
    lease = runtime(database, external, version, force_unknown_marker)
    try:
        if marker is not None and operation_name is not None:
            install_crash(marker, operation_name)
        lease.launch()
        deadline = time.monotonic() + 10
        observed = ""
        while time.monotonic() < deadline:
            with sqlite3.connect(database, timeout=30) as connection:
                observed = str(
                    connection.execute(
                        "SELECT state FROM runs WHERE run_id=?", (run_id,)
                    ).fetchone()[0]
                )
                failed = tuple(
                    row[0]
                    for row in connection.execute(
                        "SELECT status FROM workflow_status "
                        "WHERE status IN ('ERROR','CANCELLED')"
                    )
                )
            if failed:
                raise RuntimeError(f"durable workflow failed with {failed!r}")
            if observed == target_state:
                return
            time.sleep(0.025)
        raise TimeoutError(f"run stayed {observed!r}, expected {target_state!r}")
    finally:
        lease.close()


def reconcile_absence(
    database: Path,
    external: Path,
    version: str,
    force_unknown_marker: Path,
    run_id: str,
) -> None:
    lease = runtime(database, external, version, force_unknown_marker)
    try:
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
            snapshot.intent.reference,
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
    elif command == "answer":
        run_id, node_id, answer = arguments
        submit_answer(
            database,
            external,
            version,
            unknown_marker,
            run_id,
            node_id,
            answer,
        )
    elif command == "reconcile":
        (run_id,) = arguments
        reconcile_absence(database, external, version, unknown_marker, run_id)
    else:
        run_id, raw_marker, raw_operation = arguments
        target_state = {
            "execute-until-reconcile": "WAITING_RECONCILIATION",
            "execute-until-wait": "WAITING_INPUT",
            "execute-until-complete": "COMPLETED",
        }[command]
        execute_until(
            database,
            external,
            version,
            unknown_marker,
            run_id,
            target_state,
            None if raw_marker == "NONE" else Path(raw_marker),
            None if raw_operation == "NONE" else raw_operation,
        )
    print(json.dumps({"command": command, "ok": True}))


if __name__ == "__main__":
    main()
