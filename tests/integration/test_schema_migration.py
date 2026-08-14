from __future__ import annotations

import hashlib
import multiprocessing
import os
import sqlite3
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from itertools import repeat
from pathlib import Path
from threading import Barrier, Lock

import pytest
import sqlalchemy as sa
from sqlalchemy import event, exc
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256,
    PRODUCT_TABLE_NAMES,
    MigrationRequired,
    UnsupportedSchemaVersion,
    _product_schema_fingerprint,
    _product_schema_fingerprint_sha256,
    effect_intents,
    effect_receipts,
    initialize_schema,
    reconcile_commands,
    runs,
    workflow_revisions,
)

_V7_AGENT_CONFIGURATION_TABLE = """
CREATE TABLE agent_configuration_revisions (
    revision_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    auth_profile_revision_hash TEXT NOT NULL,
    executor_revision TEXT NOT NULL,
    PRIMARY KEY (revision_hash),
    UNIQUE (revision_hash, auth_profile_revision_hash, model, executor_revision),
    CHECK (length(revision_hash) = 64 AND revision_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(model) BETWEEN 1 AND 1024),
    CHECK (length(auth_profile_revision_hash) = 64
           AND auth_profile_revision_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(executor_revision) BETWEEN 1 AND 1024),
    FOREIGN KEY(auth_profile_revision_hash)
        REFERENCES auth_profile_revisions (revision_hash)
)
"""

_V7_AGENT_CONFIGURATION_TRIGGERS = """
CREATE TRIGGER agent_configuration_revisions_no_update
BEFORE UPDATE ON agent_configuration_revisions BEGIN
  SELECT RAISE(ABORT, 'agent configuration revisions are immutable');
END;
CREATE TRIGGER agent_configuration_revisions_no_delete
BEFORE DELETE ON agent_configuration_revisions BEGIN
  SELECT RAISE(ABORT, 'agent configuration revisions are immutable');
END;
"""


def _create_frozen_version_seven_database(database_path: Path) -> None:
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database_path, isolation_level=None) as connection:
        version = int(
            connection.execute(
                "SELECT version FROM atelier_schema_versions"
            ).fetchone()[0]
        )
        if version == 8:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA legacy_alter_table=ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "ALTER TABLE agent_configuration_revisions "
                    "RENAME TO agent_configuration_revisions_v8"
                )
                connection.execute(_V7_AGENT_CONFIGURATION_TABLE)
                connection.execute("DROP TABLE agent_configuration_revisions_v8")
                connection.executescript(_V7_AGENT_CONFIGURATION_TRIGGERS)
                connection.execute("UPDATE atelier_schema_versions SET version=7")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    with sqlite3.connect(database_path) as connection:
        assert (
            _product_schema_fingerprint_sha256(_product_schema_fingerprint(connection))
            == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[7]
        )


def _seed_every_version_seven_product_table(database_path: Path) -> None:
    revision_hash = "c" * 64
    auth_hash = "a" * 64
    configuration_hash = "b" * 64
    binding_hash = "d" * 64
    v2_output = b"v2-output\x00\xff"
    v2_output_hash = hashlib.sha256(v2_output).hexdigest()
    legacy_output = b"legacy-output\x00\xff"
    legacy_output_hash = hashlib.sha256(legacy_output).hexdigest()
    effect_request = b"effect-request\x00\xff"
    effect_request_hash = hashlib.sha256(effect_request).hexdigest()
    effect_result = b"effect-result\x00\xff"
    effect_result_hash = hashlib.sha256(effect_result).hexdigest()
    answer = b"answer\x00\xff"

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO workflow_revisions VALUES(?, ?)",
            (revision_hash, b"populated-v7-workflow"),
        )
        connection.execute(
            "INSERT INTO auth_profile_revisions VALUES(?, ?, ?, ?, ?)",
            (auth_hash, "max-primary", 7, "anthropic", "subscription"),
        )
        connection.execute(
            "INSERT INTO agent_configuration_revisions VALUES(?, ?, ?, ?)",
            (configuration_hash, "claude-opus", auth_hash, "claude-cli/v1"),
        )
        connection.execute(
            "INSERT INTO runs VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-v7",
                "bootstrap-v7",
                revision_hash,
                2,
                binding_hash,
                "build",
                "STARTED",
                1,
                1,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO run_agent_bindings VALUES(?, ?, ?, ?, ?)",
            (
                "run-v7",
                revision_hash,
                binding_hash,
                "builder",
                configuration_hash,
            ),
        )
        connection.execute(
            "INSERT INTO agent_receipts VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "1" * 64,
                "2" * 64,
                "run-v7",
                revision_hash,
                "legacy",
                "exact-output/v1",
                "legacy-operation",
                legacy_output,
                legacy_output_hash,
                "3" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO agent_receipts_v2 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "4" * 64,
                "5" * 64,
                "run-v7",
                revision_hash,
                "build",
                "builder",
                binding_hash,
                configuration_hash,
                auth_hash,
                "max-primary",
                7,
                "anthropic",
                "subscription",
                "claude-opus",
                "claude-cli/v1",
                "operation-v7",
                v2_output,
                v2_output_hash,
                "6" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO agent_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "7" * 64,
                "4" * 64,
                "5" * 64,
                "operation-v7",
                "run-v7",
                revision_hash,
                "build",
                1,
                "SUCCEEDED",
                2,
                "NONE",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "6" * 64,
            ),
        )
        connection.execute(
            "INSERT INTO effect_intents VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                "effect-v7",
                "run-v7",
                effect_request,
                effect_request_hash,
                revision_hash,
                "loopback/v1",
                "destination-v7",
                "operation-v7",
                "CONFIRMED",
                1,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO reconcile_commands VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "command-v7",
                "effect-v7",
                1,
                "AUTHORITATIVE_NOT_FOUND",
                "operator",
                "preserved evidence",
                None,
                None,
                None,
                "APPLIED",
            ),
        )
        connection.execute(
            "INSERT INTO effect_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "effect-v7",
                "run-v7",
                effect_request,
                effect_request_hash,
                revision_hash,
                "loopback/v1",
                "destination-v7",
                "operation-v7",
                "external-effect-v7",
                effect_result,
                effect_result_hash,
                "ADAPTER_EXECUTION",
                None,
            ),
        )
        connection.execute(
            "INSERT INTO run_events("
            "run_id,revision_hash,event_sequence,node_id,node_execution_id,"
            "event_kind,payload,payload_hash,receipt_logical_key,"
            "receipt_result_hash,event_hash,agent_attempt_id,attempt_ordinal,"
            "cancellation_command_id,replacement,cancellation_disposition,"
            "replacement_attempt_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-v7",
                revision_hash,
                1,
                "build",
                "4" * 64,
                "AGENT_COMPLETED",
                v2_output,
                v2_output_hash,
                None,
                None,
                "8" * 64,
                "7" * 64,
                1,
                None,
                None,
                None,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO wait_answers VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "run-v7",
                revision_hash,
                "wait",
                "9" * 64,
                answer,
                hashlib.sha256(answer).hexdigest(),
                "answer-workflow-v7",
                "PENDING",
                0,
            ),
        )


