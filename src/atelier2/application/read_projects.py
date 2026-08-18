"""Reading projects, as decisions rather than port answers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.projects import Project, ProjectId
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.projects import ProjectCatalog, ProjectPage
from atelier2.ports.projects import ReadUnavailable as PortReadUnavailable


@dataclass(frozen=True)
class ProjectRead:
    project: Project


@dataclass(frozen=True)
class ProjectNotFound:
    pass


type GetProjectResult = ProjectRead | ProjectNotFound | DurableStateCorrupt


@dataclass(frozen=True)
class ProjectsListed:
    items: tuple[Project, ...]
    next_after: ProjectId | None


type ListProjectsResult = ProjectsListed | ReadUnavailable | DurableStateCorrupt


def get_project(project_id: ProjectId, catalog: ProjectCatalog) -> GetProjectResult:
    project = catalog.project(project_id)
    if project is None:
        return ProjectNotFound()
    return ProjectRead(project)


def list_projects(
    after: ProjectId | None, limit: int, catalog: ProjectCatalog
) -> ListProjectsResult:
    match catalog.list_projects(after, limit):
        case ProjectPage(items, next_after):
            return ProjectsListed(items, next_after)
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
