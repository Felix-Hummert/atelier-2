"""Read the zero-or-one project this serve process actually opened."""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.host_configuration import ProjectId, ProjectRootRevision
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.host_configuration import HostConfigurationChannel
from atelier2.ports.host_configuration import (
    HostConfigurationReadUnavailable as PortHostConfigurationReadUnavailable,
)


@dataclass(frozen=True)
class ProjectRead:
    project_id: ProjectId


@dataclass(frozen=True)
class ProjectListRead:
    items: tuple[ProjectRead, ...]


@dataclass(frozen=True)
class ServedProjectUnknown:
    pass


type GetProjectResult = (
    ProjectRead | ServedProjectUnknown | ReadUnavailable | DurableStateCorrupt
)
type ListProjectsResult = (
    ProjectListRead | ServedProjectUnknown | ReadUnavailable | DurableStateCorrupt
)


def _read_active_project(
    active_project_id: ProjectId,
    channel: HostConfigurationChannel,
) -> GetProjectResult:
    match channel.latest_project_root_revision(active_project_id):
        case ProjectRootRevision(project_id=project_id):
            if project_id != active_project_id:
                return DurableStateCorrupt()
            return ProjectRead(project_id)
        case None:
            return ServedProjectUnknown()
        case PortHostConfigurationReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def list_projects(
    active_project_id: ProjectId | None,
    channel: HostConfigurationChannel,
) -> ListProjectsResult:
    if active_project_id is None:
        return ProjectListRead(())
    match _read_active_project(active_project_id, channel):
        case ProjectRead() as project:
            return ProjectListRead((project,))
        case ServedProjectUnknown() as unknown:
            return unknown
        case ReadUnavailable() as unavailable:
            return unavailable
        case DurableStateCorrupt() as corrupt:
            return corrupt
        case _ as unreachable:
            assert_never(unreachable)


def get_project(
    addressed_project_id: ProjectId,
    active_project_id: ProjectId | None,
    channel: HostConfigurationChannel,
) -> GetProjectResult:
    if active_project_id is None or addressed_project_id != active_project_id:
        return ServedProjectUnknown()
    return _read_active_project(active_project_id, channel)
