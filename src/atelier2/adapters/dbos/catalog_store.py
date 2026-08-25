from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.schema import (
    catalog_lineage_aliases,
    catalog_lineage_members,
    catalog_lineage_retirements,
    catalog_lineages,
    published_revisions,
    workflow_revisions,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
    CatalogLineageDisplayName,
    CatalogLineageId,
    CatalogRetirementState,
)
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.published_revisions import (
    AdmitCatalogMemberResult,
    CatalogAdmissionExisting,
    CatalogAdmissionKindMismatch,
    CatalogAdmissionLineageMissing,
    CatalogAdmissionNameHeld,
    CatalogAdmissionRetired,
    CatalogAdmissionRevisionOwned,
    CatalogAdmissionUnpublished,
    CatalogLineageFounded,
    CatalogLineageIdMismatch,
    CatalogLineageQuery,
    CatalogLineageRetired,
    CatalogMemberAdmitted,
    CatalogNameFound,
    CatalogNameMissing,
    CatalogReferenceLookup,
    CatalogRetirementExisting,
    CatalogRevisionPosition,
    FoundCatalogLineageResult,
    ListPublishedRevisionsResult,
    PublishedRevisionCollision,
    PublishedRevisionCreated,
    PublishedRevisionExisting,
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionPage,
    PublishedRevisionsUnavailable,
    PublishRevisionResult,
    ResolveCatalogNameResult,
    ResolvePublishedRevisionResult,
    RetireCatalogLineageResult,
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


def _next_activation_number(
    connection: sa.Connection, table: sa.Table, lineage_id: str
) -> int:
    current = connection.scalar(
        sa.select(sa.func.max(table.c.activation_number)).where(
            table.c.lineage_id == lineage_id
        )
    )
    return 1 if current is None else int(current) + 1


def _published(
    connection: sa.Connection, revision: PublishedRevision
) -> PublishedRevision | None:
    record = (
        connection.execute(
            sa.select(published_revisions).where(
                published_revisions.c.kind == revision.kind.value,
                published_revisions.c.revision_hash == revision.revision_hash.value,
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    return published_revision_from_record(record)


def _workflow_publication(
    connection: sa.Connection, revision: PublishedRevision
) -> PublishedRevision | None:
    if revision.kind is not RevisionKind.WORKFLOW:
        return None
    record = (
        connection.execute(
            sa.select(workflow_revisions).where(
                workflow_revisions.c.revision_hash == revision.revision_hash.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        return None
    stored = PublishedRevision(RevisionKind.WORKFLOW, bytes(record["document"]))
    if stored.revision_hash.value != record["revision_hash"]:
        raise ValueError("durable workflow revision hash disagrees with its bytes")
    if stored != revision:
        raise ValueError("handed workflow revision disagrees with its published bytes")
    return stored


def _record_publication(
    connection: sa.Connection, revision: PublishedRevision
) -> PublishedRevision | None:
    """The catalog FK needs published_revisions; reuse the existing publication.

    A workflow already published through workflow_revisions keeps that hash.
    This writes the same bytes under the same hash; it is not a second identity.
    """

    existing = _published(connection, revision)
    if existing is not None:
        if existing != revision:
            raise ValueError("handed revision disagrees with catalog published bytes")
        return existing
    workflow = _workflow_publication(connection, revision)
    if workflow is None:
        return None
    connection.execute(
        published_revisions.insert().values(
            kind=workflow.kind.value,
            revision_hash=workflow.revision_hash.value,
            document=workflow.document,
        )
    )
    return workflow


def _lineage_record(
    connection: sa.Connection, lineage_id: CatalogLineageId
) -> Mapping[Any, Any] | None:
    return (
        connection.execute(
            sa.select(catalog_lineages).where(
                catalog_lineages.c.lineage_id == lineage_id.value
            )
        )
        .mappings()
        .one_or_none()
    )


def _derived_lineage_or_mismatch(
    record: Mapping[Any, Any],
) -> CatalogLineage | CatalogLineageIdMismatch:
    lineage = CatalogLineage(
        RevisionKind(str(record["kind"])),
        PublishedRevisionHash(str(record["founding_revision_hash"])),
    )
    stored = CatalogLineageId(str(record["lineage_id"]))
    if lineage.lineage_id != stored:
        return CatalogLineageIdMismatch(stored, lineage.lineage_id)
    return lineage


def _name_holder(
    connection: sa.Connection,
    kind: RevisionKind,
    display_name: CatalogLineageDisplayName,
    *,
    except_lineage_id: CatalogLineageId | None = None,
) -> CatalogLineageId | None:
    statement = (
        sa.select(catalog_lineage_aliases.c.lineage_id)
        .select_from(
            catalog_lineage_aliases.join(
                catalog_lineages,
                catalog_lineages.c.lineage_id == catalog_lineage_aliases.c.lineage_id,
            )
        )
        .where(
            catalog_lineage_aliases.c.name == display_name.value,
            catalog_lineages.c.kind == kind.value,
        )
        .distinct()
    )
    if except_lineage_id is not None:
        statement = statement.where(
            catalog_lineage_aliases.c.lineage_id != except_lineage_id.value
        )
    holders = connection.execute(statement).scalars().all()
    if not holders:
        return None
    return CatalogLineageId(str(holders[0]))


def revision_owner(
    connection: sa.Connection, kind: RevisionKind, revision_hash: PublishedRevisionHash
) -> CatalogLineageId | None:
    """The lineage one published revision belongs to, read through one connection.

    Public because a caller outside the catalog needs it: a start reads the
    served project's occupancy, and an occupancy is keyed by lineage. It asks
    by revision rather than by name because a document that carries no display
    name would otherwise be skipped in silence.
    """
    owner = connection.scalar(
        sa.select(catalog_lineage_members.c.lineage_id)
        .select_from(
            catalog_lineage_members.join(
                catalog_lineages,
                catalog_lineages.c.lineage_id == catalog_lineage_members.c.lineage_id,
            )
        )
        .where(
            catalog_lineage_members.c.revision_hash == revision_hash.value,
            catalog_lineages.c.kind == kind.value,
        )
    )
    if owner is None:
        return None
    return CatalogLineageId(str(owner))


def _member(
    connection: sa.Connection,
    lineage_id: CatalogLineageId,
    revision_hash: PublishedRevisionHash,
) -> Mapping[Any, Any] | None:
    return (
        connection.execute(
            sa.select(catalog_lineage_members).where(
                catalog_lineage_members.c.lineage_id == lineage_id.value,
                catalog_lineage_members.c.revision_hash == revision_hash.value,
            )
        )
        .mappings()
        .one_or_none()
    )


def _current_display_name(
    connection: sa.Connection, lineage_id: CatalogLineageId
) -> CatalogLineageDisplayName:
    name = connection.scalar(
        sa.select(catalog_lineage_aliases.c.name)
        .where(catalog_lineage_aliases.c.lineage_id == lineage_id.value)
        .order_by(catalog_lineage_aliases.c.activation_number.desc())
        .limit(1)
    )
    if name is None:
        raise ValueError("catalog lineage has no alias history")
    return CatalogLineageDisplayName(str(name))


def _is_retired(connection: sa.Connection, lineage_id: CatalogLineageId) -> bool:
    return (
        connection.scalar(
            sa.select(sa.func.count())
            .select_from(catalog_lineage_retirements)
            .where(catalog_lineage_retirements.c.lineage_id == lineage_id.value)
        )
        or 0
    ) > 0


def _append_alias(
    connection: sa.Connection,
    lineage_id: CatalogLineageId,
    display_name: CatalogLineageDisplayName,
    actor: CatalogActor,
    activated_at: CatalogActivatedAt,
) -> None:
    connection.execute(
        catalog_lineage_aliases.insert().values(
            lineage_id=lineage_id.value,
            activation_number=_next_activation_number(
                connection, catalog_lineage_aliases, lineage_id.value
            ),
            name=display_name.value,
            actor=actor.value,
            activated_at=activated_at.value,
        )
    )


def _append_member(
    connection: sa.Connection,
    lineage_id: CatalogLineageId,
    revision: PublishedRevision,
) -> int:
    current = connection.scalar(
        sa.select(sa.func.max(catalog_lineage_members.c.revision_number)).where(
            catalog_lineage_members.c.lineage_id == lineage_id.value
        )
    )
    revision_number = 1 if current is None else int(current) + 1
    connection.execute(
        catalog_lineage_members.insert().values(
            lineage_id=lineage_id.value,
            revision_number=revision_number,
            revision_hash=revision.revision_hash.value,
        )
    )
    return revision_number


class DbosCatalogStore:
    """Published revisions and admitted named lineages over the V12 catalog tables."""

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
        try:
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
        except (OperationalError, PoolTimeoutError):
            return PublishedRevisionsUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def list_revisions(
        self, kind: RevisionKind, after: PublishedRevisionHash | None, limit: int
    ) -> ListPublishedRevisionsResult:
        if type(limit) is not int or not 1 <= limit <= MAXIMUM_PAGE_ITEMS:
            raise ValueError(
                f"revision page limit must be an integer from 1 to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._engine.connect() as connection:
                statement = sa.select(published_revisions).where(
                    published_revisions.c.kind == kind.value
                )
                if after is not None:
                    statement = statement.where(
                        published_revisions.c.revision_hash > after.value
                    )
                records = tuple(
                    connection.execute(
                        statement.order_by(published_revisions.c.revision_hash).limit(
                            limit + 1
                        )
                    ).mappings()
                )
            page = records[:limit]
            revisions = tuple(published_revision_from_record(record) for record in page)
            next_after = (
                revisions[-1].revision_hash
                if len(records) > limit and revisions
                else None
            )
            return PublishedRevisionPage(revisions, next_after)
        except (OperationalError, PoolTimeoutError):
            return PublishedRevisionsUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def resolve_reference(
        self,
        kind: RevisionKind,
        lineage_id: CatalogLineageId,
        revision_hash: PublishedRevisionHash,
    ) -> CatalogReferenceLookup:
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

    def found_lineage(
        self,
        revision: PublishedRevision,
        display_name: CatalogLineageDisplayName,
        actor: CatalogActor,
        activated_at: CatalogActivatedAt,
        claimed_lineage_id: CatalogLineageId | None = None,
    ) -> FoundCatalogLineageResult:
        lineage = CatalogLineage(revision.kind, revision.revision_hash)
        if claimed_lineage_id is not None and claimed_lineage_id != lineage.lineage_id:
            return CatalogLineageIdMismatch(claimed_lineage_id, lineage.lineage_id)
        try:
            with canonical_write_transaction(self._engine) as connection:
                if _record_publication(connection, revision) is None:
                    return CatalogAdmissionUnpublished(revision.revision_hash)
                existing = _lineage_record(connection, lineage.lineage_id)
                if existing is not None:
                    stored = _derived_lineage_or_mismatch(existing)
                    if isinstance(stored, CatalogLineageIdMismatch):
                        return stored
                    if _is_retired(connection, stored.lineage_id):
                        return CatalogAdmissionRetired(stored.lineage_id)
                    member = _member(
                        connection, lineage.lineage_id, revision.revision_hash
                    )
                    if member is None:
                        owner = revision_owner(
                            connection, revision.kind, revision.revision_hash
                        )
                        if owner is not None:
                            return CatalogAdmissionRevisionOwned(
                                revision.revision_hash, owner
                            )
                        raise ValueError(
                            "catalog founding lineage is missing its founding member"
                        )
                    return CatalogAdmissionExisting(
                        stored,
                        revision,
                        int(member["revision_number"]),
                        _current_display_name(connection, lineage.lineage_id),
                    )
                owner = revision_owner(
                    connection, revision.kind, revision.revision_hash
                )
                if owner is not None:
                    return CatalogAdmissionRevisionOwned(revision.revision_hash, owner)
                holder = _name_holder(connection, revision.kind, display_name)
                if holder is not None:
                    return CatalogAdmissionNameHeld(display_name, holder)
                connection.execute(
                    catalog_lineages.insert().values(
                        lineage_id=lineage.lineage_id.value,
                        kind=revision.kind.value,
                        founding_revision_hash=revision.revision_hash.value,
                    )
                )
                _append_member(connection, lineage.lineage_id, revision)
                _append_alias(
                    connection, lineage.lineage_id, display_name, actor, activated_at
                )
                return CatalogLineageFounded(lineage, revision, display_name)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def admit_member(
        self,
        lineage_id: CatalogLineageId,
        revision: PublishedRevision,
        display_name: CatalogLineageDisplayName,
        actor: CatalogActor,
        activated_at: CatalogActivatedAt,
    ) -> AdmitCatalogMemberResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                record = _lineage_record(connection, lineage_id)
                if record is None:
                    return CatalogAdmissionLineageMissing(lineage_id)
                lineage = _derived_lineage_or_mismatch(record)
                if isinstance(lineage, CatalogLineageIdMismatch):
                    return lineage
                if lineage.kind is not revision.kind:
                    return CatalogAdmissionKindMismatch(
                        lineage.lineage_id, lineage.kind, revision.kind
                    )
                if _is_retired(connection, lineage.lineage_id):
                    return CatalogAdmissionRetired(lineage.lineage_id)
                if _record_publication(connection, revision) is None:
                    return CatalogAdmissionUnpublished(revision.revision_hash)
                existing_member = _member(
                    connection, lineage.lineage_id, revision.revision_hash
                )
                if existing_member is not None:
                    return CatalogAdmissionExisting(
                        lineage,
                        revision,
                        int(existing_member["revision_number"]),
                        _current_display_name(connection, lineage.lineage_id),
                    )
                owner = revision_owner(
                    connection, revision.kind, revision.revision_hash
                )
                if owner is not None:
                    return CatalogAdmissionRevisionOwned(revision.revision_hash, owner)
                holder = _name_holder(
                    connection,
                    revision.kind,
                    display_name,
                    except_lineage_id=lineage.lineage_id,
                )
                if holder is not None:
                    return CatalogAdmissionNameHeld(display_name, holder)
                revision_number = _append_member(
                    connection, lineage.lineage_id, revision
                )
                _append_alias(
                    connection, lineage.lineage_id, display_name, actor, activated_at
                )
                return CatalogMemberAdmitted(
                    lineage, revision, revision_number, display_name
                )
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def retire_lineage(
        self,
        lineage_id: CatalogLineageId,
        state: CatalogRetirementState,
        actor: CatalogActor,
        activated_at: CatalogActivatedAt,
    ) -> RetireCatalogLineageResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                record = _lineage_record(connection, lineage_id)
                if record is None:
                    return CatalogAdmissionLineageMissing(lineage_id)
                lineage = _derived_lineage_or_mismatch(record)
                if isinstance(lineage, CatalogLineageIdMismatch):
                    return lineage
                if _is_retired(connection, lineage.lineage_id):
                    return CatalogRetirementExisting(lineage.lineage_id)
                connection.execute(
                    catalog_lineage_retirements.insert().values(
                        lineage_id=lineage.lineage_id.value,
                        activation_number=_next_activation_number(
                            connection,
                            catalog_lineage_retirements,
                            lineage.lineage_id.value,
                        ),
                        state=state.value,
                        actor=actor.value,
                        activated_at=activated_at.value,
                    )
                )
                return CatalogLineageRetired(lineage.lineage_id)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def resolve_name(
        self,
        kind: RevisionKind,
        lineage_id_or_name: CatalogLineageQuery,
        position: CatalogRevisionPosition,
    ) -> ResolveCatalogNameResult:
        with self._engine.connect() as connection:
            if isinstance(lineage_id_or_name, CatalogLineageId):
                record = _lineage_record(connection, lineage_id_or_name)
                if record is None:
                    return CatalogNameMissing(lineage_id_or_name, position)
                lineage = catalog_lineage_from_record(record)
                if lineage.kind is not kind:
                    return CatalogNameMissing(lineage_id_or_name, position)
            else:
                holders = (
                    connection.execute(
                        sa.select(catalog_lineage_aliases.c.lineage_id)
                        .select_from(
                            catalog_lineage_aliases.join(
                                catalog_lineages,
                                catalog_lineages.c.lineage_id
                                == catalog_lineage_aliases.c.lineage_id,
                            )
                        )
                        .where(
                            catalog_lineage_aliases.c.name == lineage_id_or_name.value,
                            catalog_lineages.c.kind == kind.value,
                        )
                        .distinct()
                    )
                    .scalars()
                    .all()
                )
                if len(holders) > 1:
                    raise ValueError(
                        "catalog name is held by more than one lineage of one kind"
                    )
                if not holders:
                    return CatalogNameMissing(lineage_id_or_name, position)
                record = _lineage_record(connection, CatalogLineageId(str(holders[0])))
                if record is None:
                    raise ValueError("catalog alias names a lineage that is missing")
                lineage = catalog_lineage_from_record(record)
            member_statement = sa.select(catalog_lineage_members).where(
                catalog_lineage_members.c.lineage_id == lineage.lineage_id.value
            )
            if position == "head":
                member_statement = member_statement.order_by(
                    catalog_lineage_members.c.revision_number.desc()
                ).limit(1)
            else:
                member_statement = member_statement.where(
                    catalog_lineage_members.c.revision_number == position
                )
            member = connection.execute(member_statement).mappings().one_or_none()
            if member is None:
                return CatalogNameMissing(lineage_id_or_name, position)
            revision_hash = PublishedRevisionHash(str(member["revision_hash"]))
            published = (
                connection.execute(
                    sa.select(published_revisions).where(
                        published_revisions.c.kind == kind.value,
                        published_revisions.c.revision_hash == revision_hash.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if published is None:
                raise ValueError(
                    "catalog lineage member names a published revision that is missing"
                )
            published_revision_from_record(published)
            return CatalogNameFound(
                lineage.lineage_id,
                revision_hash,
                int(member["revision_number"]),
                _current_display_name(connection, lineage.lineage_id),
                retired=_is_retired(connection, lineage.lineage_id),
            )
