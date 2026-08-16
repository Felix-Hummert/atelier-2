from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.schema import (
    catalog_lineage_members,
    catalog_lineages,
    published_revisions,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.catalog_v3 import CatalogLineage, CatalogLineageId
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.published_revisions import (
    CatalogLineageQuery,
    CatalogNameMissing,
    CatalogRevisionPosition,
    PublishedRevisionCollision,
    PublishedRevisionCreated,
    PublishedRevisionExisting,
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishRevisionResult,
    ResolveCatalogNameResult,
    ResolvePublishedRevisionResult,
)


def published_revision_from_record(record: Mapping[Any, Any]) -> PublishedRevision:
    revision = PublishedRevision(
        RevisionKind(str(record["kind"])),
        bytes(record["document"]),
    )
    if revision.revision_hash.value != record["revision_hash"]:
        raise ValueError("durable published revision hash disagrees with its bytes")
    return revision


def catalog_lineage_from_record(record: Mapping[Any, Any]) -> CatalogLineage:
    lineage = CatalogLineage(
        RevisionKind(str(record["kind"])),
        PublishedRevisionHash(str(record["founding_revision_hash"])),
    )
    if lineage.lineage_id.value != record["lineage_id"]:
        raise ValueError(
            "durable catalog lineage id disagrees with its founding identity"
        )
    return lineage


class DbosCatalogStore:
    """Published revisions and admitted lineage members over the thin V10 tables.

    V10 has no alias or retirement tables, so `resolve_name` cannot complete a
    `CatalogNameFound` and reports missing rather than inventing a display name
    or a retirement state.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def publish_revision(self, revision: PublishedRevision) -> PublishRevisionResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                existing = (
                    connection.execute(
                        sa.select(published_revisions).where(
                            published_revisions.c.kind == revision.kind.value,
                            published_revisions.c.revision_hash
                            == revision.revision_hash.value,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None:
                    durable = published_revision_from_record(existing)
                    if durable == revision:
                        return PublishedRevisionExisting(durable)
                    return PublishedRevisionCollision()
                connection.execute(
                    published_revisions.insert().values(
                        kind=revision.kind.value,
                        revision_hash=revision.revision_hash.value,
                        document=revision.document,
                    )
                )
                return PublishedRevisionCreated(revision)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        with self._engine.connect() as connection:
            record = (
                connection.execute(
                    sa.select(published_revisions).where(
                        published_revisions.c.kind == kind.value,
                        published_revisions.c.revision_hash == revision_hash.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if record is None:
            return PublishedRevisionMissing()
        return PublishedRevisionFound(published_revision_from_record(record))

    def resolve_reference(
        self,
        kind: RevisionKind,
        lineage_id: CatalogLineageId,
        revision_hash: PublishedRevisionHash,
    ) -> ResolvePublishedRevisionResult:
        with self._engine.connect() as connection:
            lineage_record = (
                connection.execute(
                    sa.select(catalog_lineages).where(
                        catalog_lineages.c.lineage_id == lineage_id.value
                    )
                )
                .mappings()
                .one_or_none()
            )
            if lineage_record is None:
                return PublishedRevisionMissing()
            lineage = catalog_lineage_from_record(lineage_record)
            if lineage.kind is not kind:
                return PublishedRevisionMissing()
            member = (
                connection.execute(
                    sa.select(catalog_lineage_members).where(
                        catalog_lineage_members.c.lineage_id
                        == lineage.lineage_id.value,
                        catalog_lineage_members.c.revision_hash == revision_hash.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if member is None:
                return PublishedRevisionMissing()
            revision_record = (
                connection.execute(
                    sa.select(published_revisions).where(
                        published_revisions.c.kind == kind.value,
                        published_revisions.c.revision_hash == revision_hash.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
        if revision_record is None:
            raise ValueError(
                "catalog lineage member names a published revision that is missing"
            )
        return PublishedRevisionFound(published_revision_from_record(revision_record))

    def resolve_name(
        self,
        kind: RevisionKind,
        lineage_id_or_name: CatalogLineageQuery,
        position: CatalogRevisionPosition,
    ) -> ResolveCatalogNameResult:
        del kind
        return CatalogNameMissing(lineage_id_or_name, position)
