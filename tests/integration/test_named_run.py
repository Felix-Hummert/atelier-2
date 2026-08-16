"""Start-by-name over the real catalog and the real atomic supervised start.

Every sentence here is about **in-process truth**: one engine, one transaction
discipline, no server and no HTTP boundary. A round-trip through a running API
would prove that the server wrote something, not that this caller's name reached
exactly those bytes and that the whole record arrived together.

**Nothing here runs a workflow.** Cut B's starter persists an already-decided
terminal truth "without claiming an executable engine", and this seam does not
claim one either. What is proven is which bytes a name reached and that the
record of them is whole. V3 execution is open and belongs to #194 H1.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
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
    node_artifacts_v3,
    node_receipt_access_v3,
    node_receipt_outputs_v3,
    node_receipts_v3,
    published_revisions,
    runs,
    workflow_revisions,
)
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.application.resolve_catalog_name import (
    CatalogNameLineageRetired,
    CatalogNameMissing,
)
from atelier2.application.start_named_run import (
    NamedRunNameUnresolved,
    NamedRunRevisionUnbound,
    NamedRunStarted,
    NamedRunTruthForAnotherRevision,
    start_named_run,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
    CatalogLineageDisplayName,
    CatalogLineageId,
    CatalogRetirementState,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.ports.agent_executions import AgentExecutorRegistry
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    PublishedRevisionMissing,
    ResolveCatalogNameResult,
    ResolvePublishedRevisionResult,
)
from tests.scenarios.v3_proof_run import (
    PROOF_WORKFLOW_DOCUMENT,
    decided_truth_for,
)

DISPLAY_NAME = CatalogLineageDisplayName("lasagne")
ACTOR = CatalogActor("operator")
ACTIVATED_AT = CatalogActivatedAt("2026-08-16T00:00:00Z")

THE_SEVEN_DURABLE_TABLES = (
    published_revisions,
    workflow_revisions,
    runs,
    node_artifacts_v3,
    node_receipts_v3,
    node_receipt_outputs_v3,
    node_receipt_access_v3,
)
"""Every table one supervised start writes, so "all or nothing" can be measured.

