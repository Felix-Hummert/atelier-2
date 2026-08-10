from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, assert_never

import sqlalchemy as sa
from dbos import DBOS, SQLAlchemyDatasource
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.runtime import (
    EXECUTOR_ID,
    DbosRuntimeSettings,
    create_canonical_engine,
    sqlite_url,
)
from atelier2.adapters.dbos.schema import initialize_schema
from atelier2.adapters.dbos.starter import DbosDurableRunStarter, dbos_workflow_id_for
from atelier2.adapters.dbos.workflow import QUEUE_NAME, WORKFLOW_NAME
from atelier2.application.start_run import start_run as start_product_run
from atelier2.contracts.runs import RunId, StartRunRequest, WorkflowRevision

APP_A = "executor-A"
APP_B = "executor-B"
REVISION_DOCUMENT = b"atelier2-ad0-workflow-v1"
REVISION = hashlib.sha256(REVISION_DOCUMENT).hexdigest()
REQUEST = hashlib.sha256(b"action-request-v1").hexdigest()
RESULT = hashlib.sha256(b"action-result-v1").hexdigest()
EFFECT_ADAPTER_REVISION = hashlib.sha256(b"loopback-effect-v1").hexdigest()
CRASHED = 86

PROBE_SCHEMA = """
CREATE TABLE IF NOT EXISTS effect_intents(
 logical_key TEXT PRIMARY KEY, run_id TEXT NOT NULL, request_hash TEXT NOT NULL,
 revision TEXT NOT NULL, adapter_revision TEXT NOT NULL, state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS effect_receipts(
 logical_key TEXT PRIMARY KEY, effect_id TEXT NOT NULL, request_hash TEXT NOT NULL,
 result_hash TEXT NOT NULL, revision TEXT NOT NULL, adapter_revision TEXT NOT NULL);
"""
EFFECT_SCHEMA = """
CREATE TABLE IF NOT EXISTS effects(
 logical_key TEXT PRIMARY KEY, effect_id TEXT UNIQUE NOT NULL,
 request_hash TEXT NOT NULL, result_hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS effect_calls(logical_key TEXT PRIMARY KEY, calls INTEGER NOT NULL);
"""
REQUIRED_TABLES = {
    "application_versions",
    "atelier_schema_versions",
    "datasource_outputs",
    "dbos_migrations",
    "effect_intents",
    "effect_receipts",
    "operation_outputs",
    "queues",
    "runs",
    "workflow_revisions",
    "workflow_status",
}


class ProbeFailure(AssertionError):
    pass


class IntentMismatch(RuntimeError):
    pass


class EffectReadbackKind(StrEnum):
    FOUND = "FOUND"
    AUTHORITATIVE_NOT_FOUND = "AUTHORITATIVE_NOT_FOUND"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class EffectReadback:
    kind: EffectReadbackKind
    effect_id: str | None = None
    result_hash: str | None = None


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ProbeFailure(message)


def live_db(workspace: Path) -> Path:
    return workspace / "atelier2.sqlite"


def effect_db(workspace: Path) -> Path:
    return workspace / "external-effect.sqlite"


