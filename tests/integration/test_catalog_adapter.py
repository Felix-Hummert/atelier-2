from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import (
    catalog_lineage_aliases,
    catalog_lineage_members,
    catalog_lineage_retirements,
    catalog_lineages,
    initialize_schema,
)
from atelier2.application.resolve_catalog_name import (
    CatalogNameLineageRetired,
    CatalogNameMissing,
    CatalogNameResolved,
    CatalogReferenceNonMember,
    CatalogReferenceResolved,
    resolve_catalog_name,
    resolve_catalog_reference,
)
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
    CatalogLineageDisplayName,
    CatalogLineageId,
    CatalogRetirementState,
    catalog_lineage_query,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt,
    DurableWriteUnavailable,
)
from atelier2.ports.published_revisions import (
    CatalogAdmissionExisting,
    CatalogAdmissionNameHeld,
    CatalogAdmissionRetired,
    CatalogAdmissionRevisionOwned,
    CatalogAdmissionUnpublished,
    CatalogLineageFounded,
    CatalogLineageIdMismatch,
    CatalogLineageRetired,
    CatalogMemberAdmitted,
    PublishedRevisionCreated,
    PublishedRevisionExisting,
    PublishedRevisionFound,
    PublishedRevisionMissing,
)


@dataclass(frozen=True)
class CatalogScene:
    actor: CatalogActor
    founded_at: CatalogActivatedAt
    admitted_at: CatalogActivatedAt
    retired_at: CatalogActivatedAt


@dataclass(frozen=True)
class CatalogHarness:
    catalog: DbosCatalogStore
    engine: Engine

    def found(
        self,
        published: PublishedRevision,
        display_name: CatalogLineageDisplayName,
        scene: CatalogScene,
        claimed_lineage_id: CatalogLineageId | None = None,
    ) -> CatalogLineage:
        result = self.catalog.found_lineage(
            published,
            display_name,
            scene.actor,
            scene.founded_at,
            claimed_lineage_id=claimed_lineage_id,
        )
        assert isinstance(result, CatalogLineageFounded)
        return result.lineage

    def admit(
        self,
        lineage: CatalogLineage,
        published: PublishedRevision,
        display_name: CatalogLineageDisplayName,
        scene: CatalogScene,
    ) -> CatalogMemberAdmitted:
        result = self.catalog.admit_member(
            lineage.lineage_id,
            published,
            display_name,
            scene.actor,
            scene.admitted_at,
        )
        assert isinstance(result, CatalogMemberAdmitted)
        return result

    def retire(self, lineage: CatalogLineage, scene: CatalogScene) -> None:
        result = self.catalog.retire_lineage(
            lineage.lineage_id,
            CatalogRetirementState.RETIRED,
            scene.actor,
            scene.retired_at,
        )
        assert result == CatalogLineageRetired(lineage.lineage_id)


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[CatalogHarness]:
    engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(engine)
    try:
        yield CatalogHarness(DbosCatalogStore(engine), engine)
    finally:
        engine.dispose()


@pytest.fixture
def scene() -> CatalogScene:
    return CatalogScene(
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-16T12:00:00Z"),
        CatalogActivatedAt("2026-08-16T12:01:00Z"),
        CatalogActivatedAt("2026-08-16T12:02:00Z"),
    )


def _workflow(document: bytes) -> PublishedRevision:
    return PublishedRevision(RevisionKind.WORKFLOW, document)


def _lineage_count(harness: CatalogHarness) -> int:
    with harness.engine.connect() as connection:
        return int(
            connection.scalar(sa.select(sa.func.count()).select_from(catalog_lineages))
            or 0
        )


def _member_count(harness: CatalogHarness) -> int:
    with harness.engine.connect() as connection:
        return int(
            connection.scalar(
                sa.select(sa.func.count()).select_from(catalog_lineage_members)
            )
            or 0
        )


def _catalog_snapshot(harness: CatalogHarness) -> dict[str, list[str]]:
    tables = (
        catalog_lineages,
        catalog_lineage_members,
        catalog_lineage_aliases,
        catalog_lineage_retirements,
    )
    with harness.engine.connect() as connection:
        return {
            table.name: sorted(
                repr(tuple(row)) for row in connection.execute(sa.select(table))
            )
            for table in tables
        }


