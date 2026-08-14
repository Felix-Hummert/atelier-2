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

    Resolution is lineage-free on purpose: decision 0007's `resolve_reference` proves
    membership of a named lineage, and until lineages exist a reference binds by the
    kind it is read under and the exact revision hash it pins.
    """

    def publish_revision(
        self, revision: PublishedRevision
    ) -> PublishRevisionResult: ...

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult: ...
