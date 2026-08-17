"""The offline hop a live V13 store needed and did not have.

The V13 fixture is a real predecessor store: the current create path, every later
addition removed, then a format-3 run that already wrote one event. That is the
#240 Z2 method — predecessor schema, not a version-row stub — expressed through
today's owner.

V14 and V15 each added a table, so dropping those tables was the whole reversal.
V16 changes `run_events` itself, so the fixture also restores that table's
published predecessor shape below. The literal is not a second owner of the
current table: it is the frozen artifact V13 through V15 really carried, and the
pinned V13 fingerprint refuses it the moment a character drifts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateIndex

from atelier2.adapters.dbos.run_store import event_from_record
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    _PRODUCT_TRIGGERS,
    PRODUCT_SCHEMA_HANDOFF,
    SCHEMA_VERSION,
    V13_SCHEMA_HANDOFF,
    MigrationRequired,
    _require_product_shape,
    atelier_schema_versions,
    catalog_lineage_members,
    catalog_lineages,
    context_packages_v3,
    initialize_schema,
    node_execution_requests_v3,
    node_receipts_v3,
    published_revisions,
    run_configuration_revisions,
    run_events,
    run_inputs_v3,
    runs,
    tool_redemptions,
    workflow_revisions,
)
from atelier2.contracts.catalog_v3 import CatalogLineage
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.host import main

ARCHIVED_RUN_ID = "live/erster-lauf-nach-der-nacht"
ARCHIVED_NODE_ID = "cook"
ARCHIVED_OUTPUT = b"lasagne, aufgetragen"

_PREDECESSOR_RUN_EVENTS_DDL = """
CREATE TABLE run_events (
    run_id TEXT NOT NULL,
    revision_hash TEXT NOT NULL,
    event_sequence INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    node_execution_id TEXT NOT NULL,
    event_kind TEXT NOT NULL,
    payload BLOB NOT NULL,
    payload_hash TEXT NOT NULL,
    receipt_logical_key TEXT,
    receipt_result_hash TEXT,
    event_hash TEXT NOT NULL,
    agent_attempt_id TEXT,
    attempt_ordinal INTEGER,
    cancellation_command_id TEXT,
    replacement TEXT,
    cancellation_disposition TEXT,
    replacement_attempt_id TEXT,
    PRIMARY KEY (run_id, event_sequence),
    FOREIGN KEY(run_id, revision_hash) REFERENCES runs (run_id, revision_hash),
    FOREIGN KEY(receipt_logical_key, run_id, revision_hash, receipt_result_hash) REFERENCES effect_receipts (logical_key, run_id, workflow_revision_hash, result_hash),
    CHECK (event_sequence > 0),
    CHECK (length(node_id) > 0),
    CHECK (length(node_execution_id) = 64 AND node_execution_id NOT GLOB '*[^0-9a-f]*'),
    CHECK (event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED', 'AGENT_CANCEL_REQUESTED', 'AGENT_CANCELLED', 'AGENT_INTERRUPTED', 'ACTION_RECONCILIATION_REQUIRED', 'ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED', 'WAITING_INPUT', 'WAIT_ANSWERED', 'SUBWORKFLOW_COMPLETED')),
    CHECK (length(payload_hash) = 64 AND payload_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(event_hash) = 64 AND event_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK ((event_kind IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NOT NULL AND length(receipt_logical_key) > 0 AND receipt_result_hash IS NOT NULL AND length(receipt_result_hash) = 64 AND receipt_result_hash NOT GLOB '*[^0-9a-f]*' AND receipt_result_hash = payload_hash) OR (event_kind NOT IN ('ACTION_RECONCILIATION_RESOLVED', 'ACTION_COMPLETED') AND receipt_logical_key IS NULL AND receipt_result_hash IS NULL)),
    CHECK ((agent_attempt_id IS NULL AND attempt_ordinal IS NULL AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (length(agent_attempt_id) = 64 AND agent_attempt_id NOT GLOB '*[^0-9a-f]*' AND attempt_ordinal IN (1, 2) AND ((event_kind IN ('AGENT_COMPLETED', 'AGENT_FAILED') AND cancellation_command_id IS NULL AND replacement IS NULL AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind = 'AGENT_CANCEL_REQUESTED' AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NULL AND replacement_attempt_id IS NULL) OR (event_kind IN ('AGENT_CANCELLED', 'AGENT_INTERRUPTED') AND length(cancellation_command_id) BETWEEN 1 AND 1024 AND replacement IN ('NONE', 'ONE') AND cancellation_disposition IS NOT NULL))))
)
"""


def _logical_dump(database_path: Path) -> tuple[str, ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(connection.iterdump())


def _restore_predecessor_run_events(connection: Connection) -> None:
    triggers = ("run_events_no_update", "run_events_no_delete")
    indexes = sorted(run_events.indexes, key=lambda index: index.name or "")
    for trigger in triggers:
        connection.execute(sa.text(f"DROP TRIGGER {trigger}"))
    for index in indexes:
        connection.execute(sa.text(f"DROP INDEX {index.name}"))
    connection.execute(sa.text("DROP TABLE run_events"))
    connection.execute(sa.text(_PREDECESSOR_RUN_EVENTS_DDL))
    for index in indexes:
        connection.execute(CreateIndex(index))
    for trigger in triggers:
        connection.execute(sa.text(_PRODUCT_TRIGGERS[trigger]))


def _archived_completion(revision_hash: WorkflowRevisionHash) -> RunEvent:
    """The completion an old run really wrote: no attempt binding, no receipt."""
    run_id = RunId(ARCHIVED_RUN_ID)
    return RunEvent(
        run_id,
        revision_hash,
        1,
        ARCHIVED_NODE_ID,
        NodeExecutionId.for_node(run_id, revision_hash, ARCHIVED_NODE_ID),
        RunEventKind.AGENT_COMPLETED,
        ARCHIVED_OUTPUT,
    )


def _create_populated_v13_store(database_path: Path) -> None:
    """An exact V13 product store, not a version-row witness.

    A fresh store of the current schema with each later table and its triggers
    removed, and `run_events` restored to the shape it had before V16, is the
    published V13 shape. That is the same method as the #240 Z2 testimony
    (predecessor schema from before the V14 head), expressed through today's
    owner so the fixture cannot drift from the create path the hop will reopen.
    """

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    published = PublishedRevision(RevisionKind.WORKFLOW, b"name: lasagne\n")
    lineage = CatalogLineage(published.kind, published.revision_hash)
    configuration = "44" * 32
    package = "33" * 32
    request = "22" * 32
    execution = "11" * 32
    receipt = "ef" * 32
    with engine.connect() as connection:
        for table in (run_inputs_v3.name, tool_redemptions.name):
            connection.execute(sa.text(f"DROP TRIGGER {table}_no_update"))
            connection.execute(sa.text(f"DROP TRIGGER {table}_no_delete"))
            connection.execute(sa.text(f"DROP TABLE {table}"))
        _restore_predecessor_run_events(connection)
        connection.execute(
            atelier_schema_versions.update()
            .where(atelier_schema_versions.c.version == SCHEMA_VERSION)
            .values(version=V13_SCHEMA_HANDOFF.version)
        )
        connection.execute(
            published_revisions.insert().values(
                kind=published.kind.value,
                revision_hash=published.revision_hash.value,
                document=published.document,
            )
        )
        connection.execute(
            catalog_lineages.insert().values(
                lineage_id=lineage.lineage_id.value,
                kind=published.kind.value,
                founding_revision_hash=published.revision_hash.value,
            )
        )
        connection.execute(
            catalog_lineage_members.insert().values(
                lineage_id=lineage.lineage_id.value,
                revision_number=1,
                revision_hash=published.revision_hash.value,
            )
        )
        connection.execute(
            workflow_revisions.insert().values(
                revision_hash=published.revision_hash.value,
                document=published.document,
            )
        )
        connection.execute(
            run_configuration_revisions.insert().values(
                revision_hash=configuration, preimage=b"one frozen resolution matrix"
            )
        )
        connection.execute(
            context_packages_v3.insert().values(
                package_hash=package, manifest=b"one supervised manifest"
            )
        )
        connection.execute(
            node_execution_requests_v3.insert().values(
                request_hash=request,
                node_execution_id=execution,
                run_configuration_revision_hash=configuration,
                context_package_hash=package,
                preimage=b"one node execution request",
            )
        )
        connection.execute(
            runs.insert().values(
                run_id=ARCHIVED_RUN_ID,
                bootstrap_workflow_id="bootstrap-archived-night-run",
                revision_hash=published.revision_hash.value,
                workflow_format_version=3,
                agent_binding_set_hash=None,
                current_node_id="cook",
                state="STARTED",
                state_version=1,
                last_event_sequence=1,
                terminal_hash=None,
                run_configuration_revision_hash=configuration,
            )
        )
        archived = _archived_completion(
            WorkflowRevisionHash(published.revision_hash.value)
        )
        connection.execute(
            sa.text(
                "INSERT INTO run_events (run_id, revision_hash, event_sequence, "
                "node_id, node_execution_id, event_kind, payload, payload_hash, "
                "event_hash) VALUES (:run_id, :revision_hash, :event_sequence, "
                ":node_id, :node_execution_id, :event_kind, :payload, "
                ":payload_hash, :event_hash)"
            ),
            {
                "run_id": archived.run_id.value,
                "revision_hash": archived.revision_hash.value,
                "event_sequence": archived.event_sequence,
                "node_id": archived.node_id,
                "node_execution_id": archived.node_execution_id.value,
                "event_kind": archived.event_kind.value,
                "payload": archived.payload,
                "payload_hash": archived.payload_hash.value,
                "event_hash": archived.event_hash.value,
            },
        )
        connection.execute(
            node_receipts_v3.insert().values(
                node_execution_id=execution,
                disposition="succeeded",
                reason="completed",
                request_hash=request,
                context_package_hash=package,
                receipt_hash=receipt,
            )
        )
        connection.commit()
    engine.dispose()
    with sqlite3.connect(database_path) as connection:
        _require_product_shape(connection, V13_SCHEMA_HANDOFF.version)


@pytest.mark.proves("an-exact-v13-store-migrates-and-opens-as-the-current-schema")
def test_an_exact_v13_store_migrates_and_opens_as_the_current_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    engine = create_canonical_engine(database_path)
    with pytest.raises(MigrationRequired, match="schema version 13"):
        initialize_schema(engine)
    engine.dispose()

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert all(str(step) in shown.out for step in range(13, SCHEMA_VERSION + 1))
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out

    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(atelier_schema_versions.c.version))
            == SCHEMA_VERSION
        )
        assert (
            connection.scalar(
                sa.select(runs.c.run_id).where(runs.c.run_id == ARCHIVED_RUN_ID)
            )
            == ARCHIVED_RUN_ID
        )
        assert (
            connection.scalar(sa.select(node_receipts_v3.c.disposition)) == "succeeded"
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(run_inputs_v3))
            == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tool_redemptions))
            == 0
        )
        archived = (
            connection.execute(
                sa.select(run_events).where(run_events.c.run_id == ARCHIVED_RUN_ID)
            )
            .mappings()
            .one()
        )
        expected = _archived_completion(
            WorkflowRevisionHash(str(archived["revision_hash"]))
        )
        assert bytes(archived["payload"]) == ARCHIVED_OUTPUT
        assert str(archived["event_hash"]) == expected.event_hash.value
        assert archived["agent_receipt_hash"] is None
        assert event_from_record(archived) == expected
    engine.dispose()


@pytest.mark.proves("an-unknown-or-future-schema-is-refused-by-name")
def test_an_unknown_or_future_schema_is_refused_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    with engine.begin() as connection:
        connection.execute(atelier_schema_versions.delete())
        connection.execute(
            atelier_schema_versions.insert().values(version=SCHEMA_VERSION + 1)
        )
    engine.dispose()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert str(SCHEMA_VERSION + 1) in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before


@pytest.mark.proves("a-current-schema-store-is-a-named-noop")
def test_a_current_schema_store_is_a_named_noop(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)
    initialize_schema(engine)
    engine.dispose()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 0

    shown = capsys.readouterr()
    assert "already current" in shown.out
    assert "nothing to migrate" in shown.out
    assert PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256 in shown.out
    assert _logical_dump(database_path) == before


def test_an_older_predecessor_without_a_step_is_refused_by_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
            CREATE TABLE predecessor_witness(value BLOB NOT NULL);
            INSERT INTO atelier_schema_versions VALUES(12);
            INSERT INTO predecessor_witness VALUES(X'00FF');
            """
        )
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "12" in shown.err
    assert "no migration step" in shown.err
    assert _logical_dump(database_path) == before


def test_a_locked_store_is_refused_without_mutation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    before = _logical_dump(database_path)
    holder = sqlite3.connect(database_path)
    holder.execute("BEGIN IMMEDIATE")
    try:
        assert main(["migrate", "--database", str(database_path)]) == 1
    finally:
        holder.rollback()
        holder.close()
    assert "in use" in capsys.readouterr().err
    assert _logical_dump(database_path) == before


@pytest.mark.parametrize(
    "collision_sql",
    [
        pytest.param(
            "CREATE TABLE run_events_before_the_receipt_column(wrong TEXT)",
            id="table",
        ),
        pytest.param(
            "CREATE VIEW run_events_before_the_receipt_column AS SELECT 1 AS wrong",
            id="view",
        ),
    ],
)
def test_a_refused_receipt_column_hop_rolls_back_every_earlier_step(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], collision_sql: str
) -> None:
    """The last step refuses, so the two that already ran are undone with it.

    The receipt-column hop rebuilds `run_events` under a parking name, so any
    object already holding that name is a collision the hop refuses by name.
    It sits behind two completed steps in the same transaction, which is what
    makes this the whole hop's atomicity and not just this step's.
    """
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(collision_sql)
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert "run_events_before_the_receipt_column" in shown.err
    assert "will not alter" in shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)


def test_a_failed_step_leaves_the_predecessor_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _create_populated_v13_store(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE run_inputs_v3(wrong TEXT)")
        connection.commit()
    before = _logical_dump(database_path)

    assert main(["migrate", "--database", str(database_path)]) == 1

    shown = capsys.readouterr()
    assert shown.err
    assert _logical_dump(database_path) == before
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT version FROM atelier_schema_versions"
        ).fetchone() == (13,)