def test_name_resolution_returns_the_exact_published_bytes(
    harness: CatalogHarness, scene: CatalogScene
) -> None:
    exact_document = b"name: lasagne\ndescription:  exact source spacing\nsteps: []\n"
    published = _workflow(exact_document)
    display_name = CatalogLineageDisplayName("lasagne")
    assert harness.catalog.publish_revision(published) == PublishedRevisionCreated(
        published
    )
    lineage = harness.found(published, display_name, scene)

    by_name = resolve_catalog_name(
        published.kind, display_name, "head", harness.catalog
    )
    by_id = resolve_catalog_name(
        published.kind, lineage.lineage_id, "head", harness.catalog
    )
    by_parsed_id = resolve_catalog_name(
        published.kind,
        catalog_lineage_query(lineage.lineage_id.value),
        1,
        harness.catalog,
    )

    assert by_name == by_id == by_parsed_id
    assert by_name == CatalogNameResolved(
        lineage.lineage_id, published, 1, display_name
    )
    assert isinstance(by_name, CatalogNameResolved)
    assert by_name.revision.document == exact_document
    assert by_name.revision.revision_hash == PublishedRevisionHash.of(exact_document)


def test_historical_alias_resolves_to_the_current_name_and_head_bytes(
    harness: CatalogHarness, scene: CatalogScene
) -> None:
    founding = _workflow(b"name: pasta\n")
    later = _workflow(b"name: lasagne\n")
    historical = CatalogLineageDisplayName("pasta")
    current = CatalogLineageDisplayName("lasagne")
    harness.catalog.publish_revision(founding)
    harness.catalog.publish_revision(later)
    lineage = harness.found(founding, historical, scene)
    admitted = harness.admit(lineage, later, current, scene)

    assert admitted.revision_number == 2
    assert resolve_catalog_name(
        founding.kind, historical, "head", harness.catalog
    ) == CatalogNameResolved(lineage.lineage_id, later, 2, current)
    assert resolve_catalog_name(
        founding.kind, current, 1, harness.catalog
    ) == CatalogNameResolved(lineage.lineage_id, founding, 1, current)


@pytest.mark.parametrize("query_kind", ["lineage_id", "historical_alias"])
def test_retired_lineage_is_refused_through_its_id_or_any_alias(
    harness: CatalogHarness, scene: CatalogScene, query_kind: str
) -> None:
    founding = _workflow(b"name: pasta\n")
    later = _workflow(b"name: lasagne\n")
    historical = CatalogLineageDisplayName("pasta")
    current = CatalogLineageDisplayName("lasagne")
    harness.catalog.publish_revision(founding)
    harness.catalog.publish_revision(later)
    lineage = harness.found(founding, historical, scene)
    harness.admit(lineage, later, current, scene)
    harness.retire(lineage, scene)
    query = lineage.lineage_id if query_kind == "lineage_id" else historical

    assert resolve_catalog_name(
        founding.kind, query, "head", harness.catalog
    ) == CatalogNameLineageRetired(lineage.lineage_id, current)


def test_unknown_name_and_missing_position_are_missing(
    harness: CatalogHarness, scene: CatalogScene
) -> None:
    published = _workflow(b"name: lasagne\n")
    display_name = CatalogLineageDisplayName("lasagne")
    harness.catalog.publish_revision(published)
    lineage = harness.found(published, display_name, scene)

    assert resolve_catalog_name(
        published.kind, CatalogLineageDisplayName("unknown"), "head", harness.catalog
    ) == CatalogNameMissing(CatalogLineageDisplayName("unknown"), "head")
    assert resolve_catalog_name(
        published.kind, display_name, 2, harness.catalog
    ) == CatalogNameMissing(display_name, 2)
    assert resolve_catalog_name(
        RevisionKind.SCHEMA, lineage.lineage_id, "head", harness.catalog
    ) == CatalogNameMissing(lineage.lineage_id, "head")


def test_reference_resolution_returns_the_exact_published_bytes(
    harness: CatalogHarness, scene: CatalogScene
) -> None:
    exact_document = b"name: lasagne\ndescription:  exact source spacing\nsteps: []\n"
    published = _workflow(exact_document)
    assert harness.catalog.publish_revision(published) == PublishedRevisionCreated(
        published
    )
    lineage = harness.found(published, CatalogLineageDisplayName("lasagne"), scene)

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


