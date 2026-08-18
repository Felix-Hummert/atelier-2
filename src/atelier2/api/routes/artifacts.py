from __future__ import annotations

from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    require_media_type,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import ApiProblem, artifact_problem_code
from atelier2.api.wire.resources import ArtifactResource
from atelier2.application.publish_artifact import (
    ArtifactPublicationCreated,
    ArtifactPublicationExisting,
    ArtifactPublicationInvalid,
)
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable

router = APIRouter()


@router.post(
    API_PREFIX + "/artifacts",
    response_model=ArtifactResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": ArtifactResource}},
)
async def publish_artifact_route(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    """Publish material by its bytes, and answer with the address they hash to.

    The media type is opaque bytes rather than JSON because an artifact is
    whatever a caller needs an agent to read; what an order made of it must be
    is decided later, against the schema its document pinned.
    """
    require_media_type(request, "application/octet-stream")
    content = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_artifact(content),
    )
    match result:
        case ArtifactPublicationCreated(artifact):
            status = HTTPStatus.CREATED
        case ArtifactPublicationExisting(artifact):
            status = HTTPStatus.OK
        case ArtifactPublicationInvalid(verdict):
            raise ApiProblem(artifact_problem_code(verdict.refusal), str(verdict))
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        ArtifactResource(artifact_hash=artifact.artifact_hash.value), status
    )
