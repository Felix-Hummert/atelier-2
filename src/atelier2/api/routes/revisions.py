from __future__ import annotations

from http import HTTPStatus
from typing import assert_never

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from atelier2.api._support import (
    parse_limit,
    parse_revision_view,
    require_media_type,
    resource_response,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import PROJECTION_LIMIT_DETAIL, ApiProblem
from atelier2.api.projection.workflows import (
    workflow_revision_detail_resource,
    workflow_revision_page_resource,
)
from atelier2.api.references import InvalidRevisionHash, parse_revision_hash
from atelier2.api.wire.requests import RevisionListingView
from atelier2.api.wire.resources import (
    AnyWorkflowRevisionPageResource,
    CatalogNameResolutionResource,
    VersionedWorkflowRevisionPageResource,
    WorkflowRevisionDetailResource,
    WorkflowRevisionPageResource,
    WorkflowRevisionSummaryResource,
)
from atelier2.application.publish_workflow_revision import (
    PublicationCollision,
    PublicationCreated,
    PublicationExisting,
    PublicationInvalid,
)
from atelier2.application.read_workflow_revisions import (
    WorkflowRevisionNotFound,
    WorkflowRevisionRead,
    WorkflowRevisionsDescribed,
    WorkflowRevisionsListed,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.application.resolve_catalog_name import (
    CatalogNameInvalidPosition,
    CatalogNameLineageRetired,
    CatalogNameMissing,
    CatalogNameResolved,
    CatalogReferenceNonMember,
)
from atelier2.contracts.catalog_v3 import catalog_lineage_query
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflow_refusals import WorkflowRefusal

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
        lambda: context.use_cases.publish_workflow_revision(document),
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
    response_model=AnyWorkflowRevisionPageResource,
)
async def list_revisions(
    after_revision_hash: str | None = None,
    limit: str = "50",
    view: str = RevisionListingView.SUMMARY.value,
    context: ApiContext = api_context_dependency,
) -> AnyWorkflowRevisionPageResource:
    """List published revisions in the representation the caller asked for.

    The summary is the default because it is the cheaper read and because it is
    the shape this path has always answered with; the described representation
    costs a parse per revision and is therefore requested, never assumed.
    """

    after = None
    if after_revision_hash is not None:
        try:
            after = parse_revision_hash(after_revision_hash)
        except InvalidRevisionHash as error:
            raise ApiProblem("invalid-revision-hash") from error
    parsed_limit = parse_limit(limit)
    if parse_revision_view(view) is RevisionListingView.SUMMARY:
        return await _summary_page(context, after, parsed_limit)
    return await _described_page(context, after, parsed_limit)


async def _summary_page(
    context: ApiContext, after: WorkflowRevisionHash | None, limit: int
) -> WorkflowRevisionPageResource:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_workflow_revisions(after, limit),
    )
    match result:
        case WorkflowRevisionsListed(revision_hashes, next_after):
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
        case ProjectionTooLarge():
            raise ApiProblem("temporarily-unavailable", PROJECTION_LIMIT_DETAIL)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


async def _described_page(
    context: ApiContext, after: WorkflowRevisionHash | None, limit: int
) -> VersionedWorkflowRevisionPageResource:
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_described_workflow_revisions(after, limit),
    )
    match result:
        case WorkflowRevisionsDescribed():
            return workflow_revision_page_resource(result)
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("temporarily-unavailable", PROJECTION_LIMIT_DETAIL)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


def _asked_position(position: str) -> object:
    try:
        return int(position)
    except ValueError:
        return position


@router.get(
    API_PREFIX + "/workflow-revisions/by-name/{name}",
    response_model=CatalogNameResolutionResource,
)
async def get_revision_by_name(
    name: str,
    position: str = "head",
    context: ApiContext = api_context_dependency,
) -> CatalogNameResolutionResource:
    """Answer which revision a catalog name holds, and nothing about running it.

    Registered before `/{revision_hash}` on purpose: that path would otherwise
    claim `by-name` and refuse every name as a malformed hash.
    """

    try:
        query = catalog_lineage_query(name)
    except ValueError as error:
        raise ApiProblem("catalog-name-not-found") from error
    # The application owns which positions are legal; the route only turns the
    # query string into the value it judges, so "7" and "head" travel as the
    # types that rule reads and "later" arrives as the string it refuses.
    asked: object = position if position == "head" else _asked_position(position)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.resolve_catalog_name(
            RevisionKind.WORKFLOW, query, asked
        ),
    )
    match result:
        case CatalogNameResolved(lineage_id, revision, revision_number, display_name):
            return CatalogNameResolutionResource(
                display_name=display_name.value,
                lineage_id=lineage_id.value,
                revision_hash=revision.revision_hash.value,
                revision_number=revision_number,
            )
        case CatalogNameLineageRetired():
            raise ApiProblem("catalog-lineage-retired")
        case CatalogNameMissing():
            raise ApiProblem("catalog-name-not-found")
        case CatalogNameInvalidPosition():
            raise ApiProblem("invalid-catalog-position")
        case CatalogReferenceNonMember():
            raise ApiProblem("catalog-revision-not-a-member")
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
        lambda: context.use_cases.get_workflow_revision(parsed),
    )
    match result:
        case WorkflowRevisionRead(projection):
            return workflow_revision_detail_resource(projection)
        case WorkflowRevisionNotFound():
            raise ApiProblem("workflow-revision-not-found")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case ProjectionTooLarge():
            raise ApiProblem("temporarily-unavailable", PROJECTION_LIMIT_DETAIL)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
