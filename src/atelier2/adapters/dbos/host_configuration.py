"""Durable home of the host configuration channel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.schema import (
    host_occupancy_bindings,
    host_occupancy_revisions,
    host_project_root_revisions,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.agents import AgentConfigurationRevisionHash, AgentRole
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import (
    HOST_CONFIGURATION_UNREADABLE,
    PROJECT_ROOT_MISSING,
    HostConfigurationUnreadable,
    OccupancyBinding,
    OccupancyRevision,
    OccupancyRevisionConflict,
    ProjectId,
    ProjectRootMissing,
    ProjectRootRevision,
    ProjectRootRevisionConflict,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.host_configuration import (
    HostConfigurationReadUnavailable,
    LatestOccupancyResult,
    LatestProjectRootResult,
    OccupancyRevisionCollision,
    OccupancyRevisionCreated,
    OccupancyRevisionExisting,
    PublishOccupancyResult,
)
from atelier2.ports.host_configuration import (
    OccupancyRevisionConflict as PortOccupancyRevisionConflict,
)


def project_root_revision_from_record(record: Mapping[Any, Any]) -> ProjectRootRevision:
    revision = ProjectRootRevision(
        ProjectId(str(record["project_id"])),
        int(record["revision_number"]),
        Path(str(record["root_path"])),
    )
    if revision.revision_hash.value != record["revision_hash"]:
        raise HostConfigurationUnreadable(
            f"{HOST_CONFIGURATION_UNREADABLE}: a stored project-root hash "
            "disagrees with its fields"
        )
    return revision


def latest_project_root_revision(
    engine: Engine, project_id: ProjectId
) -> ProjectRootRevision | None:
    try:
        with engine.connect() as connection:
            record = (
                connection.execute(
                    sa.select(host_project_root_revisions)
                    .where(host_project_root_revisions.c.project_id == project_id.value)
                    .order_by(host_project_root_revisions.c.revision_number.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
    except (OperationalError, PoolTimeoutError, DatabaseError) as error:
        raise HostConfigurationUnreadable(
            f"{HOST_CONFIGURATION_UNREADABLE}: the project-root channel "
            "could not be read"
        ) from error
    if record is None:
        return None
    return project_root_revision_from_record(record)


def project_root_for(engine: Engine, project_id: ProjectId) -> Path:
    revision = latest_project_root_revision(engine, project_id)
    if revision is None:
        raise ProjectRootMissing(
            f"{PROJECT_ROOT_MISSING}: project {project_id.value!r} has no "
            "configured root"
        )
    return revision.root_path


def publish_project_root_revision(
    engine: Engine, revision: ProjectRootRevision
) -> ProjectRootRevision:
    try:
        with canonical_write_transaction(engine) as connection:
            keyed = (
                connection.execute(
                    sa.select(host_project_root_revisions).where(
                        host_project_root_revisions.c.project_id
                        == revision.project_id.value,
                        host_project_root_revisions.c.revision_number
                        == revision.revision_number,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if keyed is not None:
                durable = project_root_revision_from_record(keyed)
                if durable == revision:
                    return durable
                raise ProjectRootRevisionConflict(
                    "project-root-revision-conflict: "
                    f"{revision.project_id.value!r} revision "
                    f"{revision.revision_number} already exists"
                )
            hashed = (
                connection.execute(
                    sa.select(host_project_root_revisions).where(
                        host_project_root_revisions.c.revision_hash
                        == revision.revision_hash.value
                    )
                )
                .mappings()
                .one_or_none()
            )
            if hashed is not None:
                durable = project_root_revision_from_record(hashed)
                if durable == revision:
                    return durable
                raise HostConfigurationUnreadable(
                    f"{HOST_CONFIGURATION_UNREADABLE}: project-root revision "
                    f"{revision.revision_hash.value} already names other fields"
                )
            connection.execute(
                host_project_root_revisions.insert().values(
                    revision_hash=revision.revision_hash.value,
                    project_id=revision.project_id.value,
                    revision_number=revision.revision_number,
                    root_path=str(revision.root_path),
                )
            )
            return revision
    except (
        ProjectRootRevisionConflict,
        HostConfigurationUnreadable,
        ProjectRootMissing,
    ):
        raise
    except (OperationalError, PoolTimeoutError, DatabaseError) as error:
        raise HostConfigurationUnreadable(
            f"{HOST_CONFIGURATION_UNREADABLE}: the project-root channel "
            "could not be written"
        ) from error


def append_project_root(
    engine: Engine, project_id: ProjectId, root_path: Path
) -> ProjectRootRevision:
    latest = latest_project_root_revision(engine, project_id)
    candidate = ProjectRootRevision(
        project_id,
        1 if latest is None else latest.revision_number + 1,
        root_path,
    )
    if latest is not None and latest.root_path == candidate.root_path:
        return latest
    return publish_project_root_revision(engine, candidate)


def occupancy_revision_from_records(
    header: Mapping[Any, Any], bindings: Sequence[Mapping[Any, Any]]
) -> OccupancyRevision:
    revision = OccupancyRevision(
        ProjectId(str(header["project_id"])),
        CatalogLineageId(str(header["lineage_id"])),
        int(header["revision_number"]),
        tuple(
            OccupancyBinding(
                AgentRole(str(record["role"])),
                AgentConfigurationRevisionHash(
                    str(record["agent_configuration_revision_hash"])
                ),
            )
            for record in bindings
        ),
    )
    if revision.revision_hash.value != header["revision_hash"]:
        raise HostConfigurationUnreadable(
            f"{HOST_CONFIGURATION_UNREADABLE}: a stored occupancy hash "
            "disagrees with its fields"
        )
    return revision


def latest_occupancy_revision(
    engine: Engine, project_id: ProjectId, lineage_id: CatalogLineageId
) -> OccupancyRevision | None:
    try:
        with engine.connect() as connection:
            header = (
                connection.execute(
                    sa.select(host_occupancy_revisions)
                    .where(
                        host_occupancy_revisions.c.project_id == project_id.value,
                        host_occupancy_revisions.c.lineage_id == lineage_id.value,
                    )
                    .order_by(host_occupancy_revisions.c.revision_number.desc())
                    .limit(1)
                )
                .mappings()
                .one_or_none()
            )
            if header is None:
                return None
            bindings = (
                connection.execute(
                    sa.select(host_occupancy_bindings)
                    .where(
                        host_occupancy_bindings.c.revision_hash
                        == header["revision_hash"]
                    )
                    .order_by(host_occupancy_bindings.c.role)
                )
                .mappings()
                .all()
            )
    except (OperationalError, PoolTimeoutError, DatabaseError) as error:
        raise HostConfigurationUnreadable(
            f"{HOST_CONFIGURATION_UNREADABLE}: the occupancy channel could not be read"
        ) from error
    return occupancy_revision_from_records(header, bindings)


def _occupancy_bindings_for(
    connection: Connection, revision_hash: str
) -> Sequence[Mapping[Any, Any]]:
    return (
        connection.execute(
            sa.select(host_occupancy_bindings).where(
                host_occupancy_bindings.c.revision_hash == revision_hash
            )
        )
        .mappings()
        .all()
    )


def _write_occupancy_revision(
    connection: Connection, revision: OccupancyRevision
) -> OccupancyRevisionCreated | OccupancyRevisionExisting:
    keyed = (
        connection.execute(
            sa.select(host_occupancy_revisions).where(
                host_occupancy_revisions.c.project_id == revision.project_id.value,
                host_occupancy_revisions.c.lineage_id == revision.lineage_id.value,
                host_occupancy_revisions.c.revision_number == revision.revision_number,
            )
        )
        .mappings()
        .one_or_none()
    )
    if keyed is not None:
        durable = occupancy_revision_from_records(
            keyed, _occupancy_bindings_for(connection, str(keyed["revision_hash"]))
        )
        if durable == revision:
            return OccupancyRevisionExisting(durable)
        raise OccupancyRevisionConflict(
            "occupancy-revision-conflict: "
            f"{revision.project_id.value!r} lineage "
            f"{revision.lineage_id.value} revision "
            f"{revision.revision_number} already exists"
        )
    hashed = (
        connection.execute(
            sa.select(host_occupancy_revisions).where(
                host_occupancy_revisions.c.revision_hash == revision.revision_hash.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if hashed is not None:
        durable = occupancy_revision_from_records(
            hashed, _occupancy_bindings_for(connection, str(hashed["revision_hash"]))
        )
        if durable == revision:
            return OccupancyRevisionExisting(durable)
        raise HostConfigurationUnreadable(
            f"{HOST_CONFIGURATION_UNREADABLE}: occupancy revision "
            f"{revision.revision_hash.value} already names other fields"
        )
    connection.execute(
        host_occupancy_revisions.insert().values(
            revision_hash=revision.revision_hash.value,
            project_id=revision.project_id.value,
            lineage_id=revision.lineage_id.value,
            revision_number=revision.revision_number,
        )
    )
    if revision.bindings:
        connection.execute(
            host_occupancy_bindings.insert(),
            [
                {
                    "revision_hash": revision.revision_hash.value,
                    "role": binding.role.value,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash.value
                    ),
                }
                for binding in revision.bindings
            ],
        )
    return OccupancyRevisionCreated(revision)


def publish_occupancy_revision(
    engine: Engine, revision: OccupancyRevision
) -> OccupancyRevision:
    try:
        with canonical_write_transaction(engine) as connection:
            return _write_occupancy_revision(connection, revision).revision
    except (OccupancyRevisionConflict, HostConfigurationUnreadable):
        raise
    except (OperationalError, PoolTimeoutError, DatabaseError) as error:
        raise HostConfigurationUnreadable(
            f"{HOST_CONFIGURATION_UNREADABLE}: the occupancy channel "
            "could not be written"
        ) from error


class DbosHostConfigurationChannel:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def latest_project_root_revision(
        self, project_id: ProjectId
    ) -> LatestProjectRootResult:
        try:
            return latest_project_root_revision(self._engine, project_id)
        except HostConfigurationUnreadable as error:
            return HostConfigurationReadUnavailable(str(error))
        except (ValueError, RuntimeError):
            return DurableStateCorrupt()

    def latest_occupancy_revision(
        self, project_id: ProjectId, lineage_id: CatalogLineageId
    ) -> LatestOccupancyResult:
        try:
            return latest_occupancy_revision(self._engine, project_id, lineage_id)
        except HostConfigurationUnreadable as error:
            return HostConfigurationReadUnavailable(str(error))
        except (ValueError, RuntimeError):
            return DurableStateCorrupt()

    def publish_occupancy_revision(
        self, revision: OccupancyRevision
    ) -> PublishOccupancyResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                return _write_occupancy_revision(connection, revision)
        except OccupancyRevisionConflict:
            return PortOccupancyRevisionConflict()
        except HostConfigurationUnreadable:
            return OccupancyRevisionCollision()
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
