from __future__ import annotations

from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    parse_limit,
    require_media_type,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import ApiProblem
from atelier2.api.projection.workflows import workflow_revision_detail_resource
from atelier2.api.references import InvalidRevisionHash, parse_revision_hash
from atelier2.api.wire.resources import (
    WorkflowRevisionDetailResource,
    WorkflowRevisionPageResource,
    WorkflowRevisionSummaryResource,
)
from atelier2.application.publish_workflow_revision import (
    DurableStateCorrupt,
    PublicationCollision,
    PublicationCreated,
    PublicationExisting,
    PublicationInvalid,
    WriteUnavailable,
    publish_workflow_revision,
)
from atelier2.contracts.workflow_refusals import WorkflowRefusal
from atelier2.ports.workflow_revisions import (
    QueryDurableStateCorrupt,
    ReadUnavailable,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
    WorkflowRevisionPage,
)

router = APIRouter()


@router.post(
    API_PREFIX + "/workflow-revisions",
    response_model=WorkflowRevisionDetailResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": WorkflowRevisionDetailResource}},
)
async def publish_revision(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    require_media_type(request, "application/yaml")
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: publish_workflow_revision(
            document,
            context.ports.workflow_revision_publisher,
            context.ports.workflow_document_parser,
            context.workflow_projection_limit,
        ),
    )
    match result:
        case PublicationCreated(projection):
            status = HTTPStatus.CREATED
        case PublicationExisting(projection):
            status = HTTPStatus.OK
        case PublicationInvalid(detail, WorkflowRefusal()):
            raise ApiProblem("invalid-workflow-document", detail)
        case PublicationInvalid():
            raise ApiProblem("invalid-workflow-document")
        case PublicationCollision():
            raise ApiProblem("revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    resource = workflow_revision_detail_resource(projection)
    return resource_response(resource, status)


@router.get(
    API_PREFIX + "/workflow-revisions",
    response_model=WorkflowRevisionPageResource,
)
async def list_revisions(
    after_revision_hash: str | None = None,
    limit: str = "50",
    context: ApiContext = api_context_dependency,
) -> WorkflowRevisionPageResource:
    after = None
    if after_revision_hash is not None:
        try:
            after = parse_revision_hash(after_revision_hash)
        except InvalidRevisionHash as error:
            raise ApiProblem("invalid-revision-hash") from error
    parsed_limit = parse_limit(limit)
    result = await run_control_query(
        context.control_runner,
        lambda: context.ports.workflow_revision_queries.list_workflow_revisions(
            after, parsed_limit
        ),
    )
    match result:
        case WorkflowRevisionPage(revision_hashes, next_after):
            return WorkflowRevisionPageResource(
                items=tuple(
                    WorkflowRevisionSummaryResource(revision_hash=value.value)
                    for value in revision_hashes
                ),
                next_after_revision_hash=(
                    None if next_after is None else next_after.value
                ),
            )
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case QueryDurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


@router.get(
    API_PREFIX + "/workflow-revisions/{revision_hash}",
    response_model=WorkflowRevisionDetailResource,
)
async def get_revision(
    revision_hash: str, context: ApiContext = api_context_dependency
) -> WorkflowRevisionDetailResource:
    try:
        parsed = parse_revision_hash(revision_hash)
    except InvalidRevisionHash as error:
        raise ApiProblem("invalid-revision-hash") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.ports.workflow_revision_queries.get_workflow_revision(
            parsed, context.workflow_projection_limit
        ),
    )
    match result:
        case WorkflowRevisionFound(projection):
            return workflow_revision_detail_resource(projection)
        case WorkflowRevisionMissing():
            raise ApiProblem("workflow-revision-not-found")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case QueryDurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
