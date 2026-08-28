"""Connecting a project to its external source, as this layer's decisions.

Connecting is an explicit operator act (ADR 0010 decision 2): it appends one
immutable revision binding the project to a source kind, an opaque source
address, a credential-directory reference, the chosen auth method, and the
connecting actor. The credential value never passes through here. A project
without a record answers `platform-connection-unknown`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never
from uuid import uuid4

from atelier2.application.read_projects import (
    ProjectRead,
    ServedProjectUnknown,
    get_project,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.host_configuration import (
    ConnectionActor,
    ProjectId,
    ProjectRootRevision,
    ProjectSourceConnectionLifecycle,
    ProjectSourceConnectionRevision,
    ProjectSourceId,
    ProjectUnknown,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
)
from atelier2.contracts.when import RecordedAt, recorded_instant
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.host_configuration import (
    HostConfigurationChannel,
    ProjectSourceConnectionChannel,
)
from atelier2.ports.host_configuration import (
    HostConfigurationReadUnavailable as PortHostConfigurationReadUnavailable,
)
from atelier2.ports.host_configuration import (
    ProjectSourceConnectionRevisionCollision as PortConnectionRevisionCollision,
)
from atelier2.ports.host_configuration import (
    ProjectSourceConnectionRevisionConflict as PortConnectionRevisionConflict,
)
from atelier2.ports.host_configuration import (
    ProjectSourceConnectionRevisionCreated as PortConnectionRevisionCreated,
)
from atelier2.ports.host_configuration import (
    ProjectSourceConnectionRevisionExisting as PortConnectionRevisionExisting,
)
from atelier2.ports.project_connections import (
    CredentialDepositUnavailable,
    ManagedCredentialDeposit,
    ManagedProjectSourceCredentialStore,
    ParsedProjectSourceAddress,
    ProjectSourceAddressInvalid,
    ProjectSourceAuthenticationRefused,
    ProjectSourceConnector,
    ProjectSourceValidationUnavailable,
    ValidatedProjectSource,
)


@dataclass(frozen=True)
class ProjectSourceConnectionRead:
    revision: ProjectSourceConnectionRevision


@dataclass(frozen=True)
class PlatformConnectionUnknown:
    """The project names no connection record (ADR 0010's refusal)."""


type GetProjectSourceConnectionResult = (
    ProjectSourceConnectionRead
    | PlatformConnectionUnknown
    | ReadUnavailable
    | DurableStateCorrupt
)
type GetServedProjectSourceConnectionResult = (
    GetProjectSourceConnectionResult | ServedProjectUnknown
)


@dataclass(frozen=True)
class ProjectSourceConnectionPublished:
    revision: ProjectSourceConnectionRevision


@dataclass(frozen=True)
class ProjectSourceConnectionUnchanged:
    revision: ProjectSourceConnectionRevision


@dataclass(frozen=True)
class ProjectSourceConnectionConflict:
    pass


@dataclass(frozen=True)
class ProjectSourceConnectionCollision:
    pass


@dataclass(frozen=True)
class ConnectionProjectUnknown:
    """The id is malformed, or names a project with no configured root."""


@dataclass(frozen=True)
class UnpublishableConnection:
    """The authored values do not make one connection revision."""


@dataclass(frozen=True)
class ProjectSourceSummary:
    source_id: ProjectSourceId
    source_kind: SourceKind
    public_address: str
    connected_at: RecordedAt | None
    revision_number: int
    auth_method: SourceConnectionAuthMethod


@dataclass(frozen=True)
class ProjectSourcesRead:
    sources: tuple[ProjectSourceSummary, ...]


@dataclass(frozen=True)
class ManagedProjectSourcePublished:
    source: ProjectSourceSummary


@dataclass(frozen=True)
class ProjectSourceAlreadyConnected:
    source_id: ProjectSourceId


@dataclass(frozen=True)
class ProjectSourceUnknown:
    pass


@dataclass(frozen=True)
class ProjectSourceDisconnected:
    pass


@dataclass(frozen=True)
class ProjectSourceInvalid:
    reason: str


@dataclass(frozen=True)
class ProjectSourceTokenRefused:
    reason: str


@dataclass(frozen=True)
class ProjectSourceUnavailable:
    detail: str | None = None


@dataclass(frozen=True)
class ProjectSourceDisconnectedSuccessfully:
    pass


type ListProjectSourcesResult = (
    ProjectSourcesRead | ServedProjectUnknown | ReadUnavailable | DurableStateCorrupt
)
type ConnectManagedProjectSourceResult = (
    ManagedProjectSourcePublished
    | ProjectSourceAlreadyConnected
    | ConnectionProjectUnknown
    | ProjectSourceInvalid
    | ProjectSourceTokenRefused
    | ProjectSourceUnavailable
    | WriteUnavailable
    | DurableStateCorrupt
)
type DisconnectProjectSourceResult = (
    ProjectSourceDisconnectedSuccessfully
    | ProjectSourceUnknown
    | ConnectionProjectUnknown
    | WriteUnavailable
    | DurableStateCorrupt
)
type RotateProjectSourceTokenResult = (
    ManagedProjectSourcePublished
    | ProjectSourceUnknown
    | ProjectSourceDisconnected
    | ConnectionProjectUnknown
    | ProjectSourceTokenRefused
    | ProjectSourceInvalid
    | ProjectSourceUnavailable
    | WriteUnavailable
    | DurableStateCorrupt
)


type ConnectProjectSourceResult = (
    ProjectSourceConnectionPublished
    | ProjectSourceConnectionUnchanged
    | ProjectSourceConnectionConflict
    | ProjectSourceConnectionCollision
    | ConnectionProjectUnknown
    | UnpublishableConnection
    | WriteUnavailable
    | DurableStateCorrupt
)


def get_project_source_connection(
    project_id: str,
    connections: ProjectSourceConnectionChannel,
) -> GetProjectSourceConnectionResult:
    try:
        project = ProjectId(project_id)
    except ProjectUnknown:
        return PlatformConnectionUnknown()
    match connections.latest_project_source_connection_revision(project):
        case (
            ProjectSourceConnectionRevision(
                lifecycle=ProjectSourceConnectionLifecycle.CONNECTED
            ) as revision
        ):
            return ProjectSourceConnectionRead(revision)
        case ProjectSourceConnectionRevision():
            return PlatformConnectionUnknown()
        case None:
            return PlatformConnectionUnknown()
        case PortHostConfigurationReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def get_served_project_source_connection(
    project_id: ProjectId,
    served_project_id: ProjectId | None,
    host_configuration: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
) -> GetServedProjectSourceConnectionResult:
    match get_project(project_id, served_project_id, host_configuration):
        case ProjectRead():
            return get_project_source_connection(project_id.value, connections)
        case ServedProjectUnknown() as unknown:
            return unknown
        case ReadUnavailable() as unavailable:
            return unavailable
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)


