"""The offline hop a live V13 store needed and did not have.

The V13 fixture is a real predecessor store: the current create path, the V14
addition removed, then a format-3 run. That is the #240 Z2 method — predecessor
schema, not a version-row stub — expressed through today's owner.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
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
    run_inputs_v3,
    runs,
    tool_redemptions,
    workflow_revisions,
)
from atelier2.contracts.catalog_v3 import CatalogLineage
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.host import main

ARCHIVED_RUN_ID = "live/erster-lauf-nach-der-nacht"


def _logical_dump(database_path: Path) -> tuple[str, ...]:
    with sqlite3.connect(database_path) as connection:
        return tuple(connection.iterdump())


def _create_populated_v13_store(database_path: Path) -> None:
    """An exact V13 product store, not a version-row witness.

    Every published step since V13 is strictly additive, so a fresh store of the
    current schema with each later table and its triggers removed is the
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
