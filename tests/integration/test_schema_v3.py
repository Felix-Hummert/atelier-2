from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    PRODUCT_TABLE_NAMES,
    UnsupportedSchemaVersion,
    initialize_schema,
)


def snapshot(database: Path) -> tuple[bytes, tuple[tuple[str, str, str], ...]]:
    with sqlite3.connect(database) as connection:
        schema = tuple(
            connection.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY type, name"
            )
        )
    return database.read_bytes(), schema


def rows_snapshot(
    database: Path,
) -> tuple[tuple[str, tuple[tuple[object, ...], ...]], ...]:
    with sqlite3.connect(database) as connection:
        tables = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        )
        return tuple(
            (
                table,
                tuple(
                    sorted(
                        connection.execute(f'SELECT * FROM "{table}"').fetchall(),
                        key=repr,
                    )
                ),
            )
            for table in tables
        )


def engine_snapshot(
    engine: sa.Engine,
) -> tuple[
    tuple[tuple[str, ...], ...],
    tuple[tuple[str, tuple[tuple[object, ...], ...]], ...],
]:
    with engine.connect() as connection:
        schema = tuple(
            tuple(str(value) for value in row)
            for row in connection.execute(
                sa.text(
                    "SELECT type,name,sql FROM sqlite_master "
                    "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
                )
            )
        )
        table_names = tuple(
            str(row[0])
            for row in connection.execute(
                sa.text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            )
        )
        rows = tuple(
            (
                table_name,
                tuple(
                    sorted(
                        (
                            tuple(row)
                            for row in connection.exec_driver_sql(
                                f'SELECT * FROM "{table_name}"'
                            )
                        ),
                        key=repr,
                    )
                ),
            )
            for table_name in table_names
        )
    return schema, rows


def test_fresh_v3_has_the_closed_product_tables_and_reopens_idempotently(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    try:
        initialize_schema(engine)
        initialize_schema(engine)
        with engine.connect() as connection:
            assert connection.execute(
                sa.text("SELECT version FROM atelier_schema_versions")
            ).all() == [(3,)]
            assert PRODUCT_TABLE_NAMES.issubset(
                sa.inspect(connection).get_table_names()
            )
    finally:
        engine.dispose()


def test_malformed_v3_is_refused_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY)"
        )
        connection.execute("INSERT INTO atelier_schema_versions VALUES(3)")
        connection.execute("CREATE TABLE workflow_revisions(wrong TEXT)")
    before = snapshot(database)
    engine = sa.create_engine(f"sqlite:///{database}")

    with pytest.raises(UnsupportedSchemaVersion, match="malformed v3"):
        initialize_schema(engine)

    engine.dispose()
    assert snapshot(database) == before


@pytest.mark.parametrize(
    "malformation",
    [
        "no-op-trigger",
        "trigger-literal-spacing",
        "loosened-check",
        "changed-type",
        "changed-nullability",
    ],
)
def test_existing_v3_rejects_every_product_schema_drift_without_mutation(
    tmp_path: Path, malformation: str
) -> None:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    engine.dispose()

    with sqlite3.connect(database) as connection:
        if malformation in {"no-op-trigger", "trigger-literal-spacing"}:
            trigger_body = (
                "SELECT 1"
                if malformation == "no-op-trigger"
                else "SELECT RAISE(ABORT, 'run  events are immutable')"
            )
            connection.executescript(
                f"""
                DROP TRIGGER run_events_no_update;
                CREATE TRIGGER run_events_no_update
                BEFORE UPDATE ON run_events BEGIN {trigger_body}; END;
                """
            )
        else:
            original = str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='runs'"
                ).fetchone()[0]
            )
            if malformation == "loosened-check":
                changed = original.replace("state_version >= 0", "state_version >= -1")
            elif malformation == "changed-type":
                changed = original.replace(
                    "current_node_id TEXT NOT NULL", "current_node_id BLOB NOT NULL"
                )
            else:
                changed = original.replace(
                    "current_node_id TEXT NOT NULL", "current_node_id TEXT"
                )
            assert changed != original
            connection.execute("PRAGMA writable_schema=ON")
            connection.execute(
                "UPDATE sqlite_master SET sql=? WHERE type='table' AND name='runs'",
                (changed,),
            )
            connection.execute("PRAGMA writable_schema=OFF")

    before_schema = snapshot(database)
    before_rows = rows_snapshot(database)
    reopened = sa.create_engine(f"sqlite:///{database}")
    with pytest.raises(UnsupportedSchemaVersion, match="malformed v3"):
        initialize_schema(reopened)
    reopened.dispose()

    assert snapshot(database) == before_schema
    assert rows_snapshot(database) == before_rows