def _product_rows_snapshot(
    database_path: Path,
) -> dict[str, tuple[tuple[object, ...], ...]]:
    with sqlite3.connect(database_path) as connection:
        return {
            table_name: tuple(connection.execute(f'SELECT * FROM "{table_name}"'))
            for table_name in sorted(PRODUCT_TABLE_NAMES)
        }


def _create_populated_version_one_database(database_path: Path) -> None:
    document = b"workflow-v1"
    revision_hash = hashlib.sha256(document).hexdigest()
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
            CREATE TABLE workflow_revisions(
              revision_hash TEXT PRIMARY KEY,
              document BLOB NOT NULL
            );
            CREATE TABLE runs(
              run_id TEXT PRIMARY KEY,
              dbos_workflow_id TEXT UNIQUE NOT NULL,
              revision_hash TEXT NOT NULL REFERENCES workflow_revisions(revision_hash),
              state TEXT NOT NULL CHECK(state IN ('STARTED', 'COMPLETED'))
            );
            INSERT INTO atelier_schema_versions VALUES(1);
            """
        )
        connection.execute(
            "INSERT INTO workflow_revisions VALUES(?, ?)",
            (revision_hash, document),
        )
        connection.execute(
            "INSERT INTO runs VALUES(?, ?, ?, ?)",
            ("run-1", "workflow-1", revision_hash, "COMPLETED"),
        )


def _logical_dump(database_path: Path) -> tuple[str, ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(connection.iterdump())


def _file_snapshot(root: Path) -> dict[str, bytes]:
    return {
        entry.name: entry.read_bytes() for entry in root.iterdir() if entry.is_file()
    }


def test_populated_version_one_is_refused_without_logical_mutation(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_version_one_database(database_path)
    before_files = _file_snapshot(tmp_path)
    before_logical = _logical_dump(database_path)
    engine = create_canonical_engine(database_path)

    with pytest.raises(MigrationRequired, match="explicit offline migration"):
        initialize_schema(engine)

    engine.dispose()
    assert _file_snapshot(tmp_path) == before_files
    assert _logical_dump(database_path) == before_logical


def test_version_four_is_refused_without_changing_any_file_byte(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
            CREATE TABLE v4_durable_state(value BLOB NOT NULL);
            INSERT INTO atelier_schema_versions VALUES(4);
            INSERT INTO v4_durable_state VALUES(X'00FF');
            """
        )
    before_files = _file_snapshot(tmp_path)
    engine = create_canonical_engine(database_path)

    with pytest.raises(MigrationRequired, match="schema version 4"):
        initialize_schema(engine)

    engine.dispose()
    assert _file_snapshot(tmp_path) == before_files


