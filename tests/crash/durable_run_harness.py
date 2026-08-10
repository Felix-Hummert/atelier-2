from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.application.start_run import start_run
from atelier2.contracts.runs import RunId, StartRunRequest, WorkflowRevision

CRASHED = 86


def _once(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(descriptor)
    os._exit(CRASHED)


def _install_after_datasource_crash(marker: Path) -> None:
    from dbos._sys_db import OperationResultInternal, SystemDatabase

    original = SystemDatabase.record_operation_result

    def injected(
        self: SystemDatabase,
        result: OperationResultInternal,
        *,
        completed_at_epoch_ms: int | None = None,
    ) -> None:
        if result.get("function_name") == "complete-run":
            _once(marker)
        original(self, result, completed_at_epoch_ms=completed_at_epoch_ms)

    SystemDatabase.record_operation_result = injected


def initialize(database: Path, application_version: str) -> None:
    runtime = DbosRuntime(DbosRuntimeSettings(database, application_version))
    try:
        runtime.initialize_storage()
    finally:
        runtime.close()


def seed(
    database: Path, application_version: str, run_id: str, document: bytes
) -> None:
    runtime = DbosRuntime(DbosRuntimeSettings(database, application_version))
    try:
        start_run(
            StartRunRequest(RunId(run_id), WorkflowRevision(document)),
            DbosDurableRunStarter(runtime.engine, runtime.settings),
        )
    finally:
        runtime.close()


def execute(
    database: Path,
    application_version: str,
    run_id: str,
    marker: Path | None,
    wait_seconds: float,
) -> None:
    runtime = DbosRuntime(DbosRuntimeSettings(database, application_version))
    try:
        if marker is not None:
            _install_after_datasource_crash(marker)
        runtime.launch()
        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            with sqlite3.connect(database) as connection:
                state = connection.execute(
                    "SELECT status FROM workflow_status "
                    "WHERE workflow_uuid=(SELECT dbos_workflow_id FROM runs WHERE run_id=?)",
                    (run_id,),
                ).fetchone()[0]
            if state == "SUCCESS":
                break
            time.sleep(0.025)
    finally:
        runtime.close()


def main() -> None:
    command, raw_database, application_version, *arguments = sys.argv[1:]
    database = Path(raw_database)
    if command == "initialize":
        initialize(database, application_version)
    elif command == "seed":
        run_id, document_hex = arguments
        seed(database, application_version, run_id, bytes.fromhex(document_hex))
    else:
        run_id, raw_marker, raw_wait = arguments
        marker = None if raw_marker == "NONE" else Path(raw_marker)
        execute(database, application_version, run_id, marker, float(raw_wait))
    print(json.dumps({"command": command, "ok": True}))


if __name__ == "__main__":
    main()