def test_missing_founding_is_not_a_resolved_revision(
    harness: CatalogHarness, scene: CatalogScene
) -> None:
    published = _workflow(b"name: existing\n")
    harness.catalog.publish_revision(published)
    harness.found(published, CatalogLineageDisplayName("existing"), scene)
    missing_lineage = CatalogLineageId("ab" * 32)

    assert resolve_catalog_reference(
        published.kind,
        missing_lineage,
        published.revision_hash,
        harness.catalog,
    ) == CatalogReferenceNonMember(missing_lineage, published.revision_hash)


def test_unpublished_member_is_not_a_resolved_revision(
    harness: CatalogHarness, scene: CatalogScene
) -> None:
    founding = _workflow(b"name: founding\n")
    later = _workflow(b"name: later\n")
    harness.catalog.publish_revision(founding)
    harness.catalog.publish_revision(later)
    lineage = harness.found(founding, CatalogLineageDisplayName("founding"), scene)

    assert resolve_catalog_reference(
        later.kind, lineage.lineage_id, later.revision_hash, harness.catalog
    ) == CatalogReferenceNonMember(lineage.lineage_id, later.revision_hash)
    assert harness.catalog.resolve(
        later.kind, later.revision_hash
    ) == PublishedRevisionFound(later)


def test_wrong_kind_is_not_a_resolved_revision(
    harness: CatalogHarness, scene: CatalogScene
) -> None:
    published = _workflow(b"name: lasagne\n")
    harness.catalog.publish_revision(published)
    lineage = harness.found(published, CatalogLineageDisplayName("lasagne"), scene)

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


def test_writer_derives_the_lineage_id_and_refuses_a_forged_claim(
    harness: CatalogHarness, scene: CatalogScene
) -> None:
    published = _workflow(b"name: lasagne\n")
    display_name = CatalogLineageDisplayName("lasagne")
    harness.catalog.publish_revision(published)
    derived = CatalogLineage(published.kind, published.revision_hash)
    forged = CatalogLineageId("ab" * 32)

    assert harness.catalog.found_lineage(
        published,
        display_name,
        scene.actor,
        scene.founded_at,
        claimed_lineage_id=forged,
    ) == CatalogLineageIdMismatch(forged, derived.lineage_id)
    assert _lineage_count(harness) == 0
    assert _member_count(harness) == 0

    founded = harness.catalog.found_lineage(
        published,
        display_name,
        scene.actor,
        scene.founded_at,
        claimed_lineage_id=derived.lineage_id,
    )
    assert founded == CatalogLineageFounded(derived, published, display_name)
    assert harness.catalog.found_lineage(
        published, display_name, scene.actor, scene.founded_at
    ) == CatalogAdmissionExisting(derived, published, 1, display_name)
    with harness.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(catalog_lineage_aliases)
            )
            == 1
        )


def test_writer_refuses_unpublished_bytes_and_a_reused_name(
    harness: CatalogHarness, scene: CatalogScene
) -> None:
    unpublished = _workflow(b"name: ghost\n")
    first = _workflow(b"name: lasagne\n")
    second = _workflow(b"name: other\n")
    name = CatalogLineageDisplayName("lasagne")
    harness.catalog.publish_revision(first)
    harness.catalog.publish_revision(second)
    first_lineage = harness.found(first, name, scene)

    assert harness.catalog.found_lineage(
        unpublished, CatalogLineageDisplayName("ghost"), scene.actor, scene.founded_at
    ) == CatalogAdmissionUnpublished(unpublished.revision_hash)
    assert harness.catalog.found_lineage(
        second, name, scene.actor, scene.founded_at
    ) == CatalogAdmissionNameHeld(name, first_lineage.lineage_id)
    assert _lineage_count(harness) == 1


