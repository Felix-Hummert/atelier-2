"""Publishing a schema revision: exact bytes in, the catalog's own write, hash out.

The catalog store already publishes any kind. This use-case is the door that
kind `schema` was missing: it reads the bytes against the one profile that
knows what a schema is, then asks the store that already owns the write. It
does not invent a second publication.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.schemas_v3 import SchemaRefused, read_schema_document
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.published_revisions import (
    PublishedRevisionCollision,
    PublishedRevisionCreated,
    PublishedRevisionExisting,
    PublishedRevisionFound,
    PublishedRevisionRegistry,
    PublishedRevisionResolver,
)


@dataclass(frozen=True)
class SchemaPublicationCreated:
    revision: PublishedRevision


@dataclass(frozen=True)
class SchemaPublicationExisting:
    revision: PublishedRevision


@dataclass(frozen=True)
class SchemaPublicationInvalid:
    verdict: SchemaRefused


@dataclass(frozen=True)
class SchemaPublicationCollision:
    pass


type PublishSchemaRevisionResult = (
    SchemaPublicationCreated
    | SchemaPublicationExisting
    | SchemaPublicationInvalid
    | SchemaPublicationCollision
    | WriteUnavailable
    | DurableStateCorrupt
)


def publish_schema_revision(
    document: bytes, registry: PublishedRevisionRegistry
) -> PublishSchemaRevisionResult:
    verdict = read_schema_document(document)
    if isinstance(verdict, SchemaRefused):
        return SchemaPublicationInvalid(verdict)
    revision = PublishedRevision(RevisionKind.SCHEMA, document)
    result = registry.publish_revision(revision)
    match result:
        case PublishedRevisionCreated(stored):
            return SchemaPublicationCreated(stored)
        case PublishedRevisionExisting(stored):
            return SchemaPublicationExisting(stored)
        case PublishedRevisionCollision():
            return SchemaPublicationCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


@dataclass(frozen=True)
class SchemaRevisionRead:
    revision: PublishedRevision


@dataclass(frozen=True)
class SchemaRevisionNotFound:
    pass


type GetSchemaRevisionResult = SchemaRevisionRead | SchemaRevisionNotFound


def get_schema_revision(
    revision_hash: PublishedRevisionHash, resolver: PublishedRevisionResolver
) -> GetSchemaRevisionResult:
    """The published bytes one `schema` reference pins, or that none do.

    `resolve` is lineage-free by design (ADR 0007): a `schema` revision is
    named by hash alone, never by a catalog name, so this read asks nothing
    more than a reference already carries. A hash published under another
    kind answers the same as one nobody published -- `schema-revisions`
    speaks for the `schema` kind alone, exactly as it publishes for it alone.
    """
    resolved = resolver.resolve(RevisionKind.SCHEMA, revision_hash)
    if not isinstance(resolved, PublishedRevisionFound):
        return SchemaRevisionNotFound()
    if resolved.revision.kind is not RevisionKind.SCHEMA:
        return SchemaRevisionNotFound()
    return SchemaRevisionRead(resolved.revision)