@pytest.mark.parametrize(
    "schema_sql",
    [
        "CREATE TABLE unowned_state(value TEXT NOT NULL);",
        """
        CREATE TABLE atelier_schema_versions(version INTEGER NOT NULL);
        INSERT INTO atelier_schema_versions VALUES(1);
        INSERT INTO atelier_schema_versions VALUES(2);
        """,
        """
        CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
        INSERT INTO atelier_schema_versions VALUES(3);
        """,
        """
        CREATE TABLE atelier_schema_versions(version TEXT NOT NULL);
        INSERT INTO atelier_schema_versions VALUES('2');
        """,
    ],
    ids=[
        "missing-version-owner",
        "multiple-versions",
        "future-version",
        "malformed-version",
    ],
)
def test_unknown_schema_is_refused_without_logical_mutation(
    tmp_path: Path, schema_sql: str
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(schema_sql)
    before_files = _file_snapshot(tmp_path)
    before_logical = _logical_dump(database_path)
    engine = create_canonical_engine(database_path)

    with pytest.raises(UnsupportedSchemaVersion):
        initialize_schema(engine)

    engine.dispose()
    assert _file_snapshot(tmp_path) == before_files
    assert _logical_dump(database_path) == before_logical


def test_empty_database_creates_exact_v8_and_reopens(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)

    initialize_schema(engine)

    with sqlite3.connect(database_path) as connection:
        assert (
            _product_schema_fingerprint_sha256(_product_schema_fingerprint(connection))
            == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[8]
        )
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version FROM atelier_schema_versions"))
            == 8
        )
        assert set(sa.inspect(connection).get_table_names()) >= {
            effect_intents.name,
            effect_receipts.name,
            reconcile_commands.name,
        }
        assert {
            column["name"] for column in sa.inspect(connection).get_columns("runs")
        } >= {
            "run_id",
            "bootstrap_workflow_id",
            "revision_hash",
            "workflow_format_version",
            "agent_binding_set_hash",
            "current_node_id",
            "state",
            "state_version",
            "last_event_sequence",
            "terminal_hash",
        }
        assert "dbos_workflow_id" not in {
            column["name"] for column in sa.inspect(connection).get_columns("runs")
        }
    first_dump = _logical_dump(database_path)

    initialize_schema(engine)

    assert _logical_dump(database_path) == first_dump
    engine.dispose()


