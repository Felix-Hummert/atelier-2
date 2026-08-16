"""Pure catalog reference and authoring-name resolution decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.contracts.catalog_v3 import (
    CatalogLineageDisplayName,
    CatalogLineageId,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.ports.published_revisions import (
    CatalogLineageQuery,
    CatalogNameFound,
    CatalogResolver,
    CatalogRevisionPosition,
    PublishedRevisionFound,
    PublishedRevisionMissing,
    is_catalog_revision_position,
)
from atelier2.ports.published_revisions import (
    CatalogNameMissing as PortCatalogNameMissing,
)


@dataclass(frozen=True)
class CatalogReferenceResolved:
    revision: PublishedRevision


@dataclass(frozen=True)
class CatalogReferenceNonMember:
    lineage_id: CatalogLineageId
    revision_hash: PublishedRevisionHash


type CatalogReferenceResult = CatalogReferenceResolved | CatalogReferenceNonMember


@dataclass(frozen=True)
class CatalogNameResolved:
    lineage_id: CatalogLineageId
    revision: PublishedRevision
    revision_number: int
    current_display_name: CatalogLineageDisplayName


@dataclass(frozen=True)
class CatalogNameLineageRetired:
    lineage_id: CatalogLineageId
    current_display_name: CatalogLineageDisplayName


@dataclass(frozen=True)
class CatalogNameMissing:
    query: CatalogLineageQuery
    position: CatalogRevisionPosition


@dataclass(frozen=True)
class CatalogNameInvalidPosition:
    position: object


type CatalogNameResult = (
    CatalogNameResolved
    | CatalogNameLineageRetired
    | CatalogNameMissing
    | CatalogNameInvalidPosition
    | CatalogReferenceNonMember
)


def resolve_catalog_reference(
    kind: RevisionKind,
    lineage_id: CatalogLineageId,
    revision_hash: PublishedRevisionHash,
    catalog: CatalogResolver,
) -> CatalogReferenceResult:
    """Return exact admitted bytes or name the lineage/hash pair that is not bound."""
    match catalog.resolve_reference(kind, lineage_id, revision_hash):
        case PublishedRevisionFound(revision):
            if revision.kind is not kind or revision.revision_hash != revision_hash:
                return CatalogReferenceNonMember(lineage_id, revision_hash)
            return CatalogReferenceResolved(revision)
        case PublishedRevisionMissing():
            return CatalogReferenceNonMember(lineage_id, revision_hash)
        case _ as unreachable:
            assert_never(unreachable)


def resolve_catalog_name(
    kind: RevisionKind,
    lineage_id_or_name: CatalogLineageQuery,
    position: object,
    catalog: CatalogResolver,
) -> CatalogNameResult:
    """Resolve one authoring query to the exact bytes its lineage admits."""
    if not is_catalog_revision_position(position):
        return CatalogNameInvalidPosition(position)

    match catalog.resolve_name(kind, lineage_id_or_name, position):
        case CatalogNameFound(
            lineage_id=lineage_id,
            revision_hash=revision_hash,
            revision_number=revision_number,
            current_display_name=current_display_name,
            retired=retired,
        ):
            if retired:
                return CatalogNameLineageRetired(lineage_id, current_display_name)
            match resolve_catalog_reference(kind, lineage_id, revision_hash, catalog):
                case CatalogReferenceResolved(revision):
                    return CatalogNameResolved(
                        lineage_id,
                        revision,
                        revision_number,
                        current_display_name,
                    )
                case CatalogReferenceNonMember() as not_admitted:
                    return not_admitted
                case _ as unreachable:
                    assert_never(unreachable)
        case PortCatalogNameMissing(query=query, position=missing_position):
            return CatalogNameMissing(query, missing_position)
        case _ as unreachable:
            assert_never(unreachable)
