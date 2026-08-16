"""Start-by-name over the real catalog and the real atomic V3 start.

Every sentence here is about **in-process truth**: one engine, one transaction
discipline, no server and no HTTP boundary. A round-trip through a running API
would prove that the server wrote something, not that this caller's name
resolved to exactly those bytes and that the three rows arrived together.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import (
    DbosRuntimeSettings,
    create_canonical_engine,
)
from atelier2.adapters.dbos.schema import (
    initialize_schema,
    node_receipts_v3,
    published_revisions,
    runs,
)
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
    CatalogLineageDisplayName,
    CatalogRetirementState,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.host.named_run import (
    NamedRunNameUnresolved,
    NamedRunStarted,
    NamedRunTruthForAnotherRevision,
    start_named_run,
)
from atelier2.ports.agent_executions import AgentExecutorRegistry
from tests.scenarios.v3_proof_run import (
    PROOF_WORKFLOW_DOCUMENT,
    decided_truth_for,
)

DISPLAY_NAME = CatalogLineageDisplayName("lasagne")
ACTOR = CatalogActor("operator")
ACTIVATED_AT = CatalogActivatedAt("2026-08-16T00:00:00Z")


@pytest.fixture
def workshop(
    tmp_path: Path,
) -> Iterator[tuple[Engine, DbosCatalogStore, DbosDurableRunStarter]]:
    database = tmp_path / "atelier.sqlite"
    engine = create_canonical_engine(database)
    initialize_schema(engine)
    try:
        yield (
            engine,
            DbosCatalogStore(engine),
            DbosDurableRunStarter(
                engine,
                DbosRuntimeSettings(database, "cut-c-test"),
                AgentExecutorRegistry(),
            ),
        )
    finally:
        engine.dispose()


def admitted(
    catalog: DbosCatalogStore, document: bytes = PROOF_WORKFLOW_DOCUMENT
) -> PublishedRevision:
    revision = PublishedRevision(RevisionKind.WORKFLOW, document)
    catalog.publish_revision(revision)
    catalog.found_lineage(revision, DISPLAY_NAME, ACTOR, ACTIVATED_AT)
    return revision


def stored_document(engine: Engine) -> bytes | None:
    with engine.connect() as connection:
        return connection.scalar(sa.select(published_revisions.c.document))


def table_count(engine: Engine, table: sa.Table) -> int:
    with engine.connect() as connection:
        return connection.scalar(sa.select(sa.func.count()).select_from(table)) or 0


@pytest.mark.proves("a-name-starts-exactly-the-bytes-it-resolved-to")
def test_a_name_starts_the_exact_bytes_the_catalog_holds_under_it(
    workshop: tuple[Engine, DbosCatalogStore, DbosDurableRunStarter],
) -> None:
    """The whole point of the name: it reaches these bytes and no others."""
    engine, catalog, starter = workshop
    revision = admitted(catalog)

    result = start_named_run(
        RevisionKind.WORKFLOW,
        DISPLAY_NAME,
        "head",
        decided_truth_for(revision),
        catalog,
        starter,
    )

    assert isinstance(result, NamedRunStarted)
    assert result.revision_hash == revision.revision_hash
    assert result.current_display_name == DISPLAY_NAME
    assert stored_document(engine) == PROOF_WORKFLOW_DOCUMENT


@pytest.mark.proves("a-name-starts-exactly-the-bytes-it-resolved-to")
def test_a_retired_lineage_starts_nothing_and_says_so_in_the_catalogs_words(
    workshop: tuple[Engine, DbosCatalogStore, DbosDurableRunStarter],
) -> None:
    engine, catalog, starter = workshop
    revision = admitted(catalog)
    lineage = CatalogLineage(revision.kind, revision.revision_hash)
    catalog.retire_lineage(
        lineage.lineage_id, CatalogRetirementState.RETIRED, ACTOR, ACTIVATED_AT
    )

    result = start_named_run(
        RevisionKind.WORKFLOW,
        DISPLAY_NAME,
        "head",
        decided_truth_for(revision),
        catalog,
        starter,
    )

    assert isinstance(result, NamedRunNameUnresolved)
    assert table_count(engine, runs) == 0
    assert table_count(engine, node_receipts_v3) == 0


@pytest.mark.proves("a-name-starts-exactly-the-bytes-it-resolved-to")
def test_a_name_nobody_admitted_starts_nothing(
    workshop: tuple[Engine, DbosCatalogStore, DbosDurableRunStarter],
) -> None:
    engine, catalog, starter = workshop
    revision = PublishedRevision(RevisionKind.WORKFLOW, PROOF_WORKFLOW_DOCUMENT)

    result = start_named_run(
        RevisionKind.WORKFLOW,
        CatalogLineageDisplayName("nobody-admitted-this"),
        "head",
        decided_truth_for(revision),
        catalog,
        starter,
    )

    assert isinstance(result, NamedRunNameUnresolved)
    assert table_count(engine, runs) == 0


@pytest.mark.proves("a-named-start-writes-revision-run-and-receipt-or-nothing")
def test_a_named_start_writes_the_whole_set_or_none_of_it(
    workshop: tuple[Engine, DbosCatalogStore, DbosDurableRunStarter],
) -> None:
    """The atomicity Cut B owns, proven through the door a name opens."""
    engine, catalog, starter = workshop
    revision = admitted(catalog)
    before = (table_count(engine, runs), table_count(engine, node_receipts_v3))

    result = start_named_run(
        RevisionKind.WORKFLOW,
        DISPLAY_NAME,
        "head",
        decided_truth_for(revision, break_receipt_binding=True),
        catalog,
        starter,
    )

    assert not isinstance(result, NamedRunStarted)
    assert (table_count(engine, runs), table_count(engine, node_receipts_v3)) == before


@pytest.mark.proves("a-named-start-never-writes-a-result-for-other-bytes")
def test_a_decided_truth_for_another_revision_is_refused_without_a_write(
    workshop: tuple[Engine, DbosCatalogStore, DbosDurableRunStarter],
) -> None:
    """The name resolved to one revision; a truth about another must not land."""
    engine, catalog, starter = workshop
    revision = admitted(catalog)
    stranger = PublishedRevision(
        RevisionKind.WORKFLOW, PROOF_WORKFLOW_DOCUMENT + b"# another\n"
    )

    result = start_named_run(
        RevisionKind.WORKFLOW,
        DISPLAY_NAME,
        "head",
        decided_truth_for(stranger),
        catalog,
        starter,
    )

    assert isinstance(result, NamedRunTruthForAnotherRevision)
    assert result.resolved == revision.revision_hash
    assert result.decided_for == stranger.revision_hash
    assert table_count(engine, runs) == 0
    assert table_count(engine, node_receipts_v3) == 0
