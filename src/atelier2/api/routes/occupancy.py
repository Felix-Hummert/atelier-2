from __future__ import annotations

from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    require_json_media_dependency,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import OCCUPANCY_PATH
from atelier2.api.problems import ApiProblem
from atelier2.api.projection.occupancy import occupancy_revision_resource
from atelier2.api.references import (
    InvalidPublicProjectReference,
    decode_public_project_reference,
)
from atelier2.api.wire.requests import PutOccupancyRevisionRequestResource
from atelier2.api.wire.resources import OccupancyRevisionResource
from atelier2.application.occupancy import (
    OccupancyLineageInvalid,
    OccupancyMissing,
    OccupancyProjectUnknown,
    OccupancyRead,
    OccupancyRevisionCollision,
    OccupancyRevisionConflict,
    OccupancyRevisionPublished,
    OccupancyRevisionUnchanged,
    UnpublishableOccupancy,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)

router = APIRouter()


def _addressed_project_id(public_project_reference: str) -> str:
    try:
        return decode_public_project_reference(public_project_reference).value
    except InvalidPublicProjectReference as error:
        raise ApiProblem("invalid-public-project-reference") from error


@router.put(
    OCCUPANCY_PATH,
    response_model=OccupancyRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": OccupancyRevisionResource}},
)
async def put_occupancy_revision_route(
    public_project_reference: str,
    lineage_id: str,
    body: PutOccupancyRevisionRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    project_id = _addressed_project_id(public_project_reference)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_occupancy_revision(
            project_id,
            lineage_id,
            body.revision_number,
            tuple(
                (binding.role, binding.agent_configuration_revision_hash)
                for binding in body.bindings
            ),
        ),
    )
    match result:
        case OccupancyRevisionPublished(stored):
            status = HTTPStatus.CREATED
        case OccupancyRevisionUnchanged(stored):
            status = HTTPStatus.OK
        case OccupancyProjectUnknown():
            raise ApiProblem("project-unknown")
        case OccupancyLineageInvalid():
            raise ApiProblem("catalog-lineage-missing")
        case UnpublishableOccupancy():
            raise ApiProblem("invalid-request")
        case OccupancyRevisionConflict():
            raise ApiProblem("occupancy-revision-conflict")
        case OccupancyRevisionCollision():
            raise ApiProblem("occupancy-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(occupancy_revision_resource(stored), status)


@router.get(
    OCCUPANCY_PATH,
    response_model=OccupancyRevisionResource,
)
async def get_occupancy_revision_route(
    public_project_reference: str,
    lineage_id: str,
    context: ApiContext = api_context_dependency,
) -> OccupancyRevisionResource:
    project_id = _addressed_project_id(public_project_reference)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.get_occupancy_revision(project_id, lineage_id),
    )
    match result:
        case OccupancyRead(revision):
            return occupancy_revision_resource(revision)
        case OccupancyMissing():
            raise ApiProblem("occupancy-missing")
        case OccupancyProjectUnknown():
            raise ApiProblem("project-unknown")
        case OccupancyLineageInvalid():
            raise ApiProblem("catalog-lineage-missing")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