def test_populated_exact_v7_migrates_once_without_changing_existing_identity(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_frozen_version_seven_database(database_path)
    _seed_every_version_seven_product_table(database_path)
    before = _product_rows_snapshot(database_path)
    assert all(before[table_name] for table_name in PRODUCT_TABLE_NAMES)
    engine = create_canonical_engine(database_path)

    initialize_schema(engine)

    after = _product_rows_snapshot(database_path)
    assert after["atelier_schema_versions"] == ((8,),)
    assert (
        tuple(row[:4] for row in after["agent_configuration_revisions"])
        == (before["agent_configuration_revisions"])
    )
    assert all(
        row[4:] == (1, "headless") for row in after["agent_configuration_revisions"]
    )
    assert {
        table_name: rows
        for table_name, rows in after.items()
        if table_name
        not in {"atelier_schema_versions", "agent_configuration_revisions"}
    } == {
        table_name: rows
        for table_name, rows in before.items()
        if table_name
        not in {"atelier_schema_versions", "agent_configuration_revisions"}
    }
    with sqlite3.connect(database_path) as connection:
        assert (
            _product_schema_fingerprint_sha256(_product_schema_fingerprint(connection))
            == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[8]
        )
    first_v8_dump = _logical_dump(database_path)

    initialize_schema(engine)

    assert _logical_dump(database_path) == first_v8_dump
    engine.dispose()


def _crash_v7_migration_process(database_path: str, boundary: str) -> None:
    engine = create_canonical_engine(Path(database_path))

    def exit_after_boundary(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        normalized = " ".join(statement.split())
        reached = (
            boundary == "complete-table-replacement"
            and normalized.startswith(
                "CREATE TRIGGER agent_configuration_revisions_no_delete"
            )
        ) or (
            boundary == "schema-version-cas"
            and normalized.startswith("UPDATE atelier_schema_versions")
        )
        if reached:
            os._exit(86)

    event.listen(engine, "after_cursor_execute", exit_after_boundary)
    initialize_schema(engine)
    os._exit(87)


@pytest.mark.parametrize(
    "boundary",
    ("complete-table-replacement", "schema-version-cas"),
)
def test_process_death_during_v7_migration_restores_exact_v7_then_upgrades_once(
    tmp_path: Path, boundary: str
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_frozen_version_seven_database(database_path)
    before = _logical_dump(database_path)
    process = multiprocessing.get_context("spawn").Process(
        target=_crash_v7_migration_process,
        args=(str(database_path), boundary),
    )
    process.start()
    process.join(timeout=10)
    if process.is_alive():
        process.kill()
        process.join(timeout=5)
        pytest.fail(f"migration child did not reach {boundary}")
    exit_code = process.exitcode
    process.close()

    assert exit_code == 86
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert (
            _product_schema_fingerprint_sha256(_product_schema_fingerprint(connection))
            == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[7]
        )
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version FROM atelier_schema_versions"))
            == 8
        )
    engine.dispose()


def test_concurrent_v7_openers_run_one_migration_and_converge_on_exact_v8(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_frozen_version_seven_database(database_path)
    participants = 4
    barrier = Barrier(participants)
    count_lock = Lock()
    migration_count = 0

    def migrate(path: Path, rendezvous: Barrier) -> int:
        nonlocal migration_count
        engine = create_canonical_engine(path)

        def count_migration(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: object,
        ) -> None:
            nonlocal migration_count
            if statement.strip().startswith(
                "ALTER TABLE agent_configuration_revisions"
            ):
                with count_lock:
                    migration_count += 1

        event.listen(engine, "before_cursor_execute", count_migration)
        try:
            rendezvous.wait(timeout=5)
            initialize_schema(engine)
            with engine.connect() as connection:
                return int(
                    connection.scalar(
                        sa.text("SELECT version FROM atelier_schema_versions")
                    )
                )
        finally:
            engine.dispose()

    with ThreadPoolExecutor(max_workers=participants) as pool:
        versions = list(
            pool.map(
                migrate,
                repeat(database_path, participants),
                repeat(barrier, participants),
            )
        )

    assert versions == [8] * participants
    assert migration_count == 1


@pytest.mark.parametrize(
    "malformation",
    (
        "ALTER TABLE agent_configuration_revisions ADD COLUMN unexpected TEXT",
        "DROP TRIGGER agent_configuration_revisions_no_update",
    ),
    ids=("partial-table", "missing-trigger"),
)
def test_malformed_v7_refuses_without_mutation(
    tmp_path: Path, malformation: str
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_frozen_version_seven_database(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(malformation)
    before = _logical_dump(database_path)
    engine = create_canonical_engine(database_path)

    with pytest.raises(UnsupportedSchemaVersion, match="malformed v7"):
        initialize_schema(engine)

    engine.dispose()
    assert _logical_dump(database_path) == before


@pytest.mark.parametrize(
    ("revision_format_version", "requested_capability"),
    ((3, "headless"), (2, "unknown"), (1, "interactive")),
)
def test_v8_sql_rejects_unknown_or_incompatible_capability_contract(
    tmp_path: Path,
    revision_format_version: int,
    requested_capability: str,
) -> None:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO auth_profile_revisions VALUES(?, ?, ?, ?, ?)",
            ("a" * 64, "max", 1, "anthropic", "subscription"),
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO agent_configuration_revisions VALUES(?, ?, ?, ?, ?, ?)",
            (
                "b" * 64,
                "opus",
                "a" * 64,
                "claude-cli/v1",
                revision_format_version,
                requested_capability,
            ),
        )
    engine.dispose()


def test_v6_requires_nonmutating_recreate(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
            CREATE TABLE durable_v6_witness(value BLOB NOT NULL);
            CREATE TRIGGER durable_v6_witness_no_delete
            BEFORE DELETE ON durable_v6_witness BEGIN
              SELECT RAISE(ABORT, 'immutable');
            END;
            INSERT INTO atelier_schema_versions VALUES(6);
            INSERT INTO durable_v6_witness VALUES(X'00FF');
            """
        )
    before_files = _file_snapshot(tmp_path)
    before_logical = _logical_dump(database_path)
    before_hash = hashlib.sha256(database_path.read_bytes()).hexdigest()
    engine = create_canonical_engine(database_path)

    with pytest.raises(MigrationRequired, match="schema version 6"):
        initialize_schema(engine)

    engine.dispose()
    assert _logical_dump(database_path) == before_logical
    assert _file_snapshot(tmp_path) == before_files
    assert hashlib.sha256(database_path.read_bytes()).hexdigest() == before_hash


def test_v8_preserves_both_legacy_event_guards_and_scopes_attempt_events(
    ledger_engine: Engine,
) -> None:
    with ledger_engine.begin() as connection:
        revision = str(
            connection.scalar(
                sa.select(runs.c.revision_hash).where(runs.c.run_id == "run-1")
            )
        )
        connection.execute(
            runs.insert().values(
                run_id="run-2",
                bootstrap_workflow_id="workflow-2",
                revision_hash=revision,
                workflow_format_version=1,
                agent_binding_set_hash=None,
                current_node_id="other",
                state="STARTED",
                state_version=0,
                last_event_sequence=0,
                terminal_hash=None,
            )
        )

    def insert_event(
        connection: Connection,
        *,
        run_id: str,
        sequence: int,
        node_id: str,
        execution_id: str,
        kind: str,
        attempt_id: str | None = None,
        ordinal: int | None = None,
    ) -> None:
        connection.exec_driver_sql(
            "INSERT INTO run_events("
            "run_id,revision_hash,event_sequence,node_id,node_execution_id,"
            "event_kind,payload,payload_hash,receipt_logical_key,"
            "receipt_result_hash,event_hash,agent_attempt_id,attempt_ordinal,"
            "cancellation_command_id,replacement,cancellation_disposition,"
            "replacement_attempt_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                revision,
                sequence,
                node_id,
                execution_id,
                kind,
                b"event",
                hashlib.sha256(b"event").hexdigest(),
                None,
                None,
                f"{sequence:x}" * 64,
                attempt_id,
                ordinal,
                None,
                None,
                None,
                None,
            ),
        )

    with ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-1",
            sequence=1,
            node_id="agent",
            execution_id="1" * 64,
            kind="AGENT_COMPLETED",
        )
    with pytest.raises(IntegrityError), ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-1",
            sequence=2,
            node_id="agent",
            execution_id="2" * 64,
            kind="AGENT_COMPLETED",
        )
    with pytest.raises(IntegrityError), ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-2",
            sequence=1,
            node_id="other",
            execution_id="1" * 64,
            kind="AGENT_COMPLETED",
        )

    with ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-1",
            sequence=2,
            node_id="agent",
            execution_id="1" * 64,
            kind="AGENT_FAILED",
            attempt_id="a" * 64,
            ordinal=1,
        )
        insert_event(
            connection,
            run_id="run-1",
            sequence=3,
            node_id="agent",
            execution_id="1" * 64,
            kind="AGENT_FAILED",
            attempt_id="b" * 64,
            ordinal=2,
        )
    with pytest.raises(IntegrityError), ledger_engine.begin() as connection:
        insert_event(
            connection,
            run_id="run-1",
            sequence=4,
            node_id="agent",
            execution_id="1" * 64,
            kind="AGENT_FAILED",
            attempt_id="a" * 64,
            ordinal=1,
        )


@pytest.fixture
def ledger_engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    revision_hash = hashlib.sha256(b"workflow-v1").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=revision_hash,
                document=b"workflow-v1",
            )
        )
        connection.execute(
            runs.insert().values(
                run_id="run-1",
                bootstrap_workflow_id="workflow-1",
                revision_hash=revision_hash,
                workflow_format_version=1,
                agent_binding_set_hash=None,
                current_node_id="agent",
                state="STARTED",
                state_version=0,
                last_event_sequence=0,
                terminal_hash=None,
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _add_intent(
    connection: Connection,
    *,
    logical_key: str = "effect-1",
    state: str = "PREPARED",
    state_version: int = 0,
    owner_command_id: str | None = None,
    canonical_request: bytes = b"request\x00\xff",
    overrides: Mapping[str, object] | None = None,
) -> None:
    values: dict[str, object] = {
        "logical_key": logical_key,
        "run_id": "run-1",
        "canonical_request": canonical_request,
        "request_hash": hashlib.sha256(canonical_request).hexdigest(),
        "workflow_revision_hash": hashlib.sha256(b"workflow-v1").hexdigest(),
        "adapter_revision": "adapter-1",
        "destination_identity": "destination-1",
        "adapter_operational_identity": "external-store-1",
        "state": state,
        "state_version": state_version,
        "reconciliation_owner_command_id": owner_command_id,
    }
    values.update(overrides or {})
    connection.execute(effect_intents.insert().values(values))


def _add_command(
    connection: Connection,
    *,
    command_id: str = "command-1",
    logical_key: str = "effect-1",
    determination: str = "AUTHORITATIVE_NOT_FOUND",
    found_effect_id: str | None = None,
    found_result: bytes | None = None,
    found_result_hash: str | None = None,
    state: str = "PENDING",
    overrides: Mapping[str, object] | None = None,
) -> None:
    values: dict[str, object] = {
        "command_id": command_id,
        "logical_key": logical_key,
        "expected_intent_version": 1,
        "determination": determination,
        "actor": "operator",
        "evidence": "authoritative adapter readback",
        "found_effect_id": found_effect_id,
        "found_result": found_result,
        "found_result_hash": found_result_hash,
        "state": state,
    }
    values.update(overrides or {})
    connection.execute(reconcile_commands.insert().values(values))


def _add_receipt(
    connection: Connection,
    *,
    logical_key: str = "effect-1",
    confirmation_source: str = "OPERATOR_AUTHORIZED_EXECUTION",
    command_id: str | None = "command-1",
    result: bytes = b"result\x00\xff",
    overrides: Mapping[str, object] | None = None,
) -> None:
    canonical_request = b"request\x00\xff"
    values: dict[str, object] = {
        "logical_key": logical_key,
        "run_id": "run-1",
        "canonical_request": canonical_request,
        "request_hash": hashlib.sha256(canonical_request).hexdigest(),
        "workflow_revision_hash": hashlib.sha256(b"workflow-v1").hexdigest(),
        "adapter_revision": "adapter-1",
        "destination_identity": "destination-1",
        "adapter_operational_identity": "external-store-1",
        "effect_id": "external-effect-1",
        "result": result,
        "result_hash": hashlib.sha256(result).hexdigest(),
        "confirmation_source": confirmation_source,
        "reconcile_command_id": command_id,
    }
    values.update(overrides or {})
    connection.execute(effect_receipts.insert().values(values))


@pytest.mark.parametrize(
    ("record", "field", "invalid_value"),
    [
        pytest.param("intent", "logical_key", "", id="intent-logical-key"),
        pytest.param("intent", "run_id", "", id="intent-run-id"),
        pytest.param("intent", "request_hash", "", id="intent-request-hash"),
        pytest.param(
            "intent",
            "workflow_revision_hash",
            "",
            id="intent-workflow-revision-hash",
        ),
        pytest.param("intent", "adapter_revision", "", id="intent-adapter-revision"),
        pytest.param(
            "intent",
            "destination_identity",
            "",
            id="intent-destination-identity",
        ),
        pytest.param(
            "intent",
            "adapter_operational_identity",
            "",
            id="intent-adapter-operational-identity",
        ),
        pytest.param(
            "intent",
            "reconciliation_owner_command_id",
            "",
            id="intent-reconciliation-owner-command-id",
        ),
        pytest.param("intent", "state_version", -1, id="intent-state-version"),
        pytest.param("command", "command_id", "", id="command-id"),
        pytest.param("command", "logical_key", "", id="command-logical-key"),
        pytest.param("command", "actor", "", id="command-actor"),
        pytest.param("command", "evidence", "", id="command-evidence"),
        pytest.param("command", "found_effect_id", "", id="command-found-effect-id"),
        pytest.param(
            "command", "found_result_hash", "", id="command-found-result-hash"
        ),
        pytest.param(
            "command",
            "expected_intent_version",
            -1,
            id="command-expected-intent-version",
        ),
        pytest.param("receipt", "logical_key", "", id="receipt-logical-key"),
        pytest.param("receipt", "run_id", "", id="receipt-run-id"),
        pytest.param("receipt", "request_hash", "", id="receipt-request-hash"),
        pytest.param(
            "receipt",
            "workflow_revision_hash",
            "",
            id="receipt-workflow-revision-hash",
        ),
        pytest.param("receipt", "adapter_revision", "", id="receipt-adapter-revision"),
        pytest.param(
            "receipt",
            "destination_identity",
            "",
            id="receipt-destination-identity",
        ),
        pytest.param(
            "receipt",
            "adapter_operational_identity",
            "",
            id="receipt-adapter-operational-identity",
        ),
        pytest.param("receipt", "effect_id", "", id="receipt-effect-id"),
        pytest.param("receipt", "result_hash", "", id="receipt-result-hash"),
        pytest.param(
            "receipt",
            "reconcile_command_id",
            "",
            id="receipt-reconcile-command-id",
        ),
    ],
)
def test_effect_ledger_rejects_invalid_field_shapes(
    ledger_engine: Engine,
    record: str,
    field: str,
    invalid_value: object,
) -> None:
    with (
        pytest.raises(exc.IntegrityError) as caught,
        ledger_engine.begin() as connection,
    ):
        if record == "intent":
            if field == "reconciliation_owner_command_id":
                _add_intent(connection, state="WAITING_RECONCILIATION")
                _add_command(connection)
                connection.execute(
                    effect_intents.update().values(
                        state="RECONCILING",
                        state_version=1,
                        reconciliation_owner_command_id=invalid_value,
                    )
                )
            else:
                _add_intent(connection, overrides={field: invalid_value})
        elif record == "command":
            _add_intent(connection)
            if field in {"found_effect_id", "found_result_hash"}:
                result = b"result\x00\xff"
                _add_command(
                    connection,
                    determination="FOUND",
                    found_effect_id="external-effect-1",
                    found_result=result,
                    found_result_hash=hashlib.sha256(result).hexdigest(),
                    overrides={field: invalid_value},
                )
            else:
                _add_command(connection, overrides={field: invalid_value})
        else:
            _add_intent(connection)
            _add_command(connection)
            _add_receipt(connection, overrides={field: invalid_value})

    assert "CHECK constraint failed" in str(caught.value.orig)


@pytest.mark.parametrize(
    ("state", "accepted"),
    [
        ("STARTED", True),
        ("WAITING_RECONCILIATION", True),
        ("WAITING_INPUT", True),
        ("COMPLETED", True),
        ("RECONCILING", False),
        ("PREPARED", False),
        ("CONFIRMED", False),
        ("", False),
    ],
)
def test_run_state_tokens_are_exact(
    ledger_engine: Engine, state: str, accepted: bool
) -> None:
    statement = runs.insert().values(
        run_id="candidate-run",
        bootstrap_workflow_id="candidate-workflow",
        revision_hash=hashlib.sha256(b"workflow-v1").hexdigest(),
        workflow_format_version=1,
        agent_binding_set_hash=None,
        current_node_id=("final" if state == "COMPLETED" else "node"),
        state=state,
        state_version=0,
        last_event_sequence=0,
        terminal_hash=("0" * 64 if state == "COMPLETED" else None),
    )
    if accepted:
        with ledger_engine.begin() as connection:
            connection.execute(statement)
    else:
        with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
            connection.execute(statement)


@pytest.mark.parametrize(
    ("state", "accepted"),
    [
        ("PREPARED", True),
        ("WAITING_RECONCILIATION", True),
        ("RECONCILING", True),
        ("CONFIRMED", True),
        ("STARTED", False),
        ("UNKNOWN_OUTCOME", False),
        ("", False),
    ],
)
def test_intent_state_tokens_are_exact(
    ledger_engine: Engine, state: str, accepted: bool
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)
        if state == "RECONCILING":
            _add_command(connection)
    if state == "PREPARED":
        return

    statement = effect_intents.update().values(
        state=state,
        reconciliation_owner_command_id=(
            "command-1" if state == "RECONCILING" else None
        ),
    )
    if accepted:
        with ledger_engine.begin() as connection:
            connection.execute(statement)
    else:
        with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
            connection.execute(statement)


@pytest.mark.parametrize(
    ("state", "accepted"),
    [
        ("PENDING", True),
        ("APPLIED", True),
        ("REJECTED_CONFLICT", True),
        ("COMPLETED", False),
        ("", False),
    ],
)
def test_reconcile_command_state_tokens_are_exact(
    ledger_engine: Engine, state: str, accepted: bool
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)
    if accepted:
        with ledger_engine.begin() as connection:
            _add_command(connection, state=state)
    else:
        with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
            _add_command(connection, state=state)


@pytest.mark.parametrize(
    (
        "determination",
        "found_effect_id",
        "found_result",
        "found_result_hash",
        "accepted",
    ),
    [
        ("FOUND", "external-1", b"", hashlib.sha256(b"").hexdigest(), True),
        ("FOUND", None, b"", hashlib.sha256(b"").hexdigest(), False),
        ("FOUND", "external-1", None, hashlib.sha256(b"").hexdigest(), False),
        ("FOUND", "external-1", b"", None, False),
        ("AUTHORITATIVE_NOT_FOUND", None, None, None, True),
        ("AUTHORITATIVE_NOT_FOUND", "external-1", None, None, False),
        ("AUTHORITATIVE_NOT_FOUND", None, b"", None, False),
        ("AUTHORITATIVE_NOT_FOUND", None, None, "hash", False),
        ("UNKNOWN", None, None, None, False),
    ],
)
def test_reconcile_determination_owns_the_found_payload_shape(
    ledger_engine: Engine,
    determination: str,
    found_effect_id: str | None,
    found_result: bytes | None,
    found_result_hash: str | None,
    accepted: bool,
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)

    if accepted:
        with ledger_engine.begin() as connection:
            _add_command(
                connection,
                determination=determination,
                found_effect_id=found_effect_id,
                found_result=found_result,
                found_result_hash=found_result_hash,
            )
    else:
        with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
            _add_command(
                connection,
                determination=determination,
                found_effect_id=found_effect_id,
                found_result=found_result,
                found_result_hash=found_result_hash,
            )


@pytest.mark.parametrize(
    ("source", "command_id", "accepted"),
    [
        ("ADAPTER_READBACK", None, True),
        ("ADAPTER_EXECUTION", None, True),
        ("OPERATOR_FOUND", "command-1", True),
        ("OPERATOR_AUTHORIZED_EXECUTION", "command-1", True),
        ("ADAPTER_READBACK", "command-1", False),
        ("OPERATOR_FOUND", None, False),
        ("UNKNOWN", None, False),
    ],
)
def test_confirmation_source_owns_command_provenance(
    ledger_engine: Engine, source: str, command_id: str | None, accepted: bool
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)
        if source == "OPERATOR_FOUND":
            result = b"result\x00\xff"
            _add_command(
                connection,
                determination="FOUND",
                found_effect_id="external-effect-1",
                found_result=result,
                found_result_hash=hashlib.sha256(result).hexdigest(),
            )
        else:
            _add_command(connection)

    if accepted:
        with ledger_engine.begin() as connection:
            _add_receipt(
                connection,
                confirmation_source=source,
                command_id=command_id,
            )
    else:
        with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
            _add_receipt(
                connection,
                confirmation_source=source,
                command_id=command_id,
            )


def test_receipt_round_trips_full_provenance_once_per_logical_key(
    ledger_engine: Engine,
) -> None:
    canonical_request = b"request\x00\xff"
    result = b"result\x00\xff"
    with ledger_engine.begin() as connection:
        _add_intent(connection, canonical_request=canonical_request)
        _add_command(connection)
        _add_receipt(connection, result=result)

    with ledger_engine.connect() as connection:
        assert connection.execute(sa.select(effect_receipts)).one() == (
            "effect-1",
            "run-1",
            canonical_request,
            hashlib.sha256(canonical_request).hexdigest(),
            hashlib.sha256(b"workflow-v1").hexdigest(),
            "adapter-1",
            "destination-1",
            "external-store-1",
            "external-effect-1",
            result,
            hashlib.sha256(result).hexdigest(),
            "OPERATOR_AUTHORIZED_EXECUTION",
            "command-1",
        )

    with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
        _add_receipt(connection, result=b"different")


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        pytest.param("logical_key", "effect-2", id="logical-key"),
        pytest.param("run_id", "run-2", id="run-id"),
        pytest.param("canonical_request", b"changed", id="canonical-request"),
        pytest.param("request_hash", "changed-hash", id="request-hash"),
        pytest.param(
            "workflow_revision_hash",
            hashlib.sha256(b"workflow-v2").hexdigest(),
            id="workflow-revision-hash",
        ),
        pytest.param("adapter_revision", "adapter-2", id="adapter-revision"),
        pytest.param(
            "destination_identity", "destination-2", id="destination-identity"
        ),
        pytest.param(
            "adapter_operational_identity",
            "external-store-2",
            id="adapter-operational-identity",
        ),
    ],
)
def test_every_effect_intent_binding_column_is_immutable(
    ledger_engine: Engine,
    column: str,
    replacement: object,
) -> None:
    alternate_document = b"workflow-v2"
    alternate_revision_hash = hashlib.sha256(alternate_document).hexdigest()
    with ledger_engine.begin() as connection:
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=alternate_revision_hash,
                document=alternate_document,
            )
        )
        connection.execute(
            runs.insert().values(
                run_id="run-2",
                bootstrap_workflow_id="workflow-2",
                revision_hash=alternate_revision_hash,
                workflow_format_version=1,
                agent_binding_set_hash=None,
                current_node_id="agent",
                state="STARTED",
                state_version=0,
                last_event_sequence=0,
                terminal_hash=None,
            )
        )
        _add_intent(connection)

    with (
        pytest.raises(exc.IntegrityError, match="effect intent bindings are immutable"),
        ledger_engine.begin() as connection,
    ):
        connection.execute(effect_intents.update().values({column: replacement}))


@pytest.mark.parametrize(
    ("column", "replacement"),
    [
        pytest.param("command_id", "command-2", id="command-id"),
        pytest.param("logical_key", "effect-2", id="logical-key"),
        pytest.param("expected_intent_version", 2, id="expected-intent-version"),
        pytest.param("determination", "FOUND", id="determination-update-attempt"),
        pytest.param("actor", "reviewer", id="actor"),
        pytest.param("evidence", "changed evidence", id="evidence"),
        pytest.param("found_effect_id", "external-effect-2", id="found-effect-id"),
        pytest.param("found_result", b"changed", id="found-result"),
        pytest.param("found_result_hash", "changed-hash", id="found-result-hash"),
    ],
)
def test_every_reconcile_command_identity_and_payload_column_is_immutable(
    ledger_engine: Engine,
    column: str,
    replacement: object,
) -> None:
    result = b"result\x00\xff"
    with ledger_engine.begin() as connection:
        _add_intent(connection)
        _add_intent(connection, logical_key="effect-2")
        _add_command(
            connection,
            determination="FOUND",
            found_effect_id="external-effect-1",
            found_result=result,
            found_result_hash=hashlib.sha256(result).hexdigest(),
        )

    with (
        pytest.raises(
            exc.IntegrityError, match="reconcile command payloads are immutable"
        ),
        ledger_engine.begin() as connection,
    ):
        connection.execute(reconcile_commands.update().values({column: replacement}))


@pytest.mark.parametrize(
    "operation",
    ["delete-intent", "change-receipt", "delete-receipt", "delete-command"],
)
def test_effect_evidence_rows_are_immutable(
    ledger_engine: Engine, operation: str
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)
        _add_command(connection)
        _add_receipt(connection)

    statements = {
        "delete-intent": effect_intents.delete(),
        "change-receipt": effect_receipts.update().values(result=b"changed"),
        "delete-receipt": effect_receipts.delete(),
        "delete-command": reconcile_commands.delete(),
    }
    with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
        connection.execute(statements[operation])


def test_effect_and_command_lifecycle_fields_remain_mutable(
    ledger_engine: Engine,
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection)
        connection.execute(
            effect_intents.update().values(
                state="WAITING_RECONCILIATION",
                state_version=1,
            )
        )
        _add_command(connection)
        connection.execute(
            effect_intents.update().values(
                state="RECONCILING",
                state_version=2,
                reconciliation_owner_command_id="command-1",
            )
        )
        connection.execute(reconcile_commands.update().values(state="APPLIED"))
        connection.execute(
            effect_intents.update().values(
                state="CONFIRMED",
                state_version=3,
                reconciliation_owner_command_id=None,
            )
        )

    with ledger_engine.connect() as connection:
        assert connection.execute(
            sa.select(effect_intents.c.state, effect_intents.c.state_version)
        ).one() == ("CONFIRMED", 3)
        assert connection.scalar(sa.select(reconcile_commands.c.state)) == "APPLIED"


def test_reconciliation_owner_must_reference_an_existing_command(
    ledger_engine: Engine,
) -> None:
    with ledger_engine.begin() as connection:
        _add_intent(connection, state="WAITING_RECONCILIATION")

    with pytest.raises(exc.IntegrityError), ledger_engine.begin() as connection:
        connection.execute(
            effect_intents.update()
            .where(effect_intents.c.logical_key == "effect-1")
            .values(
                state="RECONCILING",
                state_version=1,
                reconciliation_owner_command_id="missing-command",
            )
        )

    with ledger_engine.begin() as connection:
        _add_command(connection)
        connection.execute(
            effect_intents.update()
            .where(effect_intents.c.logical_key == "effect-1")
            .values(
                state="RECONCILING",
                state_version=1,
                reconciliation_owner_command_id="command-1",
            )
        )

    with ledger_engine.connect() as connection:
        assert connection.execute(
            sa.select(
                effect_intents.c.state,
                effect_intents.c.reconciliation_owner_command_id,
            )
        ).one() == ("RECONCILING", "command-1")