def scalar(path: Path, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
    with sqlite3.connect(path, timeout=30.0) as connection:
        row = connection.execute(sql, parameters).fetchone()
    return None if row is None else row[0]


def rows(
    path: Path, sql: str, parameters: tuple[Any, ...] = ()
) -> list[tuple[Any, ...]]:
    with sqlite3.connect(path, timeout=30.0) as connection:
        return connection.execute(sql, parameters).fetchall()


def count(
    path: Path, table: str, where: str = "", parameters: tuple[Any, ...] = ()
) -> int:
    suffix = f" WHERE {where}" if where else ""
    return int(scalar(path, f"SELECT COUNT(*) FROM {table}{suffix}", parameters))


def run_state(workspace: Path, run_id: str) -> str:
    return str(
        scalar(live_db(workspace), "SELECT state FROM runs WHERE run_id=?", (run_id,))
    )


def effect_call_count(workspace: Path, run_id: str) -> int:
    return count(effect_db(workspace), "effect_calls", "logical_key=?", (key(run_id),))


def initialize(workspace: Path) -> None:
    path = live_db(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    engine = create_canonical_engine(path)
    try:
        initialize_schema(engine)
        with engine.begin() as connection:
            for statement in PROBE_SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(sa.text(statement))
        SQLAlchemyDatasource.create(sqlite_url(path), engine=engine)
        DBOS(config=dbos_config(path, engine, APP_A))
        DBOS.launch()
        DBOS.register_queue(
            QUEUE_NAME, polling_interval_sec=0.05, on_conflict="always_update"
        )
        DBOS.destroy(destroy_registry=True)
    finally:
        engine.dispose()


def dbos_config(path: Path, engine: Engine, application_version: str) -> dict[str, Any]:
    return {
        "name": "atelier2-ad0",
        "system_database_url": sqlite_url(path),
        "system_database_engine": engine,
        "application_version": application_version,
        "executor_id": EXECUTOR_ID,
        "use_listen_notify": False,
        "notification_listener_polling_interval_sec": 0.01,
    }


def once(workspace: Path, actual: str, expected: str) -> None:
    if actual != expected:
        return
    marker = workspace / f"crash-{expected}.triggered"
    try:
        descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(descriptor)
    os._exit(CRASHED)


def key(run_id: str) -> str:
    return f"{run_id}/action-1"


def validate_intent_binding(
    record: tuple[Any, ...],
    request_hash: str,
    revision: str,
    adapter_revision: str,
) -> None:
    if tuple(record[:3]) != (request_hash, revision, adapter_revision):
        raise IntentMismatch(
            "logical key changed request, workflow, or adapter revision"
        )


def configure_workflow(
    workspace: Path, datasource: SQLAlchemyDatasource
) -> Callable[[str, str], str]:
    def checkpoint(run_id: str, die_inside: bool) -> str:
        if die_inside:
            once(workspace, "IN_TX", "IN_TX")
        return "CHECKPOINTED"

    def prepare(run_id: str) -> str:
        session = datasource.sql_session()
        existing = session.execute(
            sa.text(
                "SELECT request_hash, revision, adapter_revision, state "
                "FROM effect_intents WHERE logical_key=:key"
            ),
            {"key": key(run_id)},
        ).fetchone()
        if existing:
            validate_intent_binding(
                existing, REQUEST, REVISION, EFFECT_ADAPTER_REVISION
            )
            return str(existing[3])
        session.execute(
            sa.text(
                "INSERT INTO effect_intents "
                "VALUES(:key,:run_id,:request,:revision,:adapter,'PREPARED')"
            ),
            {
                "key": key(run_id),
                "run_id": run_id,
                "request": REQUEST,
                "revision": REVISION,
                "adapter": EFFECT_ADAPTER_REVISION,
            },
        )
        return "PREPARED"

    def wait_unknown(run_id: str) -> str:
        session = datasource.sql_session()
        session.execute(
            sa.text(
                "UPDATE effect_intents SET state='UNKNOWN_OUTCOME' WHERE logical_key=:key"
            ),
            {"key": key(run_id)},
        )
        return "WAITING_RECONCILIATION"

    def confirm(run_id: str, effect_id: str, result_hash: str) -> str:
        session = datasource.sql_session()
        session.execute(
            sa.text(
                "INSERT OR IGNORE INTO effect_receipts "
                "VALUES(:key,:effect,:request,:result,:revision,:adapter)"
            ),
            {
                "key": key(run_id),
                "effect": effect_id,
                "request": REQUEST,
                "result": result_hash,
                "revision": REVISION,
                "adapter": EFFECT_ADAPTER_REVISION,
            },
        )
        receipt = session.execute(
            sa.text(
                "SELECT effect_id, request_hash, result_hash, revision, adapter_revision "
                "FROM effect_receipts WHERE logical_key=:key"
            ),
            {"key": key(run_id)},
        ).one()
        if tuple(receipt) != (
            effect_id,
            REQUEST,
            result_hash,
            REVISION,
            EFFECT_ADAPTER_REVISION,
        ):
            raise IntentMismatch("receipt provenance changed")
        session.execute(
            sa.text(
                "UPDATE effect_intents SET state='CONFIRMED' WHERE logical_key=:key"
            ),
            {"key": key(run_id)},
        )
        return "CONFIRMED"

    def accept_input(run_id: str) -> int:
        return 5

    def finish(run_id: str, answer: int) -> str:
        final_hash = hashlib.sha256(
            f"{REVISION}:{RESULT}:{answer}".encode()
        ).hexdigest()
        result = datasource.sql_session().execute(
            sa.text(
                "UPDATE runs SET state='COMPLETED' "
                "WHERE run_id=:id AND revision_hash=:revision AND state='STARTED'"
            ),
            {"id": run_id, "revision": REVISION},
        )
        require(result.rowcount == 1, "final transition binding")
        return final_hash

    @DBOS.workflow(name=WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_run(run_id: str, revision_hash: str) -> str:
        require(revision_hash == REVISION, "starter revision binding")
        crash_point = probe_crash_point(run_id)
        effect_mode = "UNKNOWN" if run_id == "unknown" else "READBACK"
        datasource.run_tx_step(
            {"name": "checkpoint"}, checkpoint, run_id, crash_point == "IN_TX"
        )
        once(workspace, crash_point, "C1")
        datasource.run_tx_step({"name": "prepare"}, prepare, run_id)
        readback = read_effect(workspace, key(run_id), effect_mode)
        match readback.kind:
            case EffectReadbackKind.UNKNOWN:
                return datasource.run_tx_step(
                    {"name": "wait-unknown"}, wait_unknown, run_id
                )
            case EffectReadbackKind.AUTHORITATIVE_NOT_FOUND:
                effect_id, result_hash = execute_effect(workspace, key(run_id), REQUEST)
            case EffectReadbackKind.FOUND:
                require(
                    readback.effect_id is not None and readback.result_hash is not None,
                    "FOUND readback has no result",
                )
                effect_id, result_hash = readback.effect_id, readback.result_hash
            case unexpected:
                assert_never(unexpected)
        once(workspace, crash_point, "C2")
        datasource.run_tx_step(
            {"name": "confirm"}, confirm, run_id, effect_id, result_hash
        )
        answer = datasource.run_tx_step({"name": "input"}, accept_input, run_id)
        once(workspace, crash_point, "C3")
        return datasource.run_tx_step({"name": "finish"}, finish, run_id, answer)

    return durable_run


def probe_crash_point(run_id: str) -> str:
    if run_id in {"ambiguous", "concurrent"}:
        return "C2"
    if run_id in {"c1", "c2", "c3"}:
        return run_id.upper()
    if run_id == "inside":
        return "IN_TX"
    return "NONE"


def create_effect_store(workspace: Path) -> None:
    with sqlite3.connect(effect_db(workspace)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript(EFFECT_SCHEMA)


def read_effect(workspace: Path, logical_key: str, mode: str) -> EffectReadback:
    if mode == "UNKNOWN":
        return EffectReadback(EffectReadbackKind.UNKNOWN)
    with sqlite3.connect(effect_db(workspace), timeout=30.0) as connection:
        row = connection.execute(
            "SELECT effect_id, result_hash FROM effects WHERE logical_key=?",
            (logical_key,),
        ).fetchone()
    return (
        EffectReadback(EffectReadbackKind.AUTHORITATIVE_NOT_FOUND)
        if row is None
        else EffectReadback(EffectReadbackKind.FOUND, str(row[0]), str(row[1]))
    )


def execute_effect(
    workspace: Path, logical_key: str, request_hash: str
) -> tuple[str, str]:
    with sqlite3.connect(
        effect_db(workspace), timeout=30.0, isolation_level=None
    ) as connection:
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT effect_id, request_hash, result_hash FROM effects WHERE logical_key=?",
            (logical_key,),
        ).fetchone()
        if row:
            if row[1] != request_hash:
                raise IntentMismatch("external key changed request")
            connection.commit()
            return (str(row[0]), str(row[2]))
        effect_id = hashlib.sha256(logical_key.encode()).hexdigest()
        connection.execute(
            "INSERT INTO effects VALUES(?,?,?,?)",
            (logical_key, effect_id, request_hash, RESULT),
        )
        connection.execute("INSERT INTO effect_calls VALUES(?,1)", (logical_key,))
        connection.commit()
    return (effect_id, RESULT)


def install_lag_crash(workspace: Path) -> None:
    from dbos._sys_db import SystemDatabase

    original = SystemDatabase.record_operation_result

    def injected(self: Any, result: dict[str, Any]) -> None:
        if result.get("function_name") == "checkpoint":
            once(workspace, "AFTER_DATASOURCE", "AFTER_DATASOURCE")
        original(self, result)

    SystemDatabase.record_operation_result = injected


def runtime(
    workspace: Path, version: str, run_id: str, fault: str, wait: float
) -> None:
    path = live_db(workspace)
    engine = create_canonical_engine(path)
    try:
        datasource = SQLAlchemyDatasource.create(sqlite_url(path), engine=engine)
        configure_workflow(workspace, datasource)
        if fault == "AFTER_DATASOURCE":
            install_lag_crash(workspace)
        DBOS(config=dbos_config(path, engine, version))
        DBOS.launch()
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            state = scalar(path, "SELECT state FROM runs WHERE run_id=?", (run_id,))
            unknown = scalar(
                path,
                "SELECT state FROM effect_intents WHERE logical_key=?",
                (key(run_id),),
            )
            if state == "COMPLETED" or unknown == "UNKNOWN_OUTCOME":
                break
            time.sleep(0.025)
        DBOS.destroy(destroy_registry=True, workflow_completion_timeout_sec=1)
    finally:
        engine.dispose()


def seed(workspace: Path, run_id: str) -> None:
    create_effect_store(workspace)
    path = live_db(workspace)
    settings = DbosRuntimeSettings(path, APP_A)
    engine = create_canonical_engine(path)
    try:
        start_product_run(
            StartRunRequest(RunId(run_id), WorkflowRevision(REVISION_DOCUMENT)),
            DbosDurableRunStarter(engine, settings),
        )
    finally:
        engine.dispose()


def internal_argv(workspace: Path, command: str, *arguments: str) -> list[str]:
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "internal",
        command,
        str(workspace),
        *arguments,
    ]


def child(
    workspace: Path, command: str, *arguments: str, code: int = 0, timeout: float = 20.0
) -> None:
    result = subprocess.run(
        internal_argv(workspace, command, *arguments),
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )
    require(
        result.returncode == code,
        f"{command}: {result.returncode}/{code}; {result.stderr}",
    )


def new_case(workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    child(workspace, "initialize")


def start(
    workspace: Path,
    run_id: str,
    crash: str = "NONE",
    mode: str = "READBACK",
) -> None:
    require(
        probe_crash_point(run_id) == crash or crash == "DATASOURCE",
        "probe crash case binding",
    )
    require((run_id == "unknown") == (mode == "UNKNOWN"), "probe mode binding")
    child(workspace, "seed", run_id)


def run(
    workspace: Path,
    run_id: str,
    version: str = APP_A,
    fault: str = "NONE",
    code: int = 0,
    wait: float = 12.0,
) -> None:
    child(
        workspace,
        "runtime",
        run_id,
        version,
        fault,
        str(wait),
        code=code,
        timeout=wait + 8,
    )


def completed(workspace: Path, run_id: str) -> str:
    record = rows(
        live_db(workspace),
        "SELECT state, revision_hash FROM runs WHERE run_id=?",
        (run_id,),
    )[0]
    require(record == ("COMPLETED", REVISION), f"bad run: {record}")
    require(
        rows(
            live_db(workspace),
            "SELECT request_hash, result_hash, revision, adapter_revision "
            "FROM effect_receipts WHERE logical_key=?",
            (key(run_id),),
        )
        == [(REQUEST, RESULT, REVISION, EFFECT_ADAPTER_REVISION)],
        "bad receipt",
    )
    return hashlib.sha256(f"{REVISION}:{RESULT}:5".encode()).hexdigest()


def canonical_sqlite(workspace: Path) -> None:
    new_case(workspace)
    path = live_db(workspace)
    tables = {
        row[0]
        for row in rows(path, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    require(REQUIRED_TABLES <= tables, "authority tables missing")
    require(
        [entry for entry in workspace.glob("*.sqlite")] == [path],
        "hidden live database",
    )
    backup = workspace / "backup.sqlite"
    with sqlite3.connect(path) as source, sqlite3.connect(backup) as destination:
        source.backup(destination)
    require(scalar(backup, "PRAGMA integrity_check") == "ok", "backup invalid")
    require(
        REQUIRED_TABLES
        <= {
            row[0]
            for row in rows(backup, "SELECT name FROM sqlite_master WHERE type='table'")
        },
        "backup incomplete",
    )


def atomic_start(workspace: Path) -> None:
    new_case(workspace)
    start(workspace, "committed")
    start(workspace, "committed")
    require(
        count(live_db(workspace), "runs", "run_id='committed'") == 1,
        "run absent or duplicated",
    )
    require(
        count(
            live_db(workspace),
            "workflow_status",
            "workflow_uuid=?",
            (dbos_workflow_id_for(RunId("committed")),),
        )
        == 1,
        "enqueue absent or duplicated",
    )


def datasource_recovery(workspace: Path) -> None:
    new_case(workspace)
    path = live_db(workspace)
    start(workspace, "inside", "IN_TX")
    run(workspace, "inside", code=CRASHED, wait=5)
    require(run_state(workspace, "inside") == "STARTED", "transaction leaked")
    require(
        count(
            path,
            "datasource_outputs",
            "workflow_id=?",
            (dbos_workflow_id_for(RunId("inside")),),
        )
        == 0,
        "checkpoint leaked",
    )
    run(workspace, "inside")
    completed(workspace, "inside")
    start(workspace, "lag", "DATASOURCE")
    run(workspace, "lag", fault="AFTER_DATASOURCE", code=CRASHED, wait=5)
    require(run_state(workspace, "lag") == "STARTED", "run binding changed early")
    require(
        count(
            path,
            "datasource_outputs",
            "workflow_id=?",
            (dbos_workflow_id_for(RunId("lag")),),
        )
        == 1,
        "datasource output absent",
    )
    require(
        count(
            path,
            "operation_outputs",
            "workflow_uuid=? AND function_name='checkpoint'",
            (dbos_workflow_id_for(RunId("lag")),),
        )
        == 0,
        "outer ledger did not lag",
    )
    require(
        rows(
            path,
            "SELECT status, output, error FROM workflow_status WHERE workflow_uuid=?",
            (dbos_workflow_id_for(RunId("lag")),),
        )
        == [("PENDING", None, None)],
        "workflow ledger did not lag",
    )
    run(workspace, "lag")
    completed(workspace, "lag")


def version_fence(workspace: Path) -> None:
    new_case(workspace)
    start(workspace, "versioned")
    path = live_db(workspace)
    require(REVISION != APP_A, "identities collapsed")
    require(
        scalar(
            path,
            "SELECT application_version FROM workflow_status WHERE workflow_uuid=?",
            (dbos_workflow_id_for(RunId("versioned")),),
        )
        == APP_A,
        "version absent",
    )
    run(workspace, "versioned", APP_B, wait=1.5)
    require(run_state(workspace, "versioned") == "STARTED", "B took A")
    run(workspace, "versioned", APP_A)
    completed(workspace, "versioned")


def effect_reconciliation(workspace: Path) -> None:
    new_case(workspace)
    start(workspace, "ambiguous", "C2")
    run(workspace, "ambiguous", code=CRASHED, wait=5)
    logical_key = key("ambiguous")
    require(effect_call_count(workspace, "ambiguous") == 1, "effect absent")
    require(
        scalar(
            live_db(workspace),
            "SELECT state FROM effect_intents WHERE logical_key=?",
            (logical_key,),
        )
        == "PREPARED",
        "intent not prepared",
    )
    run(workspace, "ambiguous")
    completed(workspace, "ambiguous")
    require(effect_call_count(workspace, "ambiguous") == 1, "effect replayed")
    existing = rows(
        live_db(workspace),
        "SELECT request_hash, revision, adapter_revision "
        "FROM effect_intents WHERE logical_key=?",
        (logical_key,),
    )[0]
    changed_bindings = (
        ("changed", REVISION, EFFECT_ADAPTER_REVISION),
        (REQUEST, "changed", EFFECT_ADAPTER_REVISION),
        (REQUEST, REVISION, "changed"),
    )
    for changed_request, changed_revision, changed_adapter in changed_bindings:
        try:
            validate_intent_binding(
                existing, changed_request, changed_revision, changed_adapter
            )
        except IntentMismatch:
            pass
        else:
            raise ProbeFailure("changed intent binding accepted")
    start(workspace, "unknown", mode="UNKNOWN")
    run(workspace, "unknown")
    run(workspace, "unknown", wait=0.3)
    unknown = key("unknown")
    require(run_state(workspace, "unknown") == "STARTED", "UNKNOWN advanced run")
    require(
        scalar(
            live_db(workspace),
            "SELECT state FROM effect_intents WHERE logical_key=?",
            (unknown,),
        )
        == "UNKNOWN_OUTCOME",
        "UNKNOWN intent not waiting",
    )
    require(
        count(
            live_db(workspace),
            "effect_receipts",
            "logical_key=?",
            (unknown,),
        )
        == 0,
        "UNKNOWN receipt",
    )
    require(effect_call_count(workspace, "unknown") == 0, "UNKNOWN executed")


def concurrent_recovery(workspace: Path) -> None:
    new_case(workspace)
    start(workspace, "concurrent", "C2")
    run(workspace, "concurrent", code=CRASHED, wait=5)
    command = internal_argv(
        workspace,
        "runtime",
        "concurrent",
        APP_A,
        "NONE",
        "8",
    )
    processes = [
        subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=16)
        require(process.returncode == 0, f"concurrent runtime: {stdout}/{stderr}")
    completed(workspace, "concurrent")
    logical_key = key("concurrent")
    require(effect_call_count(workspace, "concurrent") == 1, "duplicate effect")
    require(
        count(
            live_db(workspace),
            "effect_receipts",
            "logical_key=?",
            (logical_key,),
        )
        == 1,
        "duplicate receipt",
    )


def crash_boundaries(workspace: Path) -> None:
    new_case(workspace)
    start(workspace, "baseline")
    run(workspace, "baseline")
    baseline = completed(workspace, "baseline")
    for point in ("C1", "C2", "C3"):
        run_id = point.lower()
        start(workspace, run_id, point)
        run(workspace, run_id, code=CRASHED, wait=5)
        run(workspace, run_id)
        require(completed(workspace, run_id) == baseline, f"{point} hash drift")


ARTIFACT_PREFIXES = (
    "atelier2.sqlite",
    "external-effect.sqlite",
    "backup.sqlite",
    "crash-",
)


def clean(workspace: Path) -> None:
    for entry in tuple(workspace.iterdir()):
        if entry.is_file() and entry.name.startswith(ARTIFACT_PREFIXES):
            entry.unlink()


CRITERIA: dict[str, Callable[[Path], None]] = {
    "canonical_sqlite": canonical_sqlite,
    "atomic_start": atomic_start,
    "datasource_recovery": datasource_recovery,
    "version_fence": version_fence,
    "effect_reconciliation": effect_reconciliation,
    "concurrent_recovery": concurrent_recovery,
    "crash_boundaries": crash_boundaries,
}


def internal(arguments: list[str]) -> None:
    command, raw_workspace, *values = arguments
    workspace = Path(raw_workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    if command == "initialize":
        initialize(workspace)
    elif command == "seed":
        (run_id,) = values
        seed(workspace, run_id)
    else:
        run_id, version, fault, wait = values
        runtime(workspace, version, run_id, fault, float(wait))


def main() -> None:
    mode, name, raw_workspace = sys.argv[1:4]
    if mode == "internal":
        internal(sys.argv[2:])
        return
    require(mode == "criterion" and name in CRITERIA, "unknown criterion")
    workspace = Path(raw_workspace).resolve()
    try:
        CRITERIA[name](workspace)
    finally:
        clean(workspace)
    print(
        json.dumps(
            {
                "criterion": name,
                "decision": "PASS_DBOS",
                "passed": True,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
