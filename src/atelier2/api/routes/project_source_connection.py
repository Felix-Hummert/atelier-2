from __future__ import annotations

from typing import assert_never

from fastapi import APIRouter

from atelier2.api._support import (
    decode_public_project_reference_value,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import PROJECT_SOURCE_CONNECTION_PATH
from atelier2.api.problems import ApiProblem
from atelier2.api.projection.project_source_connection import (
    project_source_connection_revision_resource,
)
from atelier2.api.wire.resources import ProjectSourceConnectionRevisionResource
from atelier2.application.project_connections import (
    PlatformConnectionUnknown,
    ProjectSourceConnectionRead,
)
from atelier2.application.read_projects import ServedProjectUnknown
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable

router = APIRouter()


@router.get(
    PROJECT_SOURCE_CONNECTION_PATH,
    response_model=ProjectSourceConnectionRevisionResource,
)
async def get_project_source_connection_route(
    public_project_reference: str,
    context: ApiContext = api_context_dependency,
) -> ProjectSourceConnectionRevisionResource:
    project_id = decode_public_project_reference_value(
        public_project_reference, context.limits
    )
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_project_source_connection(project_id),
    )
    match result:
        case ProjectSourceConnectionRead(revision):
            return project_source_connection_revision_resource(revision)
        case ServedProjectUnknown():
            raise ApiProblem("project-unknown")
        case PlatformConnectionUnknown():
            raise ApiProblem("project-source-not-connected")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
