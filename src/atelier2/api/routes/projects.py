from __future__ import annotations

from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    parse_limit,
    require_json_media_dependency,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import ApiProblem
from atelier2.api.projection.projects import project_resource
from atelier2.api.wire.requests import CreateProjectRequestResource
from atelier2.api.wire.resources import (
    InvalidFieldResource,
    ProjectPageResource,
    ProjectResource,
)
from atelier2.application.publish_project import (
    ProjectCreated,
    ProjectNameCollision,
    UnpublishableProject,
)
from atelier2.application.read_projects import (
    ProjectNotFound,
    ProjectRead,
    ProjectsListed,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.projects import ProjectId

router = APIRouter()


@router.post(
    API_PREFIX + "/projects",
    response_model=ProjectResource,
    status_code=HTTPStatus.CREATED,
)
async def create_project_route(
    body: CreateProjectRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.create_project(body.name),
    )
    match result:
        case ProjectCreated(project):
            return resource_response(project_resource(project), HTTPStatus.CREATED)
        case UnpublishableProject():
            raise ApiProblem("invalid-request")
        case ProjectNameCollision():
            raise ApiProblem("project-name-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.get(API_PREFIX + "/projects", response_model=ProjectPageResource)
async def list_projects_route(
    after: str | None = None,
    limit: str = "50",
    context: ApiContext = api_context_dependency,
) -> ProjectPageResource:
    parsed_after = None
    if after is not None:
        try:
            parsed_after = ProjectId(after)
        except ValueError:
            raise ApiProblem(
                "invalid-request",
                invalid_fields=(
                    InvalidFieldResource(
                        path="query/after", reason="not a project id this list accepts"
                    ),
                ),
            ) from None
    parsed_limit = parse_limit(limit)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_projects(parsed_after, parsed_limit),
    )
    match result:
        case ProjectsListed(items, next_after):
            return ProjectPageResource(
                items=tuple(project_resource(project) for project in items),
                next_after_project_id=(
                    None if next_after is None else next_after.value
                ),
            )
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.get(API_PREFIX + "/projects/{project_id}", response_model=ProjectResource)
async def get_project_route(
    project_id: str, context: ApiContext = api_context_dependency
) -> ProjectResource:
    try:
        parsed_id = ProjectId(project_id)
    except ValueError:
        raise ApiProblem("project-unknown") from None
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_project(parsed_id),
    )
    match result:
        case ProjectRead(project):
            return project_resource(project)
        case ProjectNotFound():
            raise ApiProblem("project-unknown")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
