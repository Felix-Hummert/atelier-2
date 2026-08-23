from __future__ import annotations

from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    decode_public_project_reference_value,
    require_json_media_dependency,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import PROJECT_ROOT_PATH
from atelier2.api.problems import ApiProblem
from atelier2.api.projection.project_root import project_root_revision_resource
from atelier2.api.wire.requests import PutProjectRootRevisionRequestResource
from atelier2.api.wire.resources import ProjectRootRevisionResource
from atelier2.application.project_root import (
    HostConfigurationUnreadable,
    ProjectRootMissing,
    ProjectRootProjectUnknown,
    ProjectRootRead,
    ProjectRootRevisionConflict,
    ProjectRootRevisionPublished,
    ProjectRootRevisionUnchanged,
    UnpublishableProjectRootRevision,
)
from atelier2.application.refusals import DurableStateCorrupt

router = APIRouter()


def _addressed_project_id(public_project_reference: str, context: ApiContext) -> str:
    return decode_public_project_reference_value(
        public_project_reference, context.limits
    ).value


@router.put(
    PROJECT_ROOT_PATH,
    response_model=ProjectRootRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": ProjectRootRevisionResource}},
)
async def put_project_root_revision_route(
    public_project_reference: str,
    body: PutProjectRootRevisionRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    project_id = _addressed_project_id(public_project_reference, context)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_project_root_revision(
            project_id, body.revision_number, body.root_path
        ),
    )
    match result:
        case ProjectRootRevisionPublished(stored):
            status = HTTPStatus.CREATED
        case ProjectRootRevisionUnchanged(stored):
            status = HTTPStatus.OK
        case ProjectRootProjectUnknown():
            raise ApiProblem("project-unknown")
        case UnpublishableProjectRootRevision():
            raise ApiProblem("invalid-request")
        case ProjectRootRevisionConflict():
            raise ApiProblem("project-root-revision-conflict")
        case HostConfigurationUnreadable(detail):
            raise ApiProblem("host-configuration-unreadable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(project_root_revision_resource(stored), status)


@router.get(
    PROJECT_ROOT_PATH,
    response_model=ProjectRootRevisionResource,
)
async def get_project_root_revision_route(
    public_project_reference: str,
    context: ApiContext = api_context_dependency,
) -> ProjectRootRevisionResource:
    project_id = _addressed_project_id(public_project_reference, context)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_project_root_revision(project_id),
    )
    match result:
        case ProjectRootRead(revision):
            return project_root_revision_resource(revision)
        case ProjectRootMissing():
            raise ApiProblem("project-root-missing")
        case ProjectRootProjectUnknown():
            raise ApiProblem("project-unknown")
        case HostConfigurationUnreadable(detail):
            raise ApiProblem("host-configuration-unreadable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