def test_existing_malformed_in_memory_v3_is_refused() -> None:
    engine = sa.create_engine("sqlite://")
    initialize_schema(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TRIGGER run_events_no_update"))
        connection.execute(
            sa.text(
                "CREATE TRIGGER run_events_no_update "
                "BEFORE UPDATE ON run_events BEGIN SELECT 1; END"
            )
        )

    with pytest.raises(UnsupportedSchemaVersion, match="malformed v3"):
        initialize_schema(engine)
    engine.dispose()


def test_nonempty_dbos_only_in_memory_database_is_not_treated_as_fresh() -> None:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE workflow_status("
                "workflow_uuid TEXT PRIMARY KEY, name TEXT NOT NULL)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO workflow_status VALUES('existing-run','dbos')")
        )
    before = engine_snapshot(engine)

    with pytest.raises(UnsupportedSchemaVersion, match="missing version owner"):
        initialize_schema(engine)

    assert engine_snapshot(engine) == before
    engine.dispose()


def test_dbos_owned_tables_are_allowed_and_unchanged_by_v3_preflight(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    engine.dispose()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE dbos_owned(id INTEGER PRIMARY KEY, value TEXT)"
        )
        connection.execute("INSERT INTO dbos_owned VALUES(7, 'kept')")
    before_row = None
    before_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    with sqlite3.connect(database) as connection:
        before_row = connection.execute("SELECT * FROM dbos_owned").fetchall()

    reopened = sa.create_engine(f"sqlite:///{database}")
    initialize_schema(reopened)
    reopened.dispose()

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT * FROM dbos_owned").fetchall() == before_row
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before_hash


def test_run_event_answer_and_action_receipt_bindings_are_immutable_and_composite(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    revision = "1" * 64
    request_hash = hashlib.sha256(b"draft-17").hexdigest()
    result_hash = hashlib.sha256(b"draft-17").hexdigest()
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO workflow_revisions VALUES(:revision, :document)"),
            {"revision": revision, "document": b"graph"},
        )
        connection.execute(
            sa.text(
                "INSERT INTO runs VALUES "
                "('run-1','bootstrap',:revision,'action','STARTED',0,0,NULL)"
            ),
            {"revision": revision},
        )
        connection.execute(
            sa.text(
                "INSERT INTO effect_intents VALUES "
                "('key','run-1',:request,:request_hash,:revision,'adapter',"
                "'destination','operation','CONFIRMED',1,NULL)"
            ),
            {
                "request": b"draft-17",
                "request_hash": request_hash,
                "revision": revision,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO effect_receipts VALUES "
                "('key','run-1',:request,:request_hash,:revision,'adapter',"
                "'destination','operation','effect',:result,:result_hash,"
                "'ADAPTER_EXECUTION',NULL)"
            ),
            {
                "request": b"draft-17",
                "request_hash": request_hash,
                "revision": revision,
                "result": b"draft-17",
                "result_hash": result_hash,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO run_events VALUES "
                "('run-1',:revision,1,'action',:node,'ACTION_COMPLETED',"
                ":payload,:payload_hash,'key',:result_hash,:event_hash)"
            ),
            {
                "revision": revision,
                "node": "2" * 64,
                "payload": b"draft-17",
                "payload_hash": result_hash,
                "result_hash": result_hash,
                "event_hash": "3" * 64,
            },
        )
        connection.execute(
            sa.text(
                "INSERT INTO wait_answers VALUES "
                "('run-1',:revision,'wait',:node,:answer,:answer_hash,"
                "'answer-workflow','PENDING',0)"
            ),
            {
                "revision": revision,
                "node": "4" * 64,
                "answer": b"5",
                "answer_hash": hashlib.sha256(b"5").hexdigest(),
            },
        )

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(sa.text("UPDATE run_events SET payload=X'00'"))
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM wait_answers"))
    engine.dispose()


