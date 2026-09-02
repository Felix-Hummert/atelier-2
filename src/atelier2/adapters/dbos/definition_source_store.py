"""Where a registered definition source and its delivered paths are kept.

One configuration is one immutable row plus its ordered selection rows, and the
two are written inside the one canonical write transaction so a registration
can never stand with half its selections. Re-registering the configuration that
already stands changes nothing and says so -- whatever number it stands under,
because what is compared is the configuration and never the number it landed
with; a changed configuration of the same repository and ref appends the next
revision of that same source rather than founding a second one
(`#660` ruled line 5).

Reading intakes is what a write-free scan needs and all it needs: the highest
`intake_number` per path, which is what ADR 0007 calls the latest intake.
Writing an intake is the intake operation's own act and lives with it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.schema import (
    catalog_source_intakes,
    host_definition_source_revisions,
    host_definition_source_selections,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.definition_sources import (
    DefinitionSourceAccess,
    DefinitionSourceActor,
    DefinitionSourceConfiguration,
    DefinitionSourceId,
    DefinitionSourceKind,
    DefinitionSourceRevision,
    DefinitionSourceSelection,
    RepositoryLocation,
    RepositoryPath,
    RepositoryRef,
    SelectionPattern,
    SourceCommit,
    SourceIntake,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.ports.definition_sources import (
    DefinitionSourceFound,
    DefinitionSourceMissing,
    DefinitionSourceRegistered,
    DefinitionSourceUnchanged,
    ReadDefinitionSourceResult,
    ReadSourceIntakesResult,
    RegisterDefinitionSourceResult,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable

_FIRST_REVISION_NUMBER = 1
_FIRST_SELECTION_ORDINAL = 1


class DbosDefinitionSources:
    """The canonical store's answer for registered definition sources."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def register(
        self, configuration: DefinitionSourceConfiguration
    ) -> RegisterDefinitionSourceResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                standing = self._newest(connection, configuration.source_id)
                if standing is not None and standing.configuration == configuration:
                    return DefinitionSourceUnchanged(standing)
                appended = DefinitionSourceRevision(
                    configuration,
                    _FIRST_REVISION_NUMBER
                    if standing is None
                    else standing.revision_number + 1,
                )
                self._insert(connection, appended)
                return DefinitionSourceRegistered(appended)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, TypeError, DatabaseError):
            return DurableStateCorrupt()

    def read_source(self, source_id: DefinitionSourceId) -> ReadDefinitionSourceResult:
        try:
            with self._engine.connect() as connection:
                newest = self._newest(connection, source_id)
                if newest is None:
                    return DefinitionSourceMissing(source_id)
                return DefinitionSourceFound(newest)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, TypeError, DatabaseError):
            return DurableStateCorrupt()

    def latest_intakes(self, source_id: DefinitionSourceId) -> ReadSourceIntakesResult:
        try:
            with self._engine.connect() as connection:
                return self._latest_intakes(connection, source_id)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, TypeError, DatabaseError):
            return DurableStateCorrupt()

    def _newest(
        self, connection: Connection, source_id: DefinitionSourceId
    ) -> DefinitionSourceRevision | None:
        record = (
            connection.execute(
                sa.select(host_definition_source_revisions)
                .where(host_definition_source_revisions.c.source_id == source_id.value)
                .order_by(host_definition_source_revisions.c.revision_number.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if record is None:
            return None
        return _revision_from_records(
            record,
            connection.execute(
                sa.select(host_definition_source_selections)
                .where(
                    host_definition_source_selections.c.revision_hash
                    == record["revision_hash"]
                )
                .order_by(host_definition_source_selections.c.selection_ordinal)
            )
            .mappings()
            .all(),
        )

    def _insert(
        self, connection: Connection, revision: DefinitionSourceRevision
    ) -> None:
        connection.execute(
            host_definition_source_revisions.insert().values(
                revision_hash=revision.revision_hash.value,
                source_id=revision.configuration.source_id.value,
                revision_number=revision.revision_number,
                source_kind=revision.configuration.kind.value,
                repository_location=revision.configuration.location.value,
                repository_ref=revision.configuration.ref.value,
                access=revision.configuration.access.value,
                connected_by=revision.configuration.connected_by.value,
            )
        )
        connection.execute(
            host_definition_source_selections.insert(),
            [
                {
                    "revision_hash": revision.revision_hash.value,
                    "selection_ordinal": ordinal,
                    "path_pattern": selection.pattern.value,
                    "revision_kind": selection.kind.value,
                }
                for ordinal, selection in enumerate(
                    revision.configuration.selections,
                    start=_FIRST_SELECTION_ORDINAL,
                )
            ],
        )

    def _latest_intakes(
        self, connection: Connection, source_id: DefinitionSourceId
    ) -> Mapping[RepositoryPath, SourceIntake]:
        highest = (
            sa.select(
                catalog_source_intakes.c.source_path,
                sa.func.max(catalog_source_intakes.c.intake_number).label(
                    "intake_number"
                ),
            )
            .where(catalog_source_intakes.c.source_id == source_id.value)
            .group_by(catalog_source_intakes.c.source_path)
            .subquery()
        )
        records = (
            connection.execute(
                sa.select(catalog_source_intakes)
                .join(
                    highest,
                    sa.and_(
                        catalog_source_intakes.c.source_path == highest.c.source_path,
                        catalog_source_intakes.c.intake_number
                        == highest.c.intake_number,
                    ),
                )
                .where(catalog_source_intakes.c.source_id == source_id.value)
            )
            .mappings()
            .all()
        )
        return {
            intake.path: intake
            for intake in (_intake_from_record(record) for record in records)
        }


def _revision_from_records(
    record: Mapping[Any, Any], selections: Sequence[Mapping[Any, Any]]
) -> DefinitionSourceRevision:
    revision = DefinitionSourceRevision(
        DefinitionSourceConfiguration(
            DefinitionSourceKind(str(record["source_kind"])),
            RepositoryLocation(str(record["repository_location"])),
            RepositoryRef(str(record["repository_ref"])),
            DefinitionSourceAccess(str(record["access"])),
            DefinitionSourceActor(str(record["connected_by"])),
            tuple(
                DefinitionSourceSelection(
                    SelectionPattern(str(selection["path_pattern"])),
                    RevisionKind(str(selection["revision_kind"])),
                )
                for selection in selections
            ),
        ),
        int(str(record["revision_number"])),
    )
    if revision.revision_hash.value != record["revision_hash"]:
        raise ValueError("durable definition source hash disagrees with its fields")
    return revision


def _intake_from_record(record: Mapping[Any, Any]) -> SourceIntake:
    return SourceIntake(
        DefinitionSourceId(str(record["source_id"])),
        RepositoryPath(str(record["source_path"])),
        int(str(record["intake_number"])),
        RevisionKind(str(record["revision_kind"])),
        PublishedRevisionHash(str(record["revision_hash"])),
        SourceCommit(str(record["source_commit"])),
    )
