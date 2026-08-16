from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    catalog_lineage_members,
    catalog_lineages,
    initialize_schema,
)
from atelier2.application.resolve_catalog_name import (
    CatalogNameMissing,
    CatalogReferenceNonMember,
    CatalogReferenceResolved,
    resolve_catalog_name,
    resolve_catalog_reference,
)
from atelier2.contracts.catalog_v3 import (
    CatalogLineage,
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
    PublishedRevisionFound,
    PublishedRevisionMissing,
)


@dataclass(frozen=True)
class CatalogHarness:
    catalog: DbosCatalogStore
    engine: Engine

    def found(self, published: PublishedRevision) -> CatalogLineage:
        lineage = CatalogLineage(published.kind, published.revision_hash)
        with self.engine.begin() as connection:
            connection.execute(
                catalog_lineages.insert().values(
                    lineage_id=lineage.lineage_id.value,
                    kind=published.kind.value,
                    founding_revision_hash=published.revision_hash.value,
                )
            )
        return lineage

    def admit(
        self,
        lineage: CatalogLineage,
        published: PublishedRevision,
        revision_number: int,
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                catalog_lineage_members.insert().values(
                    lineage_id=lineage.lineage_id.value,
                    revision_number=revision_number,
                    revision_hash=published.revision_hash.value,
                )
            )


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[CatalogHarness]:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    try:
        yield CatalogHarness(DbosCatalogStore(engine), engine)
    finally:
        engine.dispose()


def _workflow(document: bytes) -> PublishedRevision:
    return PublishedRevision(RevisionKind.WORKFLOW, document)


def test_reference_resolution_returns_the_exact_published_bytes(
    harness: CatalogHarness,
) -> None:
    exact_document = b"name: lasagne\ndescription:  exact source spacing\nsteps: []\n"
    published = _workflow(exact_document)
    assert harness.catalog.publish_revision(published) == PublishedRevisionCreated(
        published
    )
    lineage = harness.found(published)
    harness.admit(lineage, published, 1)

    result = resolve_catalog_reference(
        published.kind,
        lineage.lineage_id,
        published.revision_hash,
        harness.catalog,
    )

    assert result == CatalogReferenceResolved(published)
    assert isinstance(result, CatalogReferenceResolved)
    assert result.revision.document == exact_document
    assert result.revision.revision_hash == PublishedRevisionHash.of(exact_document)
    assert harness.catalog.resolve(
        published.kind, published.revision_hash
    ) == PublishedRevisionFound(published)


def test_lineage_free_resolve_does_not_need_membership(
    harness: CatalogHarness,
) -> None:
    published = _workflow(b"name: unpublished-member\n")
    harness.catalog.publish_revision(published)

    assert harness.catalog.resolve(
        published.kind, published.revision_hash
    ) == PublishedRevisionFound(published)
    assert resolve_catalog_reference(
        published.kind,
        CatalogLineage(published.kind, published.revision_hash).lineage_id,
        published.revision_hash,
        harness.catalog,
    ) == CatalogReferenceNonMember(
        CatalogLineage(published.kind, published.revision_hash).lineage_id,
        published.revision_hash,
    )


def test_missing_founding_is_not_a_resolved_revision(harness: CatalogHarness) -> None:
    published = _workflow(b"name: existing\n")
    harness.catalog.publish_revision(published)
    lineage = harness.found(published)
    harness.admit(lineage, published, 1)
    missing_lineage = CatalogLineageId("ab" * 32)

    assert resolve_catalog_reference(
        published.kind,
        missing_lineage,
        published.revision_hash,
        harness.catalog,
    ) == CatalogReferenceNonMember(missing_lineage, published.revision_hash)


def test_unpublished_member_is_not_a_resolved_revision(
    harness: CatalogHarness,
) -> None:
    founding = _workflow(b"name: founding\n")
    later = _workflow(b"name: later\n")
    harness.catalog.publish_revision(founding)
    harness.catalog.publish_revision(later)
    lineage = harness.found(founding)
    harness.admit(lineage, founding, 1)

    assert resolve_catalog_reference(
        later.kind, lineage.lineage_id, later.revision_hash, harness.catalog
    ) == CatalogReferenceNonMember(lineage.lineage_id, later.revision_hash)
    assert harness.catalog.resolve(
        later.kind, later.revision_hash
    ) == PublishedRevisionFound(later)


def test_wrong_kind_is_not_a_resolved_revision(harness: CatalogHarness) -> None:
    published = _workflow(b"name: lasagne\n")
    harness.catalog.publish_revision(published)
    lineage = harness.found(published)
    harness.admit(lineage, published, 1)

    assert resolve_catalog_reference(
        RevisionKind.SCHEMA,
        lineage.lineage_id,
        published.revision_hash,
        harness.catalog,
    ) == CatalogReferenceNonMember(lineage.lineage_id, published.revision_hash)
    assert (
        harness.catalog.resolve(RevisionKind.SCHEMA, published.revision_hash)
        == PublishedRevisionMissing()
    )


def test_republishing_the_same_bytes_is_existing(harness: CatalogHarness) -> None:
    published = _workflow(b"name: lasagne\n")
    assert harness.catalog.publish_revision(published) == PublishedRevisionCreated(
        published
    )
    assert harness.catalog.publish_revision(published) == PublishedRevisionExisting(
        published
    )


def test_name_lookup_does_not_invent_a_result_without_alias_tables(
    harness: CatalogHarness,
) -> None:
    published = _workflow(b"name: lasagne\n")
    harness.catalog.publish_revision(published)
    lineage = harness.found(published)
    harness.admit(lineage, published, 1)
    display_name = CatalogLineageDisplayName("lasagne")

    assert resolve_catalog_name(
        published.kind, display_name, "head", harness.catalog
    ) == CatalogNameMissing(display_name, "head")
    assert resolve_catalog_name(
        published.kind, lineage.lineage_id, "head", harness.catalog
    ) == CatalogNameMissing(lineage.lineage_id, "head")
    assert resolve_catalog_name(
        published.kind, CatalogLineageDisplayName("unknown"), 1, harness.catalog
    ) == CatalogNameMissing(CatalogLineageDisplayName("unknown"), 1)