def _known_project(
    project_id: ProjectId, channel: HostConfigurationChannel
) -> ConnectionProjectUnknown | WriteUnavailable | DurableStateCorrupt | None:
    match channel.latest_project_root_revision(project_id):
        case None:
            return ConnectionProjectUnknown()
        case ProjectRootRevision():
            return None
        case PortHostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _unchanged_fields(
    latest: ProjectSourceConnectionRevision,
    candidate: ProjectSourceConnectionRevision,
) -> bool:
    return (
        latest.source_kind,
        latest.source_address,
        latest.credential_directory,
        latest.auth_method,
        latest.connected_by,
        latest.lifecycle,
    ) == (
        candidate.source_kind,
        candidate.source_address,
        candidate.credential_directory,
        candidate.auth_method,
        candidate.connected_by,
        candidate.lifecycle,
    )


def new_project_source_id() -> ProjectSourceId:
    return ProjectSourceId(str(uuid4()))


def connect_project_source(
    project_id: str,
    source_kind: str,
    source_address: str,
    credential_directory: Path,
    auth_method: str,
    connected_by: str,
    channel: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
    *,
    source_id_generator: Callable[[], ProjectSourceId] = new_project_source_id,
    connected_at: RecordedAt | None = None,
) -> ConnectProjectSourceResult:
    try:
        project = ProjectId(project_id)
    except ProjectUnknown:
        return ConnectionProjectUnknown()
    known = _known_project(project, channel)
    if known is not None:
        return known
    try:
        typed_source_kind = SourceKind(source_kind)
        typed_source_address = SourceAddress(source_address)
    except (TypeError, ValueError):
        return UnpublishableConnection()
    latest_sources = _latest_sources(project, connections)
    if isinstance(latest_sources, ReadUnavailable):
        return WriteUnavailable(latest_sources.detail)
    if isinstance(latest_sources, DurableStateCorrupt):
        return latest_sources
    active = _active_source(latest_sources)
    if isinstance(active, DurableStateCorrupt):
        return active
    matching_history = tuple(
        revision
        for revision in latest_sources
        if revision.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED
        and revision.source_kind == typed_source_kind
        and revision.source_address == typed_source_address
    )
    if len(matching_history) > 1:
        return DurableStateCorrupt()
    latest = active or (None if not matching_history else matching_history[0])
    try:
        candidate = ProjectSourceConnectionRevision(
            project,
            source_id_generator() if latest is None else latest.source_id,
            1 if latest is None else latest.revision_number + 1,
            typed_source_kind,
            typed_source_address,
            credential_directory,
            SourceConnectionAuthMethod(auth_method),
            ConnectionActor(connected_by),
            ProjectSourceConnectionLifecycle.CONNECTED,
            (
                connected_at or recorded_instant()
                if latest is None
                or latest.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED
                else latest.connected_at
            ),
        )
    except (TypeError, ValueError):
        return UnpublishableConnection()
    if latest is not None and _unchanged_fields(latest, candidate):
        return ProjectSourceConnectionUnchanged(latest)
    return _connection_write_result(candidate, connections)


