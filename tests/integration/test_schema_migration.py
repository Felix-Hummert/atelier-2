from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import exc
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    _PRODUCT_SCHEMA_FINGERPRINT_SHA256,
    PRODUCT_SCHEMA_HANDOFF,
    SCHEMA_VERSION,
    V9_SCHEMA_HANDOFF,
    V10_SCHEMA_HANDOFF,
    V11_SCHEMA_HANDOFF,
    V12_SCHEMA_HANDOFF,
    V13_SCHEMA_HANDOFF,
    V14_SCHEMA_HANDOFF,
    V15_SCHEMA_HANDOFF,
    MigrationRequired,
    UnsupportedSchemaVersion,
    _product_schema_fingerprint,
    _product_schema_fingerprint_sha256,
    catalog_lineage_aliases,
    catalog_lineage_members,
    catalog_lineage_retirements,
    catalog_lineages,
    context_packages_v3,
    effect_intents,
    effect_receipts,
    initialize_schema,
    node_artifacts_v3,
    node_execution_requests_v3,
    node_receipt_access_v3,
    node_receipt_outputs_v3,
    node_receipts_v3,
    published_revisions,
    reconcile_commands,
    run_configuration_revisions,
    run_events,
    runs,
    workflow_revisions,
)
from atelier2.contracts.catalog_v3 import CatalogLineage
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind


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


def test_empty_database_creates_the_exact_current_schema_and_reopens(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database_path)

    initialize_schema(engine)

    with sqlite3.connect(database_path) as connection:
        assert (
            _product_schema_fingerprint_sha256(_product_schema_fingerprint(connection))
            == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[SCHEMA_VERSION]
        )
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.text("SELECT version FROM atelier_schema_versions"))
            == SCHEMA_VERSION
        )
        assert set(sa.inspect(connection).get_table_names()) >= {
            effect_intents.name,
            effect_receipts.name,
            reconcile_commands.name,
            published_revisions.name,
            catalog_lineages.name,
            catalog_lineage_members.name,
            catalog_lineage_aliases.name,
            catalog_lineage_retirements.name,
            node_artifacts_v3.name,
            node_receipts_v3.name,
            node_receipt_outputs_v3.name,
            node_receipt_access_v3.name,
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


def test_published_handoffs_pin_every_predecessor_and_the_current_schema() -> None:
    assert V9_SCHEMA_HANDOFF.version == 9
    assert (
        V9_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[8]
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[9]
        == "6ba76214cb567ffcdab46e5a3ae00fc10824b962f16a8036ce90590be0b79b38"
    )
    assert V10_SCHEMA_HANDOFF.version == 10
    assert (
        V10_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[10]
        == "4a7bbd9bf07880868aa2f7ddae3e7262eb270f711d4fdc420f902457817bfff7"
    )
    assert V11_SCHEMA_HANDOFF.version == 11
    assert (
        V11_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[11]
        == "18dead2ab36c15bf61fa1b1bb5fed3b5a1075dc773d83d8b57c00c05c84178ef"
    )
    assert V12_SCHEMA_HANDOFF.version == 12
    assert (
        V12_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[12]
        == "feef25b171e305bb9a3a9637cc4d0fb1c8dec4a4a7a9813e060ccf12598a5cc7"
    )
    assert V13_SCHEMA_HANDOFF.version == 13
    assert (
        V13_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[13]
        == "5782fdc1331c52f3f04097f6a2a6d416ab528d6ee8a6546a7d6435ae9d11c175"
    )
    assert V14_SCHEMA_HANDOFF.version == 14
    assert (
        V14_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[14]
        == "6cf56491322e716fce9be2310584ed2b92533961b8fda341bfcc317182432f0a"
    )
    assert V15_SCHEMA_HANDOFF.version == 15
    assert (
        V15_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[15]
        == "375e81d1c8967053951d1be0cab19cee274e35272f364feae15ec3413eb3c9b9"
    )
    assert PRODUCT_SCHEMA_HANDOFF.version == SCHEMA_VERSION == 16
    assert (
        PRODUCT_SCHEMA_HANDOFF.fingerprint_sha256
        == _PRODUCT_SCHEMA_FINGERPRINT_SHA256[16]
        == "97605fb330cb6382d52a554d644015f631cccea3759c04c27de3ca5f1fea9c3a"
    )


@pytest.mark.parametrize("version", [7, 8, 9, 10, 11, 12, 13, 14, 15])
def test_predecessor_store_is_refused_without_mutation(
    tmp_path: Path, version: int
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            f"""
            CREATE TABLE atelier_schema_versions(version INTEGER PRIMARY KEY);
            CREATE TABLE predecessor_witness(value BLOB NOT NULL);
            INSERT INTO atelier_schema_versions VALUES({version});
            INSERT INTO predecessor_witness VALUES(X'00FF');
            """
        )
    before_files = _file_snapshot(tmp_path)
    before_logical = _logical_dump(database_path)
    engine = create_canonical_engine(database_path)

    with pytest.raises(MigrationRequired, match=f"schema version {version}"):
        initialize_schema(engine)

    engine.dispose()
    assert _file_snapshot(tmp_path) == before_files
    assert _logical_dump(database_path) == before_logical


def _published_workflow(document: bytes = b"name: lasagne\n") -> PublishedRevision:
    return PublishedRevision(RevisionKind.WORKFLOW, document)


def _write_thin_vertical_set(
    connection: Connection,
    published: PublishedRevision,
    *,
    run_id: str = "run-lasagne",
    execution: str = "11" * 32,
    request: str = "22" * 32,
    package: str = "33" * 32,
    configuration: str = "44" * 32,
    receipt: str = "ef" * 32,
) -> CatalogLineage:
    lineage = CatalogLineage(published.kind, published.revision_hash)
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
            revision_hash=published.revision_hash.value, document=published.document
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
            run_id=run_id,
            bootstrap_workflow_id="bootstrap-lasagne",
            revision_hash=published.revision_hash.value,
            workflow_format_version=3,
            agent_binding_set_hash=None,
            current_node_id="cook",
            state="STARTED",
            state_version=0,
            last_event_sequence=0,
            terminal_hash=None,
            run_configuration_revision_hash=configuration,
        )
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
    return lineage