def test_writer_refuses_bytes_owned_by_another_lineage_and_a_retired_target(
    harness: CatalogHarness, scene: CatalogScene
) -> None:
    first = _workflow(b"name: pasta\n")
    second = _workflow(b"name: lasagne\n")
    harness.catalog.publish_revision(first)
    harness.catalog.publish_revision(second)
    first_lineage = harness.found(first, CatalogLineageDisplayName("pasta"), scene)
    second_lineage = harness.found(second, CatalogLineageDisplayName("lasagne"), scene)

    assert harness.catalog.admit_member(
        second_lineage.lineage_id,
        first,
        CatalogLineageDisplayName("shared"),
        scene.actor,
        scene.admitted_at,
    ) == CatalogAdmissionRevisionOwned(first.revision_hash, first_lineage.lineage_id)

    harness.retire(first_lineage, scene)
    later = _workflow(b"name: next\n")
    harness.catalog.publish_revision(later)
    assert harness.catalog.admit_member(
        first_lineage.lineage_id,
        later,
        CatalogLineageDisplayName("next"),
        scene.actor,
        scene.admitted_at,
    ) == CatalogAdmissionRetired(first_lineage.lineage_id)


def test_founding_a_retired_lineage_again_is_refused_and_writes_nothing(
    harness: CatalogHarness, scene: CatalogScene
) -> None:
    founding = _workflow(b"name: pasta\n")
    display_name = CatalogLineageDisplayName("pasta")
    harness.catalog.publish_revision(founding)
    lineage = harness.found(founding, display_name, scene)
    harness.retire(lineage, scene)
    before = _catalog_snapshot(harness)

    assert harness.catalog.found_lineage(
        founding, display_name, scene.actor, scene.founded_at
    ) == CatalogAdmissionRetired(lineage.lineage_id)
    assert _catalog_snapshot(harness) == before


@pytest.mark.parametrize("held_name", ["historical", "current"])
def test_a_retired_lineage_keeps_holding_every_name_it_ever_carried(
    harness: CatalogHarness, scene: CatalogScene, held_name: str
) -> None:
    founding = _workflow(b"name: pasta\n")
    later = _workflow(b"name: lasagne\n")
    historical = CatalogLineageDisplayName("pasta")
    current = CatalogLineageDisplayName("lasagne")
    harness.catalog.publish_revision(founding)
    harness.catalog.publish_revision(later)
    retired_lineage = harness.found(founding, historical, scene)
    harness.admit(retired_lineage, later, current, scene)
    harness.retire(retired_lineage, scene)
    claimed = historical if held_name == "historical" else current
    successor = _workflow(b"name: successor\n")
    harness.catalog.publish_revision(successor)
    before = _catalog_snapshot(harness)

    assert harness.catalog.found_lineage(
        successor, claimed, scene.actor, scene.founded_at
    ) == CatalogAdmissionNameHeld(claimed, retired_lineage.lineage_id)
    assert _catalog_snapshot(harness) == before


def _refuse_every_alias_insert(harness: CatalogHarness) -> None:
    with harness.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER refuse_alias_insert "
            "BEFORE INSERT ON catalog_lineage_aliases "
            "BEGIN SELECT RAISE(ABORT, 'injected alias failure'); END"
        )


@pytest.mark.parametrize("write", ["founding", "admission"])
def test_a_failed_alias_write_leaves_the_whole_catalog_untouched(
    harness: CatalogHarness, scene: CatalogScene, write: str
) -> None:
    standing = _workflow(b"name: pasta\n")
    open_lineage_founding = _workflow(b"name: lasagne\n")
    admitted = _workflow(b"name: lasagne two\n")
    for revision in (standing, open_lineage_founding, admitted):
        harness.catalog.publish_revision(revision)
    retired_lineage = harness.found(standing, CatalogLineageDisplayName("pasta"), scene)
    open_lineage = harness.found(
        open_lineage_founding, CatalogLineageDisplayName("lasagne"), scene
    )
    harness.retire(retired_lineage, scene)
    _refuse_every_alias_insert(harness)
    before = _catalog_snapshot(harness)
    assert all(before[table] for table in before)

    if write == "founding":
        candidate = _workflow(b"name: carbonara\n")
        harness.catalog.publish_revision(candidate)
        result = harness.catalog.found_lineage(
            candidate,
            CatalogLineageDisplayName("carbonara"),
            scene.actor,
            scene.founded_at,
        )
    else:
        result = harness.catalog.admit_member(
            open_lineage.lineage_id,
            admitted,
            CatalogLineageDisplayName("lasagne-two"),
            scene.actor,
            scene.admitted_at,
        )

    assert isinstance(result, DurableWriteUnavailable | DurableStateCorrupt)
    assert _catalog_snapshot(harness) == before