def _connection_write_result(
    candidate: ProjectSourceConnectionRevision,
    connections: ProjectSourceConnectionChannel,
) -> ConnectProjectSourceResult:
    match connections.publish_project_source_connection_revision(candidate):
        case PortConnectionRevisionCreated(stored):
            return ProjectSourceConnectionPublished(stored)
        case PortConnectionRevisionExisting(stored):
            return ProjectSourceConnectionUnchanged(stored)
        case PortConnectionRevisionConflict():
            return ProjectSourceConnectionConflict()
        case PortConnectionRevisionCollision():
            return ProjectSourceConnectionCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _latest_sources(
    project_id: ProjectId,
    connections: ProjectSourceConnectionChannel,
) -> (
    tuple[ProjectSourceConnectionRevision, ...] | ReadUnavailable | DurableStateCorrupt
):
    match connections.latest_project_source_connection_revisions(project_id):
        case tuple() as revisions:
            return revisions
        case PortHostConfigurationReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def _active_source(
    revisions: tuple[ProjectSourceConnectionRevision, ...],
) -> ProjectSourceConnectionRevision | DurableStateCorrupt | None:
    active = tuple(
        revision
        for revision in revisions
        if revision.lifecycle is ProjectSourceConnectionLifecycle.CONNECTED
    )
    if len(active) > 1:
        return DurableStateCorrupt()
    return None if not active else active[0]


def list_served_project_sources(
    project_id: ProjectId,
    served_project_id: ProjectId | None,
    host_configuration: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
    connector: ProjectSourceConnector,
) -> ListProjectSourcesResult:
    match get_project(project_id, served_project_id, host_configuration):
        case ProjectRead():
            pass
        case ServedProjectUnknown() as unknown:
            return unknown
        case ReadUnavailable() as unavailable:
            return unavailable
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)
    latest = _latest_sources(project_id, connections)
    if isinstance(latest, (ReadUnavailable, DurableStateCorrupt)):
        return latest
    active = _active_source(latest)
    if isinstance(active, DurableStateCorrupt):
        return active
    summaries: list[ProjectSourceSummary] = []
    try:
        if active is not None:
            summaries.append(
                ProjectSourceSummary(
                    active.source_id,
                    active.source_kind,
                    connector.public_address(active.source_address),
                    active.connected_at,
                    active.revision_number,
                    active.auth_method,
                )
            )
    except ValueError:
        return DurableStateCorrupt()
    return ProjectSourcesRead(tuple(summaries))


def _managed_connect_candidate(
    project_id: ProjectId,
    source_id: ProjectSourceId,
    revision_number: int,
    validated: ValidatedProjectSource,
    credential_directory: Path,
    connected_at: RecordedAt,
) -> ProjectSourceConnectionRevision:
    return ProjectSourceConnectionRevision(
        project_id,
        source_id,
        revision_number,
        validated.source_kind,
        validated.source_address,
        credential_directory,
        SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
        ConnectionActor("http-api"),
        ProjectSourceConnectionLifecycle.CONNECTED,
        connected_at,
    )


def _source_summary(
    revision: ProjectSourceConnectionRevision, public_address: str
) -> ProjectSourceSummary:
    return ProjectSourceSummary(
        revision.source_id,
        revision.source_kind,
        public_address,
        revision.connected_at,
        revision.revision_number,
        revision.auth_method,
    )


