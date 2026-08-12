from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy.engine import Connection, Engine

from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    MigrationRequired,
    UnsupportedSchemaVersion,
    effect_intents,
    effect_receipts,
    initialize_schema,
    reconcile_commands,
    runs,
    workflow_revisions,
)


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


def test_new_database_has_the_version_five_runtime_ledger(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)

    initialize_schema(engine)

    with engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version FROM atelier_schema_versions"))
            == 5
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