Naming them here rather than counting rows in one of them is what makes the
rollback assertion whole: a partial write that reached six of the seven would
pass any narrower check.
"""


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


def durable_snapshot(engine: Engine) -> dict[str, list[str]]:
    """Every row of every table a supervised start touches, in full."""
    with engine.connect() as connection:
        return {
            table.name: sorted(
                repr(tuple(row)) for row in connection.execute(sa.select(table))
            )
            for table in THE_SEVEN_DURABLE_TABLES
        }


def row_counts(engine: Engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table.name: connection.scalar(sa.select(sa.func.count()).select_from(table))
            or 0
            for table in THE_SEVEN_DURABLE_TABLES
        }


def refuse_the_last_write(engine: Engine) -> None:
    """Fail the final write of the set, after every earlier one has landed.

    The receipt's access entries are written last, so a fault here is the latest
    boundary a named start has: everything the transaction did is already staged
    behind it. Cut B proves each individual boundary; what this proves is that
    the *named* path rolls the whole set back from the last one.
    """
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER refuse_receipt_access "
            "BEFORE INSERT ON node_receipt_access_v3 "
            "BEGIN SELECT RAISE(ABORT, 'injected late boundary failure'); END"
        )


@pytest.mark.proves("a-name-reaches-exactly-the-bytes-it-resolved-to")
def test_a_name_reaches_the_exact_bytes_the_catalog_holds_under_it(
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
    with engine.connect() as connection:
        assert (
            connection.scalar(sa.select(published_revisions.c.document))
            == PROOF_WORKFLOW_DOCUMENT
        )


@pytest.mark.proves("a-named-start-records-the-whole-durable-set-or-nothing")
def test_a_named_start_records_a_row_in_every_one_of_the_seven_tables(
    workshop: tuple[Engine, DbosCatalogStore, DbosDurableRunStarter],
) -> None:
    """All-or-nothing is only a claim if "all" is stated table by table."""
    engine, catalog, starter = workshop
    revision = admitted(catalog)
    # Admission already published the revision; everything the start itself owns
    # is still empty, which is what makes the counts below its own work.
    before = row_counts(engine)
    assert before[published_revisions.name] == 1
    assert all(
        count == 0
        for table, count in before.items()
        if table != published_revisions.name
    )

    result = start_named_run(
        RevisionKind.WORKFLOW,
        DISPLAY_NAME,
        "head",
        decided_truth_for(revision),
        catalog,
        starter,
    )

    assert isinstance(result, NamedRunStarted)
    assert row_counts(engine) == {
        published_revisions.name: 1,
        workflow_revisions.name: 1,
        runs.name: 1,
        node_artifacts_v3.name: 1,
        node_receipts_v3.name: 1,
        node_receipt_outputs_v3.name: 1,
        node_receipt_access_v3.name: 1,
    }


@pytest.mark.proves("a-named-start-records-the-whole-durable-set-or-nothing")
def test_a_failure_at_the_last_boundary_rolls_the_whole_named_start_back(
    workshop: tuple[Engine, DbosCatalogStore, DbosDurableRunStarter],
) -> None:
    engine, catalog, starter = workshop
    revision = admitted(catalog)
    refuse_the_last_write(engine)
    before = durable_snapshot(engine)

    result = start_named_run(
        RevisionKind.WORKFLOW,
        DISPLAY_NAME,
        "head",
        decided_truth_for(revision),
        catalog,
        starter,
    )

    assert not isinstance(result, NamedRunStarted)
    assert durable_snapshot(engine) == before


@pytest.mark.proves("a-name-reaches-exactly-the-bytes-it-resolved-to")
def test_a_retired_lineage_records_nothing_and_says_which_refusal_it_is(
    workshop: tuple[Engine, DbosCatalogStore, DbosDurableRunStarter],
) -> None:
    engine, catalog, starter = workshop
    revision = admitted(catalog)
    lineage = CatalogLineage(revision.kind, revision.revision_hash)
    catalog.retire_lineage(
        lineage.lineage_id, CatalogRetirementState.RETIRED, ACTOR, ACTIVATED_AT
    )
    before = durable_snapshot(engine)

    result = start_named_run(
        RevisionKind.WORKFLOW,
        DISPLAY_NAME,
        "head",
        decided_truth_for(revision),
        catalog,
        starter,
    )

    assert isinstance(result, NamedRunNameUnresolved)
    assert isinstance(result.refusal, CatalogNameLineageRetired)
    assert result.refusal.lineage_id == lineage.lineage_id
    assert durable_snapshot(engine) == before


@pytest.mark.proves("a-name-reaches-exactly-the-bytes-it-resolved-to")
def test_a_name_nobody_admitted_records_nothing_and_says_which_refusal_it_is(
    workshop: tuple[Engine, DbosCatalogStore, DbosDurableRunStarter],
) -> None:
    engine, catalog, starter = workshop
    revision = PublishedRevision(RevisionKind.WORKFLOW, PROOF_WORKFLOW_DOCUMENT)
    unknown = CatalogLineageDisplayName("nobody-admitted-this")
    before = durable_snapshot(engine)

    result = start_named_run(
        RevisionKind.WORKFLOW,
        unknown,
        "head",
        decided_truth_for(revision),
        catalog,
        starter,
    )

    assert isinstance(result, NamedRunNameUnresolved)
    assert isinstance(result.refusal, CatalogNameMissing)
    assert result.refusal.query == unknown
    assert durable_snapshot(engine) == before


@pytest.mark.proves("a-named-start-never-records-a-result-for-other-bytes")
def test_a_decided_truth_for_another_revision_is_refused_without_a_write(
    workshop: tuple[Engine, DbosCatalogStore, DbosDurableRunStarter],
) -> None:
    """The name reached one revision; a truth about another must not land."""
    engine, catalog, starter = workshop
    revision = admitted(catalog)
    stranger = PublishedRevision(
        RevisionKind.WORKFLOW, PROOF_WORKFLOW_DOCUMENT + b"# another\n"
    )
    before = durable_snapshot(engine)

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
    assert durable_snapshot(engine) == before


@dataclass
class ResolverThatDeniesItsOwnName:
    """A resolver whose name lookup finds what its reference lookup denies.

    A self-consistent store cannot reach this state, which is exactly why the
    door needs a fake to prove it handles the outcome the port's contract
    permits. Deleting the arm instead would fold a named refusal back into the
    generic one -- the regression #187's own repair existed to remove.
    """

    lineage_id: CatalogLineageId
    revision_hash: PublishedRevisionHash

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        return PublishedRevisionMissing()

    def resolve_reference(
        self,
        kind: RevisionKind,
        lineage_id: CatalogLineageId,
        revision_hash: PublishedRevisionHash,
    ) -> ResolvePublishedRevisionResult:
        return PublishedRevisionMissing()

    def resolve_name(
        self, kind: RevisionKind, lineage_id_or_name: object, position: object
    ) -> ResolveCatalogNameResult:
        return CatalogNameFound(
            lineage_id=self.lineage_id,
            revision_hash=self.revision_hash,
            revision_number=1,
            current_display_name=DISPLAY_NAME,
            retired=False,
        )


@pytest.mark.proves("a-name-reaches-exactly-the-bytes-it-resolved-to")
def test_a_name_whose_member_is_not_admitted_is_refused_by_that_name(
    workshop: tuple[Engine, DbosCatalogStore, DbosDurableRunStarter],
) -> None:
    engine, _catalog, starter = workshop
    revision = PublishedRevision(RevisionKind.WORKFLOW, PROOF_WORKFLOW_DOCUMENT)
    lineage = CatalogLineage(revision.kind, revision.revision_hash)
    before = durable_snapshot(engine)

    result = start_named_run(
        RevisionKind.WORKFLOW,
        DISPLAY_NAME,
        "head",
        decided_truth_for(revision),
        ResolverThatDeniesItsOwnName(lineage.lineage_id, revision.revision_hash),
        starter,
    )

    assert isinstance(result, NamedRunRevisionUnbound)
    assert result.lineage_id == lineage.lineage_id
    assert durable_snapshot(engine) == before