def _discard_managed_token(
    staged: ManagedCredentialDeposit,
) -> ProjectSourceUnavailable | None:
    try:
        staged.discard()
    except (OSError, RuntimeError) as error:
        return ProjectSourceUnavailable(f"managed token cleanup failed: {error}")
    return None


def connect_managed_project_source(
    project_id: ProjectId,
    served_project_id: ProjectId | None,
    address: str,
    token: str,
    host_configuration: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
    connector: ProjectSourceConnector,
    token_deposits: ManagedProjectSourceCredentialStore,
    source_id_generator: Callable[[], ProjectSourceId],
    clock: Callable[[], RecordedAt],
) -> ConnectManagedProjectSourceResult:
    match get_project(project_id, served_project_id, host_configuration):
        case ProjectRead():
            pass
        case ServedProjectUnknown():
            return ConnectionProjectUnknown()
        case ReadUnavailable(detail):
            return WriteUnavailable(detail)
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)
    match connector.parse_address(address):
        case ParsedProjectSourceAddress() as parsed:
            pass
        case ProjectSourceAddressInvalid(reason):
            return ProjectSourceInvalid(reason)
        case _ as unreachable:
            assert_never(unreachable)
    latest = _latest_sources(project_id, connections)
    if isinstance(latest, ReadUnavailable):
        return WriteUnavailable(latest.detail)
    if isinstance(latest, DurableStateCorrupt):
        return latest
    try:
        active = _active_source(latest)
        if isinstance(active, DurableStateCorrupt):
            return active
        if active is not None:
            return ProjectSourceAlreadyConnected(active.source_id)
        prior = next(
            (
                revision
                for revision in latest
                if connector.public_address(revision.source_address)
                == parsed.public_address
            ),
            None,
        )
    except ValueError:
        return DurableStateCorrupt()
    source_id = source_id_generator() if prior is None else prior.source_id
    staged = token_deposits.stage(source_id, token)
    if isinstance(staged, CredentialDepositUnavailable):
        return ProjectSourceUnavailable(staged.detail)
    try:
        validation = connector.validate(parsed, staged.credential_directory)
    except (OSError, RuntimeError, TypeError, ValueError):
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or ProjectSourceUnavailable(
            "source validation failed unexpectedly"
        )
    match validation:
        case ValidatedProjectSource() as validated:
            pass
        case ProjectSourceAuthenticationRefused(reason):
            cleanup_failure = _discard_managed_token(staged)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceTokenRefused(reason)
        case ProjectSourceAddressInvalid(reason):
            cleanup_failure = _discard_managed_token(staged)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceInvalid(reason)
        case ProjectSourceValidationUnavailable(detail):
            cleanup_failure = _discard_managed_token(staged)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceUnavailable(detail)
        case _ as unreachable:
            assert_never(unreachable)
    try:
        credential_directory = staged.publish()
        candidate = _managed_connect_candidate(
            project_id,
            source_id,
            1 if prior is None else prior.revision_number + 1,
            validated,
            credential_directory,
            clock(),
        )
        result = _connection_write_result(candidate, connections)
    except (OSError, TypeError, ValueError) as error:
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or ProjectSourceUnavailable(str(error))
    if isinstance(result, ProjectSourceConnectionPublished):
        return ManagedProjectSourcePublished(
            _source_summary(result.revision, validated.public_address),
        )
    cleanup_failure = _discard_managed_token(staged)
    if cleanup_failure is not None:
        return cleanup_failure
    if isinstance(result, ProjectSourceConnectionConflict):
        after_conflict = _latest_sources(project_id, connections)
        if isinstance(after_conflict, ReadUnavailable):
            return WriteUnavailable(after_conflict.detail)
        if isinstance(after_conflict, DurableStateCorrupt):
            return after_conflict
        active_after_conflict = _active_source(after_conflict)
        if isinstance(active_after_conflict, DurableStateCorrupt):
            return active_after_conflict
        if active_after_conflict is not None:
            return ProjectSourceAlreadyConnected(active_after_conflict.source_id)
    if isinstance(result, (WriteUnavailable, DurableStateCorrupt)):
        return result
    return DurableStateCorrupt()