def test_run_event_schema_receipt_binding_matrix(tmp_path: Path) -> None:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    revision = "1" * 64
    result = b"result"
    result_hash = hashlib.sha256(result).hexdigest()
    different_payload = b"different-result"
    different_payload_hash = hashlib.sha256(different_payload).hexdigest()
    receipt_kinds = ("ACTION_RECONCILIATION_RESOLVED", "ACTION_COMPLETED")
    nonreceipt_kinds = (
        "AGENT_COMPLETED",
        "ACTION_RECONCILIATION_REQUIRED",
        "WAITING_INPUT",
        "WAIT_ANSWERED",
        "SUBWORKFLOW_COMPLETED",
    )
    missing_receipt_bindings = (
        (None, None),
        ("key", None),
        (None, result_hash),
    )
    forbidden_nonreceipt_bindings = (
        ("key", None),
        (None, result_hash),
        ("key", result_hash),
    )
    with engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO workflow_revisions VALUES(:revision,:document)"),
            {"revision": revision, "document": b"graph"},
        )
        connection.execute(
            sa.text(
                "INSERT INTO runs VALUES "
                "('run-1','bootstrap',:revision,'action','STARTED',0,0,NULL)"
            ),
            {"revision": revision},
        )
        connection.execute(
            sa.text(
                "INSERT INTO effect_intents VALUES "
                "('key','run-1',:result,:result_hash,:revision,'adapter',"
                "'destination','operation','CONFIRMED',1,NULL)"
            ),
            {"result": result, "result_hash": result_hash, "revision": revision},
        )
        connection.execute(
            sa.text(
                "INSERT INTO effect_receipts VALUES "
                "('key','run-1',:result,:result_hash,:revision,'adapter',"
                "'destination','operation','effect',:result,:result_hash,"
                "'ADAPTER_EXECUTION',NULL)"
            ),
            {"result": result, "result_hash": result_hash, "revision": revision},
        )

        for sequence, event_kind in enumerate(receipt_kinds, start=1):
            connection.execute(
                sa.text(
                    "INSERT INTO run_events VALUES "
                    "('run-1',:revision,:sequence,'action',:node,:event_kind,"
                    ":payload,:payload_hash,'key',:result_hash,:event_hash)"
                ),
                {
                    "revision": revision,
                    "sequence": sequence,
                    "node": "2" * 64,
                    "event_kind": event_kind,
                    "payload": result,
                    "payload_hash": result_hash,
                    "result_hash": result_hash,
                    "event_hash": str(sequence + 2) * 64,
                },
            )

    invalid_scenarios = (
        tuple(
            (
                event_kind,
                result,
                result_hash,
                receipt_logical_key,
                receipt_result_hash,
            )
            for event_kind in receipt_kinds
            for receipt_logical_key, receipt_result_hash in missing_receipt_bindings
        )
        + tuple(
            (event_kind, different_payload, different_payload_hash, "key", result_hash)
            for event_kind in receipt_kinds
        )
        + tuple(
            (
                event_kind,
                result,
                result_hash,
                receipt_logical_key,
                receipt_result_hash,
            )
            for event_kind in nonreceipt_kinds
            for receipt_logical_key, receipt_result_hash in forbidden_nonreceipt_bindings
        )
    )
    before = rows_snapshot(database)
    for (
        event_kind,
        payload,
        payload_hash,
        receipt_logical_key,
        receipt_result_hash,
    ) in invalid_scenarios:
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO run_events VALUES "
                    "('run-1',:revision,3,'node',:node,:event_kind,"
                    ":payload,:payload_hash,:receipt_logical_key,"
                    ":receipt_result_hash,:event_hash)"
                ),
                {
                    "revision": revision,
                    "node": "4" * 64,
                    "event_kind": event_kind,
                    "payload": payload,
                    "payload_hash": payload_hash,
                    "receipt_logical_key": receipt_logical_key,
                    "receipt_result_hash": receipt_result_hash,
                    "event_hash": "5" * 64,
                },
            )
        assert rows_snapshot(database) == before
    engine.dispose()


