from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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


type ResolvePublishedRevisionResult = PublishedRevisionFound | PublishedRevisionMissing


class PublishedRevisionRegistry(Protocol):
    """The registries a versioned reference of a V3 document resolves against.

    Resolution is lineage-free, and that is a named decision rather than an
    oversight. ADR 0007 is PROPOSED, not accepted: under it a reference reads
    `{ref: <lineage id>, revision: <revision hash>}` and binding calls
    `resolve_reference(kind, lineage_id, revision_hash)`, which proves the revision
    is an admitted member of that lineage. No lineage, membership or admission
    exists in this repository yet, so building against that operation would be
    building against a record nobody has accepted.

    Until it exists, a reference binds by the kind it is read under and the exact
    revision hash it pins, and the authored `ref` travels into the run
    configuration's hashed identity without proving membership. That is a visible
    debt with one successor: when lineages land, this port gains
    `resolve_reference` and this is the single call site that moves.
    """

    def publish_revision(
        self, revision: PublishedRevision
    ) -> PublishRevisionResult: ...

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult: ...