def disconnect_project_source(
    project_id: ProjectId,
    served_project_id: ProjectId | None,
    source_id: ProjectSourceId,
    host_configuration: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
) -> DisconnectProjectSourceResult:
    match get_project(project_id, served_project_id, host_configuration):
        case ProjectRead():
            pass
        case ServedProjectUnknown():
            return ConnectionProjectUnknown()
        case ReadUnavailable(detail):
            return WriteUnavailable(detail)
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)
    match connections.latest_project_source_connection_revision_by_source(
        project_id, source_id
    ):
        case None:
            return ProjectSourceUnknown()
        case ProjectSourceConnectionRevision() as latest:
            pass
        case PortHostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    if latest.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED:
        return ProjectSourceDisconnectedSuccessfully()
    candidate = ProjectSourceConnectionRevision(
        latest.project_id,
        latest.source_id,
        latest.revision_number + 1,
        latest.source_kind,
        latest.source_address,
        latest.credential_directory,
        latest.auth_method,
        latest.connected_by,
        ProjectSourceConnectionLifecycle.DISCONNECTED,
        latest.connected_at,
    )
    result = _connection_write_result(candidate, connections)
    if isinstance(
        result, (ProjectSourceConnectionPublished, ProjectSourceConnectionUnchanged)
    ):
        return ProjectSourceDisconnectedSuccessfully()
    if isinstance(result, (WriteUnavailable, DurableStateCorrupt)):
        return result
    return DurableStateCorrupt()


def rotate_project_source_token(
    project_id: ProjectId,
    served_project_id: ProjectId | None,
    source_id: ProjectSourceId,
    token: str,
    host_configuration: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
    connector: ProjectSourceConnector,
    token_deposits: ManagedProjectSourceCredentialStore,
) -> RotateProjectSourceTokenResult:
    match get_project(project_id, served_project_id, host_configuration):
        case ProjectRead():
            pass
        case ServedProjectUnknown():
            return ConnectionProjectUnknown()
        case ReadUnavailable(detail):
            return WriteUnavailable(detail)
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)
    match connections.latest_project_source_connection_revision_by_source(
        project_id, source_id
    ):
        case None:
            return ProjectSourceUnknown()
        case ProjectSourceConnectionRevision() as latest:
            pass
        case PortHostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    if latest.lifecycle is ProjectSourceConnectionLifecycle.DISCONNECTED:
        return ProjectSourceDisconnected()
    parsed = connector.parse_stored_address(latest.source_address)
    if isinstance(parsed, ProjectSourceAddressInvalid):
        return DurableStateCorrupt()
    staged = token_deposits.stage(source_id, token)
    if isinstance(staged, CredentialDepositUnavailable):
        return ProjectSourceUnavailable(staged.detail)
    try:
        validation = connector.validate(parsed, staged.credential_directory)
    except (OSError, RuntimeError, TypeError, ValueError):
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or ProjectSourceUnavailable(
            "source validation failed unexpectedly"
        )
    match validation:
        case ValidatedProjectSource() as validated:
            pass
        case ProjectSourceAuthenticationRefused(reason):
            cleanup_failure = _discard_managed_token(staged)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceTokenRefused(reason)
        case ProjectSourceAddressInvalid(reason):
            cleanup_failure = _discard_managed_token(staged)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceInvalid(reason)
        case ProjectSourceValidationUnavailable(detail):
            cleanup_failure = _discard_managed_token(staged)
            if cleanup_failure is not None:
                return cleanup_failure
            return ProjectSourceUnavailable(detail)
        case _ as unreachable:
            assert_never(unreachable)
    try:
        credential_directory = staged.publish()
        candidate = ProjectSourceConnectionRevision(
            latest.project_id,
            latest.source_id,
            latest.revision_number + 1,
            validated.source_kind,
            validated.source_address,
            credential_directory,
            latest.auth_method,
            latest.connected_by,
            ProjectSourceConnectionLifecycle.CONNECTED,
            latest.connected_at,
        )
        result = _connection_write_result(candidate, connections)
    except (OSError, TypeError, ValueError) as error:
        cleanup_failure = _discard_managed_token(staged)
        return cleanup_failure or ProjectSourceUnavailable(str(error))
    if isinstance(result, ProjectSourceConnectionPublished):
        return ManagedProjectSourcePublished(
            _source_summary(result.revision, validated.public_address),
        )
    cleanup_failure = _discard_managed_token(staged)
    if cleanup_failure is not None:
        return cleanup_failure
    if isinstance(result, (WriteUnavailable, DurableStateCorrupt)):
        return result
    return DurableStateCorrupt()
