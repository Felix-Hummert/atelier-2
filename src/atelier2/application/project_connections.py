"""Connecting a project to its external source, as this layer's decisions.

Connecting is an explicit operator act (ADR 0010 decision 2): it appends one
immutable revision binding the project to a source kind, an opaque source
address, a credential-directory reference, the chosen auth method, and the
connecting actor. The credential value never passes through here. A project
without a record answers `platform-connection-unknown`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import assert_never

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
    ProjectSourceConnectionRevision,
    ProjectUnknown,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
)
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
        case ProjectSourceConnectionRevision() as revision:
            return ProjectSourceConnectionRead(revision)
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
    ) == (
        candidate.source_kind,
        candidate.source_address,
        candidate.credential_directory,
        candidate.auth_method,
        candidate.connected_by,
    )


def connect_project_source(
    project_id: str,
    source_kind: str,
    source_address: str,
    credential_directory: Path,
    auth_method: str,
    connected_by: str,
    channel: HostConfigurationChannel,
    connections: ProjectSourceConnectionChannel,
) -> ConnectProjectSourceResult:
    try:
        project = ProjectId(project_id)
    except ProjectUnknown:
        return ConnectionProjectUnknown()
    known = _known_project(project, channel)
    if known is not None:
        return known
    match connections.latest_project_source_connection_revision(project):
        case ProjectSourceConnectionRevision() | None as latest:
            pass
        case PortHostConfigurationReadUnavailable(detail):
            return WriteUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
    try:
        candidate = ProjectSourceConnectionRevision(
            project,
            1 if latest is None else latest.revision_number + 1,
            SourceKind(source_kind),
            SourceAddress(source_address),
            credential_directory,
            SourceConnectionAuthMethod(auth_method),
            ConnectionActor(connected_by),
        )
    except (TypeError, ValueError):
        return UnpublishableConnection()
    if latest is not None and _unchanged_fields(latest, candidate):
        return ProjectSourceConnectionUnchanged(latest)
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
