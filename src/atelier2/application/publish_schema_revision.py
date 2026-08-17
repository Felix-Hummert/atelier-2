"""Publish exact schema bytes through the catalog, or name why they are not a schema.

The store already writes any published kind. This use-case is the missing first
act for `schema`: the closed profile must accept the bytes before a row exists,
so a document that is not a schema writes nothing and carries the refusal the
profile already named.
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
    refusal: SchemaRefused


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
    result = registry.publish_revision(PublishedRevision(RevisionKind.SCHEMA, document))
    match result:
        case PublishedRevisionCreated(revision):
            return SchemaPublicationCreated(revision)
        case PublishedRevisionExisting(revision):
            return SchemaPublicationExisting(revision)
        case PublishedRevisionCollision():
            return SchemaPublicationCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
