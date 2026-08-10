from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.engine import Engine

SCHEMA_VERSION = 1

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
    sa.Column("dbos_workflow_id", sa.Text, unique=True, nullable=False),
    sa.Column(
        "revision_hash",
        sa.Text,
        sa.ForeignKey("workflow_revisions.revision_hash"),
        nullable=False,
    ),
    sa.Column("state", sa.Text, nullable=False),
    sa.CheckConstraint("state IN ('STARTED', 'COMPLETED')"),
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


class UnsupportedSchemaVersion(RuntimeError):
    def __init__(self, actual: object) -> None:
        super().__init__(
            f"Atelier schema version {actual!r} is unsupported; expected {SCHEMA_VERSION}"
        )


def initialize_schema(engine: Engine) -> None:
    with engine.connect() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            inspector = sa.inspect(connection)
            if not inspector.has_table(atelier_schema_versions.name):
                metadata.create_all(connection)
                connection.execute(
                    atelier_schema_versions.insert().values(version=SCHEMA_VERSION)
                )
                for statement in _IMMUTABLE_REVISION_TRIGGERS:
                    connection.execute(sa.text(statement))

            versions = (
                connection.execute(sa.select(atelier_schema_versions.c.version))
                .scalars()
                .all()
            )
            if versions != [SCHEMA_VERSION]:
                raise UnsupportedSchemaVersion(tuple(versions))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
