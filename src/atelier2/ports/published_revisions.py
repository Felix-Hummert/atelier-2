from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, TypeGuard

from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogAdmissionExisting,
    CatalogAdmissionKindMismatch,
    CatalogAdmissionLineageMissing,
    CatalogAdmissionNameHeld,
    CatalogAdmissionRetired,
    CatalogAdmissionRevisionOwned,
    CatalogAdmissionUnpublished,
    CatalogLineageDisplayName,
    CatalogLineageFounded,
    CatalogLineageId,
    CatalogLineageIdMismatch,
    CatalogLineageQuery,
    CatalogMemberAdmitted,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


@dataclass(frozen=True)
class PublishedRevisionCreated:
    revision: PublishedRevision


@dataclass(frozen=True)
class PublishedRevisionExisting:
    revision: PublishedRevision


@dataclass(frozen=True)
class PublishedRevisionCollision:
    pass


type PublishRevisionResult = (
    PublishedRevisionCreated
    | PublishedRevisionExisting
    | PublishedRevisionCollision
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class PublishedRevisionFound:
    revision: PublishedRevision


@dataclass(frozen=True)
class PublishedRevisionMissing:
    pass


type CatalogReferenceLookup = PublishedRevisionFound | PublishedRevisionMissing
"""What `CatalogResolver.resolve_reference` answers; its store failures still raise."""
type ResolvePublishedRevisionResult = (
    PublishedRevisionFound
    | PublishedRevisionMissing
    | PublishedRevisionsUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class PublishedRevisionPage:
    """One hash-ordered page of the revisions published under one kind."""

    revisions: tuple[PublishedRevision, ...]
    next_after: PublishedRevisionHash | None


@dataclass(frozen=True)
class PublishedRevisionsUnavailable:
    detail: str | None = None


type ListPublishedRevisionsResult = (
    PublishedRevisionPage | PublishedRevisionsUnavailable | DurableStateCorrupt
)


type CatalogRevisionPosition = int | Literal["head"]


def is_catalog_revision_position(
    position: object,
) -> TypeGuard[CatalogRevisionPosition]:
    return (type(position) is str and position == "head") or (
        type(position) is int and position >= 1
    )


@dataclass(frozen=True)
class CatalogNameFound:
    lineage_id: CatalogLineageId
    revision_hash: PublishedRevisionHash
    revision_number: int
    current_display_name: CatalogLineageDisplayName
    retired: bool

    def __post_init__(self) -> None:
        if not isinstance(self.lineage_id, CatalogLineageId):
            raise TypeError("a catalog name result requires a typed lineage id")
        if not isinstance(self.revision_hash, PublishedRevisionHash):
            raise TypeError("a catalog name result requires a typed revision hash")
        if type(self.revision_number) is not int or self.revision_number < 1:
            raise ValueError("a catalog revision number must be a positive integer")
        if not isinstance(self.current_display_name, CatalogLineageDisplayName):
            raise TypeError("a catalog name result requires a typed display name")
        if type(self.retired) is not bool:
            raise TypeError("catalog retirement state must be true or false")


@dataclass(frozen=True)
class CatalogNameMissing:
    query: CatalogLineageQuery
    position: CatalogRevisionPosition

    def __post_init__(self) -> None:
        if not isinstance(self.query, CatalogLineageId | CatalogLineageDisplayName):
            raise TypeError("a catalog name query must be a lineage id or display name")
        if not is_catalog_revision_position(self.position):
            raise ValueError("a catalog position must be head or a positive integer")


type ResolveCatalogNameResult = CatalogNameFound | CatalogNameMissing


type FoundCatalogLineageResult = (
    CatalogLineageFounded
    | CatalogAdmissionExisting
    | CatalogLineageIdMismatch
    | CatalogAdmissionUnpublished
    | CatalogAdmissionNameHeld
    | CatalogAdmissionRevisionOwned
    | CatalogAdmissionRetired
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


type AdmitCatalogMemberResult = (
    CatalogMemberAdmitted
    | CatalogAdmissionExisting
    | CatalogLineageIdMismatch
    | CatalogAdmissionUnpublished
    | CatalogAdmissionNameHeld
    | CatalogAdmissionRevisionOwned
    | CatalogAdmissionRetired
    | CatalogAdmissionLineageMissing
    | CatalogAdmissionKindMismatch
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class CatalogLineageRetired:
    lineage_id: CatalogLineageId


@dataclass(frozen=True)
class CatalogRetirementExisting:
    lineage_id: CatalogLineageId


type RetireCatalogLineageResult = (
    CatalogLineageRetired
    | CatalogRetirementExisting
    | CatalogAdmissionLineageMissing
    | CatalogLineageIdMismatch
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class PublishedRevisionResolver(Protocol):
    """Lineage-free lookup of exact immutable published bytes."""

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult: ...


class PublishedRevisionRegistry(PublishedRevisionResolver, Protocol):
    """Publication and lineage-free lookup of exact immutable revisions.

    ADR 0007 keeps this lookup for callers whose immutable input already pins a
    revision without claiming lineage membership. A declared V3 reference instead
    uses `CatalogResolver.resolve_reference` once the catalog store implements it.
    """

    def publish_revision(
        self, revision: PublishedRevision
    ) -> PublishRevisionResult: ...


class PublishedRevisionListing(Protocol):
    """Browsing what one kind holds, which neither publishing nor pinning asks.

    It stays apart from `PublishedRevisionRegistry` because the two answer
    opposite questions: a resolver is given the hash it wants, while this one
    exists for a reader who has no hash yet -- the operator opening the catalog
    to see what is there at all.
    """

    def list_revisions(
        self, kind: RevisionKind, after: PublishedRevisionHash | None, limit: int
    ) -> ListPublishedRevisionsResult: ...


class CatalogAdmissions(Protocol):
    """The two catalog writes ADR 0007 Decision 3 names as its own commands.

    Publication and admission are two acts, so the port that admits is not the
    port that publishes. Founding is the first admission of a lineage and takes
    the name; every later one joins a lineage that already holds it.
    """

    def found_lineage(
        self,
        revision: PublishedRevision,
        display_name: CatalogLineageDisplayName,
        actor: CatalogActor,
        activated_at: CatalogActivatedAt,
        claimed_lineage_id: CatalogLineageId | None = None,
    ) -> FoundCatalogLineageResult: ...

    def admit_member(
        self,
        lineage_id: CatalogLineageId,
        revision: PublishedRevision,
        display_name: CatalogLineageDisplayName,
        actor: CatalogActor,
        activated_at: CatalogActivatedAt,
    ) -> AdmitCatalogMemberResult: ...


class CatalogResolver(PublishedRevisionResolver, Protocol):
    """The three catalog reads fixed by ADR 0007's resolution contract.

    Exact reference binding proves admitted membership and returns immutable
    revision bytes. Name lookup is authoring-only and may project mutable current
    display and retirement state; neither enters the reference result.
    """

    def resolve_reference(
        self,
        kind: RevisionKind,
        lineage_id: CatalogLineageId,
        revision_hash: PublishedRevisionHash,
    ) -> CatalogReferenceLookup: ...

    def resolve_name(
        self,
        kind: RevisionKind,
        lineage_id_or_name: CatalogLineageQuery,
        position: CatalogRevisionPosition,
    ) -> ResolveCatalogNameResult: ...
