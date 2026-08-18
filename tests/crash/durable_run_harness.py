from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

from atelier2.adapters.dbos.names import BOOTSTRAP_STEP_NAME
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.runs import RunId, WorkflowRevision
from tests.scenarios.runs import start_published_v1_run

CRASHED = 86
UNSETTLED_STATUSES = ("PENDING", "ENQUEUED")


def _runtime(database: Path, application_version: str) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(database, application_version),
        LoopbackEffectAdapterFactory(
            database.with_name("external-effect.sqlite"),
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
    )


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
        if result.get("function_name") == BOOTSTRAP_STEP_NAME:
            _once(marker)
        original(self, result, completed_at_epoch_ms=completed_at_epoch_ms)

    SystemDatabase.record_operation_result = injected


def initialize(database: Path, application_version: str) -> None:
    runtime = _runtime(database, application_version)
    try:
        runtime.initialize_storage()
    finally:
        runtime.close()


def seed(
    database: Path, application_version: str, run_id: str, document: bytes
) -> None:
    runtime = _runtime(database, application_version)
    try:
        start_published_v1_run(
            runtime.engine,
            runtime.settings,
            RunId(run_id),
            WorkflowRevision(document),
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
    runtime = _runtime(database, application_version)
    try:
        if marker is not None:
            _install_after_datasource_crash(marker)
        runtime.launch()
        deadline = time.monotonic() + wait_seconds
        state = "PENDING"
        running = 1
        while time.monotonic() < deadline:
            with sqlite3.connect(database) as connection:
                state = connection.execute(
                    "SELECT status FROM workflow_status "
                    "WHERE workflow_uuid=(SELECT bootstrap_workflow_id FROM runs WHERE run_id=?)",
                    (run_id,),
                ).fetchone()[0]
                running = connection.execute(
                    "SELECT COUNT(*) FROM workflow_status WHERE status IN (?, ?)",
                    UNSETTLED_STATUSES,
                ).fetchone()[0]
            # The bootstrap workflow only starts the node chain, so its own SUCCESS
            # says nothing about the run state the chain writes. Returning while a
            # started workflow is still running would leave the durable outcome to
            # the shutdown drain, which is bounded by wall time.
            if state == "SUCCESS" and running == 0:
                return
            if state in {"ERROR", "CANCELLED"}:
                raise RuntimeError(f"bootstrap workflow failed with {state}")
            time.sleep(0.025)
        raise TimeoutError(
            f"bootstrap workflow stayed {state} with {running} workflows still "
            f"running for {wait_seconds} seconds"
        )
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