def test_thin_v14_store_accepts_revision_lineage_member_run_and_receipt(
    tmp_path: Path,
) -> None:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    published = _published_workflow()
    with engine.begin() as connection:
        _write_thin_vertical_set(connection, published)
    with engine.connect() as connection:
        stored_hash = connection.scalar(sa.select(published_revisions.c.revision_hash))
        assert stored_hash == published.revision_hash.value
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(catalog_lineage_members)
            )
            == 1
        )
        assert (
            connection.scalar(sa.select(node_receipts_v3.c.disposition)) == "succeeded"
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            node_receipts_v3.insert().values(
                node_execution_id="44" * 32,
                disposition="stale",
                reason="projected",
                request_hash="22" * 32,
                context_package_hash="33" * 32,
                receipt_hash="55" * 32,
            )
        )
    engine.dispose()


def test_thin_v14_store_refuses_invented_kind_and_unpublished_membership(
    tmp_path: Path,
) -> None:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    published = _published_workflow()
    schema_revision = PublishedRevision(RevisionKind.SCHEMA, b"type: object\n")
    missing = "ab" * 32
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            published_revisions.insert().values(
                kind="invented_kind",
                revision_hash=published.revision_hash.value,
                document=published.document,
            )
        )
    with engine.begin() as connection:
        connection.execute(
            published_revisions.insert().values(
                kind=published.kind.value,
                revision_hash=published.revision_hash.value,
                document=published.document,
            )
        )
        connection.execute(
            published_revisions.insert().values(
                kind=schema_revision.kind.value,
                revision_hash=schema_revision.revision_hash.value,
                document=schema_revision.document,
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            catalog_lineages.insert().values(
                lineage_id="cd" * 32,
                kind=published.kind.value,
                founding_revision_hash=missing,
            )
        )
    lineage = CatalogLineage(published.kind, published.revision_hash)
    with engine.begin() as connection:
        connection.execute(
            catalog_lineages.insert().values(
                lineage_id=lineage.lineage_id.value,
                kind=published.kind.value,
                founding_revision_hash=published.revision_hash.value,
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            catalog_lineage_members.insert().values(
                lineage_id=lineage.lineage_id.value,
                revision_number=1,
                revision_hash=missing,
            )
        )
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            catalog_lineage_members.insert().values(
                lineage_id=lineage.lineage_id.value,
                revision_number=1,
                revision_hash=schema_revision.revision_hash.value,
            )
        )
    engine.dispose()


@pytest.mark.parametrize(
    "failpoint",
    (
        "published_revisions",
        "catalog_lineages",
        "catalog_lineage_members",
        "runs",
        "node_receipts_v3",
    ),
)
def test_thin_v14_write_failpoint_rolls_back_revision_lineage_run_and_receipt(
    tmp_path: Path, failpoint: str
) -> None:
    engine = create_canonical_engine(tmp_path / failpoint)
    initialize_schema(engine)
    published = _published_workflow()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            f"CREATE TRIGGER fail_{failpoint} BEFORE INSERT ON {failpoint} "
            "BEGIN SELECT RAISE(ABORT, 'failpoint'); END"
        )
    with (
        pytest.raises(exc.DatabaseError, match="failpoint"),
        engine.begin() as connection,
    ):
        _write_thin_vertical_set(connection, published)
    with engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(published_revisions)
            )
            == 0
        )
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(catalog_lineages))
            == 0
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(catalog_lineage_members)
            )
            == 0
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(node_receipts_v3))
            == 0
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(workflow_revisions)
            )
            == 0
        )
    engine.dispose()


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


