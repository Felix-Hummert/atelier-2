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
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.schemas_v3 import SchemaRefused, read_schema_document
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.published_revisions import (
    PublishedRevisionCollision,
    PublishedRevisionCreated,
    PublishedRevisionExisting,
    PublishedRevisionRegistry,
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
