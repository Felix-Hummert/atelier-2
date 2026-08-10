from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

SCHEMA_VERSION = 2

metadata = sa.MetaData()

atelier_schema_versions = sa.Table(
    "atelier_schema_versions",
    metadata,
    sa.Column("version", sa.Integer, primary_key=True),
)
workflow_revisions = sa.Table(
    "workflow_revisions",
    metadata,
    sa.Column("revision_hash", sa.Text, primary_key=True),
    sa.Column("document", sa.LargeBinary, nullable=False),
)
runs = sa.Table(
    "runs",
    metadata,
    sa.Column("run_id", sa.Text, primary_key=True),
    sa.Column("bootstrap_workflow_id", sa.Text, unique=True, nullable=False),
    sa.Column(
        "revision_hash",
        sa.Text,
        sa.ForeignKey("workflow_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("state", sa.Text, nullable=False),
    sa.CheckConstraint("state IN ('STARTED', 'WAITING_RECONCILIATION', 'COMPLETED')"),
)
effect_intents = sa.Table(
    "effect_intents",
    metadata,
    sa.Column("logical_key", sa.Text, primary_key=True),
    sa.Column("run_id", sa.Text, sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("canonical_request", sa.LargeBinary, nullable=False),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column(
        "workflow_revision_hash",
        sa.Text,
        sa.ForeignKey("workflow_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("adapter_revision", sa.Text, nullable=False),
    sa.Column("destination_identity", sa.Text, nullable=False),
    sa.Column("adapter_operational_identity", sa.Text, nullable=False),
    sa.Column("state", sa.Text, nullable=False),
    sa.Column("state_version", sa.Integer, nullable=False),
    sa.Column(
        "reconciliation_owner_command_id",
        sa.Text,
        sa.ForeignKey("reconcile_commands.command_id", ondelete="RESTRICT"),
        nullable=True,
    ),
    sa.CheckConstraint("length(logical_key) > 0"),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint("length(request_hash) > 0"),
    sa.CheckConstraint("length(workflow_revision_hash) > 0"),
    sa.CheckConstraint("length(adapter_revision) > 0"),
    sa.CheckConstraint("length(destination_identity) > 0"),
    sa.CheckConstraint("length(adapter_operational_identity) > 0"),
    sa.CheckConstraint(
        "state IN ('PREPARED', 'WAITING_RECONCILIATION', 'RECONCILING', 'CONFIRMED')"
    ),
    sa.CheckConstraint("state_version >= 0"),
    sa.CheckConstraint(
        "(state = 'RECONCILING' "
        "AND reconciliation_owner_command_id IS NOT NULL "
        "AND length(reconciliation_owner_command_id) > 0) "
        "OR (state <> 'RECONCILING' "
        "AND reconciliation_owner_command_id IS NULL)"
    ),
)
reconcile_commands = sa.Table(
    "reconcile_commands",
    metadata,
    sa.Column("command_id", sa.Text, primary_key=True),
    sa.Column(
        "logical_key",
        sa.Text,
        sa.ForeignKey("effect_intents.logical_key"),
        nullable=False,
    ),
    sa.Column("expected_intent_version", sa.Integer, nullable=False),
    sa.Column("determination", sa.Text, nullable=False),
    sa.Column("actor", sa.Text, nullable=False),
    sa.Column("evidence", sa.Text, nullable=False),
    sa.Column("found_effect_id", sa.Text, nullable=True),
    sa.Column("found_result", sa.LargeBinary, nullable=True),
    sa.Column("found_result_hash", sa.Text, nullable=True),
    sa.Column("state", sa.Text, nullable=False),
    sa.CheckConstraint("length(command_id) > 0"),
    sa.CheckConstraint("length(logical_key) > 0"),
    sa.CheckConstraint("expected_intent_version >= 0"),
    sa.CheckConstraint("determination IN ('FOUND', 'AUTHORITATIVE_NOT_FOUND')"),
    sa.CheckConstraint("length(actor) > 0"),
    sa.CheckConstraint("length(evidence) > 0"),
    sa.CheckConstraint(
        "(determination = 'FOUND' "
        "AND found_effect_id IS NOT NULL AND length(found_effect_id) > 0 "
        "AND found_result IS NOT NULL "
        "AND found_result_hash IS NOT NULL AND length(found_result_hash) > 0) "
        "OR (determination = 'AUTHORITATIVE_NOT_FOUND' "
        "AND found_effect_id IS NULL "
        "AND found_result IS NULL "
        "AND found_result_hash IS NULL)"
    ),
    sa.CheckConstraint("state IN ('PENDING', 'APPLIED', 'REJECTED_CONFLICT')"),
)
effect_receipts = sa.Table(
    "effect_receipts",
    metadata,
    sa.Column(
        "logical_key",
        sa.Text,
        sa.ForeignKey("effect_intents.logical_key"),
        primary_key=True,
    ),
    sa.Column("run_id", sa.Text, sa.ForeignKey("runs.run_id"), nullable=False),
    sa.Column("canonical_request", sa.LargeBinary, nullable=False),
    sa.Column("request_hash", sa.Text, nullable=False),
    sa.Column(
        "workflow_revision_hash",
        sa.Text,
        sa.ForeignKey("workflow_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("adapter_revision", sa.Text, nullable=False),
    sa.Column("destination_identity", sa.Text, nullable=False),
    sa.Column("adapter_operational_identity", sa.Text, nullable=False),
    sa.Column("effect_id", sa.Text, nullable=False),
    sa.Column("result", sa.LargeBinary, nullable=False),
    sa.Column("result_hash", sa.Text, nullable=False),
    sa.Column("confirmation_source", sa.Text, nullable=False),
    sa.Column(
        "reconcile_command_id",
        sa.Text,
        sa.ForeignKey("reconcile_commands.command_id"),
        nullable=True,
    ),
    sa.CheckConstraint("length(logical_key) > 0"),
    sa.CheckConstraint("length(run_id) > 0"),
    sa.CheckConstraint("length(request_hash) > 0"),
    sa.CheckConstraint("length(workflow_revision_hash) > 0"),
    sa.CheckConstraint("length(adapter_revision) > 0"),
    sa.CheckConstraint("length(destination_identity) > 0"),
    sa.CheckConstraint("length(adapter_operational_identity) > 0"),
    sa.CheckConstraint("length(effect_id) > 0"),
    sa.CheckConstraint("length(result_hash) > 0"),
    sa.CheckConstraint(
        "confirmation_source IN "
        "('ADAPTER_READBACK', 'ADAPTER_EXECUTION', "
        "'OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION')"
    ),
    sa.CheckConstraint(
        "(confirmation_source IN ('ADAPTER_READBACK', 'ADAPTER_EXECUTION') "
        "AND reconcile_command_id IS NULL) "
        "OR (confirmation_source IN "
        "('OPERATOR_FOUND', 'OPERATOR_AUTHORIZED_EXECUTION') "
        "AND reconcile_command_id IS NOT NULL "
        "AND length(reconcile_command_id) > 0)"
    ),
)

_IMMUTABLE_REVISION_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS workflow_revisions_no_update
    BEFORE UPDATE ON workflow_revisions
    BEGIN
      SELECT RAISE(ABORT, 'workflow revisions are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS workflow_revisions_no_delete
    BEFORE DELETE ON workflow_revisions
    BEGIN
      SELECT RAISE(ABORT, 'workflow revisions are immutable');
    END
    """,
)

_EFFECT_LEDGER_TRIGGERS = (
    """
    CREATE TRIGGER IF NOT EXISTS effect_intents_binding_no_update
    BEFORE UPDATE OF logical_key, run_id, canonical_request, request_hash,
                     workflow_revision_hash, adapter_revision, destination_identity,
                     adapter_operational_identity
    ON effect_intents
    BEGIN
      SELECT RAISE(ABORT, 'effect intent bindings are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS effect_intents_no_delete
    BEFORE DELETE ON effect_intents
    BEGIN
      SELECT RAISE(ABORT, 'effect intents are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS effect_receipts_no_update
    BEFORE UPDATE ON effect_receipts
    BEGIN
      SELECT RAISE(ABORT, 'effect receipts are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS effect_receipts_no_delete
    BEFORE DELETE ON effect_receipts
    BEGIN
      SELECT RAISE(ABORT, 'effect receipts are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS reconcile_commands_payload_no_update
    BEFORE UPDATE OF command_id, logical_key, expected_intent_version,
                     determination, actor, evidence, found_effect_id,
                     found_result, found_result_hash
    ON reconcile_commands
    BEGIN
      SELECT RAISE(ABORT, 'reconcile command payloads are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS reconcile_commands_no_delete
    BEFORE DELETE ON reconcile_commands
    BEGIN
      SELECT RAISE(ABORT, 'reconcile commands are immutable');
    END
    """,
)


class UnsupportedSchemaVersion(RuntimeError):
    def __init__(self, actual: object) -> None:
        super().__init__(
            f"Atelier schema version {actual!r} is unsupported; expected {SCHEMA_VERSION}"
        )


class MigrationRequired(UnsupportedSchemaVersion):
    def __init__(self) -> None:
        RuntimeError.__init__(
            self,
            "Atelier schema version 1 requires an explicit offline migration; "
            "runtime startup will not alter it",
        )


def _require_supported_versions(versions: Sequence[int]) -> None:
    normalized = tuple(versions)
    if normalized == (1,):
        raise MigrationRequired
    if normalized != (SCHEMA_VERSION,):
        raise UnsupportedSchemaVersion(normalized)


def _preflight_existing_schema(engine: Engine) -> None:
    """Refuse incompatible disk state before configured connections can mutate it."""

    raw_database_path = engine.url.database
    if engine.url.get_backend_name() != "sqlite" or raw_database_path is None:
        return
    if raw_database_path in {"", ":memory:"}:
        return
    database_path = Path(raw_database_path).resolve()
    if not database_path.is_file() or database_path.stat().st_size == 0:
        return
    try:
        with sqlite3.connect(
            f"{database_path.as_uri()}?mode=ro", uri=True
        ) as connection:
            table_names = {
                str(record[0])
                for record in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if not table_names:
                return
            if atelier_schema_versions.name not in table_names:
                raise UnsupportedSchemaVersion(
                    f"missing version owner beside tables {tuple(sorted(table_names))!r}"
                )
            versions = []
            for record in connection.execute(
                "SELECT version FROM atelier_schema_versions"
            ):
                version = record[0]
                if not isinstance(version, int):
                    raise TypeError("schema version must be stored as an integer")
                versions.append(version)
    except (sqlite3.DatabaseError, TypeError, ValueError) as error:
        raise UnsupportedSchemaVersion("unreadable schema version owner") from error
    _require_supported_versions(versions)


def initialize_schema(engine: Engine) -> None:
    _preflight_existing_schema(engine)
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            inspector = sa.inspect(connection)
            if not inspector.has_table(atelier_schema_versions.name):
                existing_tables = inspector.get_table_names()
                if existing_tables:
                    raise UnsupportedSchemaVersion(
                        f"missing version owner beside tables {tuple(existing_tables)!r}"
                    )
                metadata.create_all(connection)
                connection.execute(
                    atelier_schema_versions.insert().values(version=SCHEMA_VERSION)
                )
                for statement in (
                    *_IMMUTABLE_REVISION_TRIGGERS,
                    *_EFFECT_LEDGER_TRIGGERS,
                ):
                    connection.execute(sa.text(statement))

            versions = (
                connection.execute(sa.select(atelier_schema_versions.c.version))
                .scalars()
                .all()
            )
            _require_supported_versions(versions)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