def test_composite_foreign_keys_reject_individually_valid_cross_bindings(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    revisions = ("1" * 64, "2" * 64)
    payload = b"draft-17"
    payload_hash = hashlib.sha256(payload).hexdigest()
    with engine.begin() as connection:
        for index, revision in enumerate(revisions, start=1):
            connection.execute(
                sa.text("INSERT INTO workflow_revisions VALUES(:revision,:document)"),
                {"revision": revision, "document": f"graph-{index}".encode()},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO runs VALUES "
                    "(:run,:bootstrap,:revision,'action','STARTED',0,0,NULL)"
                ),
                {
                    "run": f"run-{index}",
                    "bootstrap": f"bootstrap-{index}",
                    "revision": revision,
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO effect_intents VALUES "
                    "(:key,:run,:payload,:payload_hash,:revision,'adapter',"
                    "'destination','operation','CONFIRMED',1,NULL)"
                ),
                {
                    "key": f"key-{index}",
                    "run": f"run-{index}",
                    "payload": payload,
                    "payload_hash": payload_hash,
                    "revision": revision,
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO effect_receipts VALUES "
                    "(:key,:run,:payload,:payload_hash,:revision,'adapter',"
                    "'destination','operation',:effect,:payload,:payload_hash,"
                    "'ADAPTER_EXECUTION',NULL)"
                ),
                {
                    "key": f"key-{index}",
                    "run": f"run-{index}",
                    "payload": payload,
                    "payload_hash": payload_hash,
                    "revision": revision,
                    "effect": f"effect-{index}",
                },
            )
        connection.execute(
            sa.text(
                "INSERT INTO effect_intents VALUES "
                "('key-3','run-1',:payload,:payload_hash,:revision,'adapter',"
                "'destination','operation','CONFIRMED',1,NULL)"
            ),
            {
                "payload": payload,
                "payload_hash": payload_hash,
                "revision": revisions[0],
            },
        )

    expected_rows = rows_snapshot(database)
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO run_events VALUES "
                "('run-2',:revision,1,'action',:node,'ACTION_COMPLETED',"
                ":payload,:payload_hash,'key-1',:payload_hash,:event_hash)"
            ),
            {
                "revision": revisions[1],
                "node": "3" * 64,
                "payload": payload,
                "payload_hash": payload_hash,
                "event_hash": "4" * 64,
            },
        )
    assert rows_snapshot(database) == expected_rows

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO effect_intents VALUES "
                "('wrong-revision','run-2',:payload,:payload_hash,:revision,'adapter',"
                "'destination','operation','PREPARED',0,NULL)"
            ),
            {
                "payload": payload,
                "payload_hash": payload_hash,
                "revision": revisions[0],
            },
        )
    assert rows_snapshot(database) == expected_rows

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO effect_receipts VALUES "
                "('key-3','run-2',:payload,:payload_hash,:revision,'adapter',"
                "'destination','operation','effect-3',:payload,:payload_hash,"
                "'ADAPTER_EXECUTION',NULL)"
            ),
            {
                "payload": payload,
                "payload_hash": payload_hash,
                "revision": revisions[1],
            },
        )
    assert rows_snapshot(database) == expected_rows
    engine.dispose()
