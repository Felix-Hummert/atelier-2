from __future__ import annotations

import secrets
from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.schema import projects
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.projects import Project, ProjectId, ProjectName
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.projects import (
    CreateProjectResult,
    ListProjectsResult,
    ProjectCreated,
    ProjectNameCollision,
    ProjectPage,
)
from atelier2.ports.projects import ReadUnavailable as PortReadUnavailable


def project_from_record(record: Mapping[Any, Any]) -> Project:
    return Project(
        ProjectId(str(record["project_id"])), ProjectName(str(record["name"]))
    )


class DbosProjectCatalog:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_project(self, name: ProjectName) -> CreateProjectResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                held = (
                    connection.execute(
                        sa.select(projects).where(projects.c.name == name.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if held is not None:
                    return ProjectNameCollision()
                project_id = ProjectId(secrets.token_hex(32))
                connection.execute(
                    projects.insert().values(
                        project_id=project_id.value, name=name.value
                    )
                )
                return ProjectCreated(Project(project_id, name))
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def project(self, project_id: ProjectId) -> Project | None:
        with self._engine.connect() as connection:
            record = (
                connection.execute(
                    sa.select(projects).where(projects.c.project_id == project_id.value)
                )
                .mappings()
                .one_or_none()
            )
        return None if record is None else project_from_record(record)

    def list_projects(self, after: ProjectId | None, limit: int) -> ListProjectsResult:
        if type(limit) is not int or not 1 <= limit <= MAXIMUM_PAGE_ITEMS:
            raise ValueError(
                f"project page limit must be an integer from 1 to {MAXIMUM_PAGE_ITEMS}"
            )
        try:
            with self._engine.connect() as connection:
                statement = sa.select(projects)
                if after is not None:
                    statement = statement.where(projects.c.project_id > after.value)
                records = tuple(
                    connection.execute(
                        statement.order_by(projects.c.project_id).limit(limit + 1)
                    ).mappings()
                )
                has_more = len(records) > limit
                items = tuple(project_from_record(record) for record in records[:limit])
                return ProjectPage(
                    items,
                    items[-1].project_id if has_more and items else None,
                )
        except (OperationalError, PoolTimeoutError):
            return PortReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
