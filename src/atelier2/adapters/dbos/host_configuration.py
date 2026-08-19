"""Durable home of the host configuration channel."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.schema import host_project_root_revisions
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.host_configuration import (
    HOST_CONFIGURATION_UNREADABLE,
    PROJECT_ROOT_MISSING,
    HostConfigurationUnreadable,
    ProjectId,
    ProjectRootMissing,
    ProjectRootRevision,
    ProjectRootRevisionConflict,
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
