"""Durable home of the host configuration channel."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, NoSuchTableError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.schema import (
    host_model_registry_entries,
    host_model_registry_revisions,
    host_project_model_defaults,
    host_project_model_defaults_revisions,
    host_project_root_revisions,
    host_project_source_connection_revisions,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.agents import AgentConfigurationRevisionHash, ProviderId
from atelier2.contracts.host_configuration import (
    HOST_CONFIGURATION_UNREADABLE,
    PROJECT_ROOT_MISSING,
    ConnectionActor,
    HostConfigurationUnreadable,
    HostModelConfigurationSnapshot,
    HostModelRegistryRevisionHash,
    ModelRegistryBytesDisagree,
    ModelRegistryEntry,
    ModelRegistryEntrySource,
    ModelRegistryRevision,
    ModelRegistryRevisionConflict,
    ModelRegistryRevisionHashCollision,
    ProjectId,
    ProjectModelDefault,
    ProjectModelDefaultsBytesDisagree,
    ProjectModelDefaultsRevision,
    ProjectModelDefaultsRevisionConflict,
    ProjectModelDefaultsRevisionHashCollision,
    ProjectRootBytesDisagree,
    ProjectRootMissing,
    ProjectRootRevision,
    ProjectRootRevisionConflict,
    ProjectSourceConnectionBytesDisagree,
    ProjectSourceConnectionConflict,
    ProjectSourceConnectionHashCollision,
    ProjectSourceConnectionLifecycle,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    ProjectUnknown,
    ProviderModelCheck,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
    SourceReference,
)
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflows_v3 import RoleDifficulty
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.host_configuration import (
    HostConfigurationReadUnavailable,
    HostModelConfigurationSnapshotResult,
    LatestModelRegistriesResult,
    LatestModelRegistryResult,
    LatestProjectModelDefaultsResult,
    LatestProjectRootResult,
    LatestProjectSourceConnectionResult,
    LatestProjectSourceConnectionsResult,
    ModelRegistryRevisionCollision,
    ModelRegistryRevisionCreated,
    ModelRegistryRevisionExisting,
    ProjectModelDefaultsRevisionCollision,
    ProjectModelDefaultsRevisionCreated,
    ProjectModelDefaultsRevisionExisting,
    ProjectModelDefaultsRevisionInvalid,
    ProjectSourceConnectionRevisionCollision,
    ProjectSourceConnectionRevisionConflict,
    ProjectSourceConnectionRevisionCreated,
    ProjectSourceConnectionRevisionExisting,
    ProjectSourceCredentialDirectoryReferenced,
    ProjectSourceCredentialDirectoryReferenceResult,
    ProjectSourceCredentialDirectoryUnreferenced,
    PublishModelRegistryResult,
    PublishProjectModelDefaultsResult,
    PublishProjectSourceConnectionResult,
)
from atelier2.ports.host_configuration import (
    ModelRegistryRevisionConflict as PortModelRegistryRevisionConflict,
)
from atelier2.ports.host_configuration import (
    ProjectModelDefaultsRevisionConflict as PortProjectModelDefaultsRevisionConflict,
)


def _require_project_source_shape(connection: Connection) -> None:
    try:
        stored_columns = frozenset(
            str(column["name"])
            for column in sa.inspect(connection).get_columns(
                host_project_source_connection_revisions.name
            )
        )
    except NoSuchTableError as error:
        raise ProjectSourceConnectionBytesDisagree(
            "project-source connection bytes disagree: the V45 table is absent"
        ) from error
    declared_columns = frozenset(host_project_source_connection_revisions.c.keys())
    if stored_columns != declared_columns:
        raise ProjectSourceConnectionBytesDisagree(
            "project-source connection bytes disagree: the V45 table shape is "
            "incomplete"
        )


def project_root_revision_from_record(record: Mapping[Any, Any]) -> ProjectRootRevision:
    revision = ProjectRootRevision(
        ProjectId(str(record["project_id"])),
        int(record["revision_number"]),
        Path(str(record["root_path"])),
    )
    if revision.revision_hash.value != record["revision_hash"]:
        raise ProjectRootBytesDisagree(
            "project-root bytes disagree: a stored project-root hash "
            "does not match its fields"
        )
    return revision


def _latest_project_root_revision(
    connection: Connection, project_id: ProjectId
) -> ProjectRootRevision | None:
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
    if record is None:
        return None
    return project_root_revision_from_record(record)


def latest_project_root_revision(
    engine: Engine, project_id: ProjectId
) -> ProjectRootRevision | None:
    try:
        with engine.connect() as connection:
            return _latest_project_root_revision(connection, project_id)
    except (OperationalError, PoolTimeoutError, DatabaseError) as error:
        raise HostConfigurationUnreadable(
            f"{HOST_CONFIGURATION_UNREADABLE}: the project-root channel "
            "could not be read"
        ) from error


def project_root_for(engine: Engine, project_id: ProjectId) -> Path:
    revision = latest_project_root_revision(engine, project_id)
    if revision is None:
        raise ProjectRootMissing(
            f"{PROJECT_ROOT_MISSING}: project {project_id.value!r} has no "
            "configured root"
        )
    return revision.root_path


def _write_project_root_revision(
    connection: Connection, revision: ProjectRootRevision
) -> ProjectRootRevision:
    keyed = (
        connection.execute(
            sa.select(host_project_root_revisions).where(
                host_project_root_revisions.c.project_id == revision.project_id.value,
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


def publish_project_root_revision(
    engine: Engine, revision: ProjectRootRevision
) -> ProjectRootRevision:
    try:
        with canonical_write_transaction(engine) as connection:
            return _write_project_root_revision(connection, revision)
    except (
        ProjectRootBytesDisagree,
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
    try:
        with canonical_write_transaction(engine) as connection:
            latest = _latest_project_root_revision(connection, project_id)
            candidate = ProjectRootRevision(
                project_id,
                1 if latest is None else latest.revision_number + 1,
                root_path,
            )
            if latest is not None and latest.root_path == candidate.root_path:
                return latest
            return _write_project_root_revision(connection, candidate)
    except (
        ProjectRootBytesDisagree,
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


def model_registry_revision_from_records(
    header: Mapping[Any, Any], entries: Sequence[Mapping[Any, Any]]
) -> ModelRegistryRevision:
    revision = ModelRegistryRevision(
        ProviderId(str(header["provider_id"])),
        int(header["revision_number"]),
        tuple(
            ModelRegistryEntry(
                str(record["model_id"]),
                AgentConfigurationRevisionHash(
                    str(record["agent_configuration_revision_hash"])
                ),
                ModelRegistryEntrySource(str(record["source"])),
                ProviderModelCheck(str(record["provider_check"])),
            )
            for record in entries
        ),
    )
    if revision.revision_hash.value != header["revision_hash"]:
        raise ModelRegistryBytesDisagree(
            "model registry bytes disagree with their stored revision hash"
        )
    if any(record["provider_id"] != revision.provider_id.value for record in entries):
        raise ModelRegistryBytesDisagree(
            "model registry entry provider disagrees with its revision"
        )
    return revision


def _model_registry_entries_for(
    connection: Connection, revision_hash: str
) -> Sequence[Mapping[Any, Any]]:
    return (
        connection.execute(
            sa.select(host_model_registry_entries)
            .where(host_model_registry_entries.c.revision_hash == revision_hash)
            .order_by(host_model_registry_entries.c.model_id)
        )
        .mappings()
        .all()
    )


def _latest_model_registry_revision(
    connection: Connection, provider_id: ProviderId
) -> ModelRegistryRevision | None:
    header = (
        connection.execute(
            sa.select(host_model_registry_revisions)
            .where(host_model_registry_revisions.c.provider_id == provider_id.value)
            .order_by(host_model_registry_revisions.c.revision_number.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if header is None:
        return None
    return model_registry_revision_from_records(
        header, _model_registry_entries_for(connection, str(header["revision_hash"]))
    )


def _latest_model_registry_revisions(
    connection: Connection,
) -> tuple[ModelRegistryRevision, ...]:
    provider_ids = connection.scalars(
        sa.select(host_model_registry_revisions.c.provider_id)
        .distinct()
        .order_by(host_model_registry_revisions.c.provider_id)
    ).all()
    revisions = tuple(
        _latest_model_registry_revision(connection, ProviderId(provider_id))
        for provider_id in provider_ids
    )
    if any(revision is None for revision in revisions):
        raise ModelRegistryBytesDisagree(
            "model registry provider vanished during one channel read"
        )
    return tuple(revision for revision in revisions if revision is not None)


def _write_model_registry_revision(
    connection: Connection, revision: ModelRegistryRevision
) -> ModelRegistryRevisionCreated | ModelRegistryRevisionExisting:
    keyed = (
        connection.execute(
            sa.select(host_model_registry_revisions).where(
                host_model_registry_revisions.c.provider_id
                == revision.provider_id.value,
                host_model_registry_revisions.c.revision_number
                == revision.revision_number,
            )
        )
        .mappings()
        .one_or_none()
    )
    if keyed is not None:
        durable = model_registry_revision_from_records(
            keyed, _model_registry_entries_for(connection, str(keyed["revision_hash"]))
        )
        if durable == revision:
            return ModelRegistryRevisionExisting(durable)
        raise ModelRegistryRevisionConflict(
            f"model-registry-revision-conflict: {revision.provider_id.value!r} "
            f"revision {revision.revision_number} already exists"
        )
    hashed = (
        connection.execute(
            sa.select(host_model_registry_revisions).where(
                host_model_registry_revisions.c.revision_hash
                == revision.revision_hash.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if hashed is not None:
        durable = model_registry_revision_from_records(
            hashed,
            _model_registry_entries_for(connection, str(hashed["revision_hash"])),
        )
        if durable == revision:
            return ModelRegistryRevisionExisting(durable)
        raise ModelRegistryRevisionHashCollision(
            "model-registry-revision-collision: registry hash already names "
            "other fields"
        )
    connection.execute(
        host_model_registry_revisions.insert().values(
            revision_hash=revision.revision_hash.value,
            provider_id=revision.provider_id.value,
            revision_number=revision.revision_number,
        )
    )
    if revision.entries:
        connection.execute(
            host_model_registry_entries.insert(),
            [
                {
                    "revision_hash": revision.revision_hash.value,
                    "provider_id": revision.provider_id.value,
                    "model_id": entry.model_id,
                    "agent_configuration_revision_hash": (
                        entry.agent_configuration_revision_hash.value
                    ),
                    "source": entry.source.value,
                    "provider_check": entry.provider_check.value,
                }
                for entry in revision.entries
            ],
        )
    return ModelRegistryRevisionCreated(revision)


def project_model_defaults_revision_from_records(
    header: Mapping[Any, Any], defaults: Sequence[Mapping[Any, Any]]
) -> ProjectModelDefaultsRevision:
    revision = ProjectModelDefaultsRevision(
        ProjectId(str(header["project_id"])),
        int(header["revision_number"]),
        tuple(
            ProjectModelDefault(
                cast(RoleDifficulty, int(record["difficulty"])),
                HostModelRegistryRevisionHash(
                    str(record["model_registry_revision_hash"])
                ),
                ProviderId(str(record["provider_id"])),
                str(record["model_id"]),
                AgentConfigurationRevisionHash(
                    str(record["agent_configuration_revision_hash"])
                ),
            )
            for record in defaults
        ),
    )
    if revision.revision_hash.value != header["revision_hash"]:
        raise ProjectModelDefaultsBytesDisagree(
            "project model-default bytes disagree with their stored revision hash"
        )
    return revision


def _project_model_defaults_for(
    connection: Connection, revision_hash: str
) -> Sequence[Mapping[Any, Any]]:
    return (
        connection.execute(
            sa.select(host_project_model_defaults)
            .where(host_project_model_defaults.c.revision_hash == revision_hash)
            .order_by(host_project_model_defaults.c.difficulty)
        )
        .mappings()
        .all()
    )


def _latest_project_model_defaults_revision(
    connection: Connection, project_id: ProjectId
) -> ProjectModelDefaultsRevision | None:
    header = (
        connection.execute(
            sa.select(host_project_model_defaults_revisions)
            .where(
                host_project_model_defaults_revisions.c.project_id == project_id.value
            )
            .order_by(host_project_model_defaults_revisions.c.revision_number.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if header is None:
        return None
    return project_model_defaults_revision_from_records(
        header, _project_model_defaults_for(connection, str(header["revision_hash"]))
    )


def model_configuration_snapshot(
    connection: Connection, project_id: ProjectId | None
) -> HostModelConfigurationSnapshot:
    """Read the model-configuration families through one transaction snapshot."""

    return HostModelConfigurationSnapshot(
        _latest_model_registry_revisions(connection),
        (
            None
            if project_id is None
            else _latest_project_model_defaults_revision(connection, project_id)
        ),
    )


def _write_project_model_defaults_revision(
    connection: Connection, revision: ProjectModelDefaultsRevision
) -> ProjectModelDefaultsRevisionCreated | ProjectModelDefaultsRevisionExisting:
    existing = _existing_project_model_defaults_revision(connection, revision)
    if existing is not None:
        return existing
    connection.execute(
        host_project_model_defaults_revisions.insert().values(
            revision_hash=revision.revision_hash.value,
            project_id=revision.project_id.value,
            revision_number=revision.revision_number,
        )
    )
    if revision.defaults:
        connection.execute(
            host_project_model_defaults.insert(),
            [
                {
                    "revision_hash": revision.revision_hash.value,
                    "difficulty": default.difficulty,
                    "model_registry_revision_hash": (
                        default.model_registry_revision_hash.value
                    ),
                    "provider_id": default.provider_id.value,
                    "model_id": default.model_id,
                    "agent_configuration_revision_hash": (
                        default.agent_configuration_revision_hash.value
                    ),
                }
                for default in revision.defaults
            ],
        )
    return ProjectModelDefaultsRevisionCreated(revision)


def _existing_project_model_defaults_revision(
    connection: Connection, revision: ProjectModelDefaultsRevision
) -> ProjectModelDefaultsRevisionExisting | None:
    keyed = (
        connection.execute(
            sa.select(host_project_model_defaults_revisions).where(
                host_project_model_defaults_revisions.c.project_id
                == revision.project_id.value,
                host_project_model_defaults_revisions.c.revision_number
                == revision.revision_number,
            )
        )
        .mappings()
        .one_or_none()
    )
    if keyed is not None:
        durable = project_model_defaults_revision_from_records(
            keyed,
            _project_model_defaults_for(connection, str(keyed["revision_hash"])),
        )
        if durable == revision:
            return ProjectModelDefaultsRevisionExisting(durable)
        raise ProjectModelDefaultsRevisionConflict(
            f"project-model-defaults-revision-conflict: {revision.project_id.value!r} "
            f"revision {revision.revision_number} already exists"
        )
    hashed = (
        connection.execute(
            sa.select(host_project_model_defaults_revisions).where(
                host_project_model_defaults_revisions.c.revision_hash
                == revision.revision_hash.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if hashed is not None:
        durable = project_model_defaults_revision_from_records(
            hashed,
            _project_model_defaults_for(connection, str(hashed["revision_hash"])),
        )
        if durable == revision:
            return ProjectModelDefaultsRevisionExisting(durable)
        raise ProjectModelDefaultsRevisionHashCollision(
            "project-model-defaults-revision-collision: defaults hash already "
            "names other fields"
        )
    return None


def _defaults_reference_current_model_choices(
    connection: Connection, defaults: ProjectModelDefaultsRevision
) -> bool:
    checked_memberships = {
        (
            registry.provider_id,
            entry.model_id,
            entry.agent_configuration_revision_hash,
        )
        for registry in _latest_model_registry_revisions(connection)
        for entry in registry.entries
        if entry.provider_check is ProviderModelCheck.CHECKED
    }
    current = _latest_project_model_defaults_revision(connection, defaults.project_id)
    carried_forward = frozenset(() if current is None else current.defaults)
    return all(
        _model_registry_reference_exists(connection, default)
        and (
            (
                default.provider_id,
                default.model_id,
                default.agent_configuration_revision_hash,
            )
            in checked_memberships
            or default in carried_forward
        )
        for default in defaults.defaults
    )


def _model_registry_reference_exists(
    connection: Connection, default: ProjectModelDefault
) -> bool:
    return (
        connection.execute(
            sa.select(host_model_registry_entries.c.revision_hash)
            .where(
                host_model_registry_entries.c.revision_hash
                == default.model_registry_revision_hash.value,
                host_model_registry_entries.c.provider_id == default.provider_id.value,
                host_model_registry_entries.c.model_id == default.model_id,
                host_model_registry_entries.c.agent_configuration_revision_hash
                == default.agent_configuration_revision_hash.value,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def project_source_connection_revision_from_record(
    record: Mapping[Any, Any],
) -> ProjectSourceConnectionRevision:
    revision = ProjectSourceConnectionRevision(
        ProjectId(str(record["project_id"])),
        ProjectSourceId(str(record["source_id"])),
        int(record["revision_number"]),
        SourceKind(str(record["source_kind"])),
        SourceAddress(str(record["source_address"])),
        Path(str(record["credential_directory"])),
        SourceConnectionAuthMethod(str(record["auth_method"])),
        ConnectionActor(str(record["connected_by"])),
        ProjectSourceConnectionLifecycle(str(record["lifecycle"])),
        (
            None
            if record["connected_at"] is None
            else RecordedAt(str(record["connected_at"]))
        ),
        (
            None
            if record["source_ref"] is None
            else SourceReference(str(record["source_ref"]))
        ),
    )
    if revision.revision_hash.value != record["revision_hash"]:
        raise ProjectSourceConnectionBytesDisagree(
            "project-source connection bytes disagree: a stored connection "
            "hash does not match its fields"
        )
    return revision


def _latest_project_source_connection_revision(
    connection: Connection, project_id: ProjectId
) -> ProjectSourceConnectionRevision | None:
    _require_project_source_shape(connection)
    ranked = (
        sa.select(
            host_project_source_connection_revisions,
            sa.func.row_number()
            .over(
                partition_by=host_project_source_connection_revisions.c.source_id,
                order_by=host_project_source_connection_revisions.c.revision_number.desc(),
            )
            .label("source_rank"),
        )
        .where(
            host_project_source_connection_revisions.c.project_id == project_id.value
        )
        .subquery()
    )
    records = (
        connection.execute(
            sa.select(ranked).where(
                ranked.c.source_rank == 1,
                ranked.c.lifecycle == ProjectSourceConnectionLifecycle.CONNECTED.value,
            )
        )
        .mappings()
        .all()
    )
    if len(records) > 1:
        raise ProjectSourceConnectionBytesDisagree(
            "project-source connection bytes disagree: a project has more than "
            "one active source"
        )
    return (
        None
        if not records
        else project_source_connection_revision_from_record(records[0])
    )


def latest_project_source_connection_revision(
    engine: Engine, project_id: ProjectId
) -> ProjectSourceConnectionRevision | None:
    try:
        with engine.connect() as connection:
            return _latest_project_source_connection_revision(connection, project_id)
    except OperationalError as error:
        raise HostConfigurationUnreadable(
            f"{HOST_CONFIGURATION_UNREADABLE}: the project-source connection "
            "channel could not be read"
        ) from error
    except (PoolTimeoutError, DatabaseError) as error:
        raise HostConfigurationUnreadable(
            f"{HOST_CONFIGURATION_UNREADABLE}: the project-source connection "
            "channel could not be read"
        ) from error


def _latest_project_source_connection_revisions(
    connection: Connection, project_id: ProjectId
) -> tuple[ProjectSourceConnectionRevision, ...]:
    _require_project_source_shape(connection)
    ranked = (
        sa.select(
            host_project_source_connection_revisions,
            sa.func.row_number()
            .over(
                partition_by=host_project_source_connection_revisions.c.source_id,
                order_by=host_project_source_connection_revisions.c.revision_number.desc(),
            )
            .label("source_rank"),
        )
        .where(
            host_project_source_connection_revisions.c.project_id == project_id.value
        )
        .subquery()
    )
    records = (
        connection.execute(
            sa.select(ranked)
            .where(ranked.c.source_rank == 1)
            .order_by(ranked.c.source_id)
        )
        .mappings()
        .all()
    )
    return tuple(
        project_source_connection_revision_from_record(record) for record in records
    )


def _latest_project_source_connection_revision_by_source(
    connection: Connection, project_id: ProjectId, source_id: ProjectSourceId
) -> ProjectSourceConnectionRevision | None:
    _require_project_source_shape(connection)
    record = (
        connection.execute(
            sa.select(host_project_source_connection_revisions)
            .where(
                host_project_source_connection_revisions.c.project_id
                == project_id.value,
                host_project_source_connection_revisions.c.source_id == source_id.value,
            )
            .order_by(host_project_source_connection_revisions.c.revision_number.desc())
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    return (
        None
        if record is None
        else project_source_connection_revision_from_record(record)
    )


def _project_source_credential_directory_reference(
    connection: Connection,
    project_id: ProjectId,
    credential_directory: Path,
) -> ProjectSourceCredentialDirectoryReferenceResult:
    _require_project_source_shape(connection)
    records = (
        connection.execute(
            sa.select(host_project_source_connection_revisions).where(
                host_project_source_connection_revisions.c.project_id
                == project_id.value,
                host_project_source_connection_revisions.c.credential_directory
                == str(credential_directory),
            )
        )
        .mappings()
        .all()
    )
    for record in records:
        project_source_connection_revision_from_record(record)
    return (
        ProjectSourceCredentialDirectoryUnreferenced()
        if not records
        else ProjectSourceCredentialDirectoryReferenced()
    )


def _write_project_source_connection_revision(
    connection: Connection, revision: ProjectSourceConnectionRevision
) -> ProjectSourceConnectionRevisionCreated | ProjectSourceConnectionRevisionExisting:
    _require_project_source_shape(connection)
    if revision.lifecycle is ProjectSourceConnectionLifecycle.CONNECTED:
        active_sources = tuple(
            stored
            for stored in _latest_project_source_connection_revisions(
                connection, revision.project_id
            )
            if stored.lifecycle is ProjectSourceConnectionLifecycle.CONNECTED
        )
        if len(active_sources) > 1:
            raise ProjectSourceConnectionBytesDisagree(
                "project-source connection bytes disagree: a project has more "
                "than one active source"
            )
        if active_sources and active_sources[0].source_id != revision.source_id:
            raise ProjectSourceConnectionConflict(
                "project-source-connection-conflict: project already has an "
                f"active source {active_sources[0].source_id.value!r}"
            )
    keyed = (
        connection.execute(
            sa.select(host_project_source_connection_revisions).where(
                host_project_source_connection_revisions.c.project_id
                == revision.project_id.value,
                host_project_source_connection_revisions.c.source_id
                == revision.source_id.value,
                host_project_source_connection_revisions.c.revision_number
                == revision.revision_number,
            )
        )
        .mappings()
        .one_or_none()
    )
    if keyed is not None:
        durable = project_source_connection_revision_from_record(keyed)
        if durable == revision:
            return ProjectSourceConnectionRevisionExisting(durable)
        raise ProjectSourceConnectionConflict(
            "project-source-connection-conflict: "
            f"{revision.project_id.value!r} source {revision.source_id.value!r} "
            f"revision {revision.revision_number} already exists"
        )
    hashed = (
        connection.execute(
            sa.select(host_project_source_connection_revisions).where(
                host_project_source_connection_revisions.c.revision_hash
                == revision.revision_hash.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if hashed is not None:
        durable = project_source_connection_revision_from_record(hashed)
        if durable == revision:
            return ProjectSourceConnectionRevisionExisting(durable)
        raise ProjectSourceConnectionHashCollision(
            "project-source-connection-collision: connection revision "
            f"{revision.revision_hash.value} already names other fields"
        )
    connection.execute(
        host_project_source_connection_revisions.insert().values(
            revision_hash=revision.revision_hash.value,
            project_id=revision.project_id.value,
            source_id=revision.source_id.value,
            source_kind=revision.source_kind.value,
            revision_number=revision.revision_number,
            source_address=revision.source_address.value,
            source_ref=(
                None if revision.source_ref is None else revision.source_ref.value
            ),
            credential_directory=str(revision.credential_directory),
            auth_method=revision.auth_method.value,
            connected_by=revision.connected_by.value,
            lifecycle=revision.lifecycle.value,
            connected_at=(
                None if revision.connected_at is None else revision.connected_at.value
            ),
        )
    )
    return ProjectSourceConnectionRevisionCreated(revision)


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
        except ProjectRootBytesDisagree:
            return DurableStateCorrupt()
        except (ValueError, RuntimeError):
            return DurableStateCorrupt()

    def latest_model_registry_revision(
        self, provider_id: ProviderId
    ) -> LatestModelRegistryResult:
        try:
            with self._engine.connect() as connection:
                return _latest_model_registry_revision(connection, provider_id)
        except ModelRegistryBytesDisagree:
            return DurableStateCorrupt()
        except (OperationalError, PoolTimeoutError):
            return HostConfigurationReadUnavailable()
        except (ValueError, TypeError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def publish_model_registry_revision(
        self, revision: ModelRegistryRevision
    ) -> PublishModelRegistryResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                return _write_model_registry_revision(connection, revision)
        except ModelRegistryRevisionConflict:
            return PortModelRegistryRevisionConflict()
        except ModelRegistryRevisionHashCollision:
            return ModelRegistryRevisionCollision()
        except ModelRegistryBytesDisagree:
            return DurableStateCorrupt()
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, TypeError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def latest_model_registry_revisions(self) -> LatestModelRegistriesResult:
        try:
            with self._engine.connect() as connection:
                return _latest_model_registry_revisions(connection)
        except ModelRegistryBytesDisagree:
            return DurableStateCorrupt()
        except (OperationalError, PoolTimeoutError):
            return HostConfigurationReadUnavailable()
        except (ValueError, TypeError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def latest_project_model_defaults_revision(
        self, project_id: ProjectId
    ) -> LatestProjectModelDefaultsResult:
        try:
            with self._engine.connect() as connection:
                return _latest_project_model_defaults_revision(connection, project_id)
        except ProjectModelDefaultsBytesDisagree:
            return DurableStateCorrupt()
        except (OperationalError, PoolTimeoutError):
            return HostConfigurationReadUnavailable()
        except (ProjectUnknown, ValueError, TypeError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def model_configuration_snapshot(
        self, project_id: ProjectId | None
    ) -> HostModelConfigurationSnapshotResult:
        try:
            with self._engine.connect() as connection:
                connection.exec_driver_sql("BEGIN DEFERRED")
                try:
                    return model_configuration_snapshot(connection, project_id)
                finally:
                    connection.rollback()
        except (ModelRegistryBytesDisagree, ProjectModelDefaultsBytesDisagree):
            return DurableStateCorrupt()
        except (OperationalError, PoolTimeoutError):
            return HostConfigurationReadUnavailable()
        except (ProjectUnknown, ValueError, TypeError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def publish_project_model_defaults_revision(
        self, revision: ProjectModelDefaultsRevision
    ) -> PublishProjectModelDefaultsResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                existing = _existing_project_model_defaults_revision(
                    connection, revision
                )
                if existing is not None:
                    return existing
                if not _defaults_reference_current_model_choices(connection, revision):
                    return ProjectModelDefaultsRevisionInvalid()
                return _write_project_model_defaults_revision(connection, revision)
        except ProjectModelDefaultsRevisionConflict:
            return PortProjectModelDefaultsRevisionConflict()
        except ProjectModelDefaultsRevisionHashCollision:
            return ProjectModelDefaultsRevisionCollision()
        except ProjectModelDefaultsBytesDisagree:
            return DurableStateCorrupt()
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ProjectUnknown, ValueError, TypeError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def latest_project_source_connection_revision(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionResult:
        try:
            return latest_project_source_connection_revision(self._engine, project_id)
        except HostConfigurationUnreadable as error:
            return HostConfigurationReadUnavailable(str(error))
        except ProjectSourceConnectionBytesDisagree:
            return DurableStateCorrupt()
        except (ProjectUnknown, ValueError, TypeError, RuntimeError):
            return DurableStateCorrupt()

    def latest_project_source_connection_revisions(
        self, project_id: ProjectId
    ) -> LatestProjectSourceConnectionsResult:
        try:
            with self._engine.connect() as connection:
                return _latest_project_source_connection_revisions(
                    connection, project_id
                )
        except ProjectSourceConnectionBytesDisagree:
            return DurableStateCorrupt()
        except OperationalError:
            return HostConfigurationReadUnavailable()
        except PoolTimeoutError:
            return HostConfigurationReadUnavailable()
        except (ProjectUnknown, ValueError, TypeError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def latest_project_source_connection_revision_by_source(
        self, project_id: ProjectId, source_id: ProjectSourceId
    ) -> LatestProjectSourceConnectionResult:
        try:
            with self._engine.connect() as connection:
                return _latest_project_source_connection_revision_by_source(
                    connection, project_id, source_id
                )
        except ProjectSourceConnectionBytesDisagree:
            return DurableStateCorrupt()
        except OperationalError:
            return HostConfigurationReadUnavailable()
        except PoolTimeoutError:
            return HostConfigurationReadUnavailable()
        except (ProjectUnknown, ValueError, TypeError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def project_source_credential_directory_reference(
        self, project_id: ProjectId, credential_directory: Path
    ) -> ProjectSourceCredentialDirectoryReferenceResult:
        try:
            with self._engine.connect() as connection:
                return _project_source_credential_directory_reference(
                    connection, project_id, credential_directory
                )
        except ProjectSourceConnectionBytesDisagree:
            return DurableStateCorrupt()
        except OperationalError:
            return HostConfigurationReadUnavailable()
        except PoolTimeoutError:
            return HostConfigurationReadUnavailable()
        except (ProjectUnknown, ValueError, TypeError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def publish_project_source_connection_revision(
        self, revision: ProjectSourceConnectionRevision
    ) -> PublishProjectSourceConnectionResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                return _write_project_source_connection_revision(connection, revision)
        except ProjectSourceConnectionConflict:
            return ProjectSourceConnectionRevisionConflict()
        except ProjectSourceConnectionHashCollision:
            return ProjectSourceConnectionRevisionCollision()
        except ProjectSourceConnectionBytesDisagree:
            return DurableStateCorrupt()
        except OperationalError:
            return DurableWriteUnavailable()
        except PoolTimeoutError:
            return DurableWriteUnavailable()
        except (ProjectUnknown, ValueError, TypeError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
