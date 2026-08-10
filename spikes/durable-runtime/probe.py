from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from dbos import DBOS, DBOSClient, EnqueueOptions, SQLAlchemyDatasource
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.runtime import (
    EXECUTOR_ID,
    create_canonical_engine,
    sqlite_url,
)
from atelier2.adapters.dbos.schema import initialize_schema

APPLICATION_VERSION = "c3-probe-v1"
QUEUE_NAME = "atelier2-c3-probe"
WORKFLOW_NAME = "atelier2_c3_probe"
WORKFLOW_ID = "atelier2-c3-probe-run"
INPUT_STEP_NAME = "accept-input"
FINISH_STEP_NAME = "finish-after-input"
CRASHED = 86


def database(root: Path) -> Path:
    return root / "atelier.sqlite"


def marker(root: Path) -> Path:
    return root / "c3-after-input"


def _dbos_config(path: Path, engine: Engine) -> dict[str, Any]:
    return {
        "name": "atelier2-c3-probe",
        "system_database_url": sqlite_url(path),
        "system_database_engine": engine,
        "application_version": APPLICATION_VERSION,
        "executor_id": EXECUTOR_ID,
        "use_listen_notify": False,
        "notification_listener_polling_interval_sec": 0.01,
    }


def _crash_once(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.write(descriptor, b"C3:after-durable-input")
    os.close(descriptor)
    os._exit(CRASHED)


def _register(root: Path, datasource: SQLAlchemyDatasource) -> None:
    def accept_input() -> int:
        return 5

    def finish(answer: int) -> str:
        result = hashlib.sha256(f"durable-input:{answer}".encode()).hexdigest()
        session = datasource.sql_session()
        session.execute(
            sa.text(
                "INSERT OR IGNORE INTO probe_c3_results(singleton, result_hash) "
                "VALUES(1, :result)"
            ),
            {"result": result},
        )
        stored = session.scalar(
            sa.text("SELECT result_hash FROM probe_c3_results WHERE singleton=1")
        )
        if stored != result:
            raise RuntimeError("C3 replay changed the post-input result")
        return result

    @DBOS.workflow(name=WORKFLOW_NAME, max_recovery_attempts=None)
    def c3_probe() -> str:
        answer = datasource.run_tx_step({"name": INPUT_STEP_NAME}, accept_input)
        _crash_once(marker(root))
        return str(datasource.run_tx_step({"name": FINISH_STEP_NAME}, finish, answer))


def _open(root: Path) -> tuple[Engine, SQLAlchemyDatasource]:
    root.mkdir(parents=True, exist_ok=True)
    engine = create_canonical_engine(database(root))
    initialize_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE IF NOT EXISTS probe_c3_results("
                "singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "result_hash TEXT NOT NULL)"
            )
        )
    datasource = SQLAlchemyDatasource.create(sqlite_url(database(root)), engine=engine)
    _register(root, datasource)
    return engine, datasource


def initialize(root: Path) -> None:
    engine, _datasource = _open(root)
    try:
        DBOS(config=_dbos_config(database(root), engine))
        DBOS.launch()
        DBOS.register_queue(
            QUEUE_NAME, polling_interval_sec=0.05, on_conflict="always_update"
        )
        DBOS.destroy(destroy_registry=True)
    finally:
        engine.dispose()


def seed(root: Path) -> None:
    engine, _datasource = _open(root)
    client = DBOSClient(system_database_engine=engine, use_listen_notify=False)
    try:
        options: EnqueueOptions = {
            "workflow_name": WORKFLOW_NAME,
            "queue_name": QUEUE_NAME,
            "workflow_id": WORKFLOW_ID,
            "app_version": APPLICATION_VERSION,
        }
        with engine.begin() as connection:
            client.enqueue_in_transaction(connection, options)
    finally:
        client.destroy()
        DBOS.destroy(destroy_registry=True)
        engine.dispose()


def execute(root: Path, wait_seconds: float) -> None:
    engine, _datasource = _open(root)
    try:
        DBOS(config=_dbos_config(database(root), engine))
        DBOS.launch()
        DBOS.register_queue(
            QUEUE_NAME, polling_interval_sec=0.05, on_conflict="always_update"
        )
        deadline = time.monotonic() + wait_seconds
        status = "PENDING"
        while time.monotonic() < deadline:
            with sqlite3.connect(database(root), timeout=30) as connection:
                status = str(
                    connection.execute(
                        "SELECT status FROM workflow_status WHERE workflow_uuid=?",
                        (WORKFLOW_ID,),
                    ).fetchone()[0]
                )
            if status == "SUCCESS":
                return
            if status in {"ERROR", "CANCELLED"}:
                raise RuntimeError(f"C3 workflow failed with {status}")
            time.sleep(0.025)
        raise TimeoutError(f"C3 workflow stayed {status} for {wait_seconds}s")
    finally:
        DBOS.destroy(destroy_registry=True, workflow_completion_timeout_sec=1)
        engine.dispose()


def main() -> None:
    command, raw_root, *arguments = sys.argv[1:]
    root = Path(raw_root).resolve()
    if command == "initialize":
        initialize(root)
    elif command == "seed":
        seed(root)
    else:
        (raw_wait,) = arguments
        execute(root, float(raw_wait))
    print(json.dumps({"command": command, "ok": True}))


if __name__ == "__main__":
    main()