@pytest.mark.parametrize(
    ("event_kind", "agent_receipt_hash", "admitted"),
    [
        pytest.param("AGENT_COMPLETED", "c" * 64, True, id="completion-with-binding"),
        pytest.param("AGENT_COMPLETED", None, True, id="completion-before-v3"),
        pytest.param("AGENT_COMPLETED", "c" * 63, False, id="not-a-digest"),
        pytest.param("AGENT_COMPLETED", "C" * 64, False, id="not-lowercase"),
        pytest.param("WAITING_INPUT", "c" * 64, False, id="wait-carrying-a-binding"),
        pytest.param(
            "SUBWORKFLOW_COMPLETED",
            "c" * 64,
            False,
            id="subworkflow-carrying-a-binding",
        ),
    ],
)
def test_the_store_admits_a_receipt_binding_only_on_an_agent_completion(
    ledger_engine: Engine,
    event_kind: str,
    agent_receipt_hash: str | None,
    admitted: bool,
) -> None:
    """The store's half of the rule `contracts/executions.py` states.

    Both halves land together on purpose: a contract that admits a field the
    store refuses is a rule only the integration run discovers.
    """

    def write() -> None:
        with ledger_engine.begin() as connection:
            revision = str(
                connection.scalar(
                    sa.select(runs.c.revision_hash).where(runs.c.run_id == "run-1")
                )
            )
            connection.execute(
                run_events.insert().values(
                    run_id="run-1",
                    revision_hash=revision,
                    event_sequence=1,
                    node_id="agent",
                    node_execution_id="1" * 64,
                    event_kind=event_kind,
                    payload=b"event",
                    payload_hash=hashlib.sha256(b"event").hexdigest(),
                    event_hash="a" * 64,
                    agent_receipt_hash=agent_receipt_hash,
                )
            )

    if admitted:
        write()
        with ledger_engine.connect() as connection:
            assert (
                connection.scalar(sa.select(run_events.c.agent_receipt_hash))
                == agent_receipt_hash
            )
        return
    with pytest.raises(IntegrityError, match="agent_receipt_hash"):
        write()


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
