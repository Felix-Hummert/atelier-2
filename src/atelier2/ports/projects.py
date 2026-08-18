from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.projects import Project, ProjectId, ProjectName
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


@dataclass(frozen=True)
class ProjectPage:
    items: tuple[Project, ...]
    next_after: ProjectId | None


@dataclass(frozen=True)
class ReadUnavailable:
    detail: str | None = None


type ListProjectsResult = ProjectPage | ReadUnavailable | DurableStateCorrupt


@dataclass(frozen=True)
class ProjectCreated:
    project: Project


@dataclass(frozen=True)
class ProjectNameCollision:
    pass


type CreateProjectResult = (
    ProjectCreated
    | ProjectNameCollision
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class ProjectCatalog(Protocol):
    def create_project(self, name: ProjectName) -> CreateProjectResult: ...

    def project(self, project_id: ProjectId) -> Project | None: ...

    def list_projects(
        self, after: ProjectId | None, limit: int
    ) -> ListProjectsResult: ...
