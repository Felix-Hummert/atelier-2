"""Creating a project, as a decision rather than a port answer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.projects import Project, ProjectName
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.projects import ProjectCatalog
from atelier2.ports.projects import ProjectCreated as PortProjectCreated
from atelier2.ports.projects import ProjectNameCollision as PortProjectNameCollision


@dataclass(frozen=True)
class ProjectCreated:
    project: Project


@dataclass(frozen=True)
class ProjectNameCollision:
    pass


@dataclass(frozen=True)
class UnpublishableProject:
    """The authored name does not make one project name."""


type CreateProjectResult = (
    ProjectCreated
    | ProjectNameCollision
    | UnpublishableProject
    | WriteUnavailable
    | DurableStateCorrupt
)


def create_project(name: str, catalog: ProjectCatalog) -> CreateProjectResult:
    try:
        project_name = ProjectName(name)
    except (TypeError, ValueError):
        return UnpublishableProject()
    match catalog.create_project(project_name):
        case PortProjectCreated(project):
            return ProjectCreated(project)
        case PortProjectNameCollision():
            return ProjectNameCollision()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
