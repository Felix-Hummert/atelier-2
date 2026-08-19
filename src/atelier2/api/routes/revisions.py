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
from atelier2.api.problems import (
    PROJECTION_LIMIT_DETAIL,
    ApiProblem,
    budget_document_problem_code,
    schema_document_problem_code,
    tool_grant_document_problem_code,
)
from atelier2.api.projection.workflows import (
    workflow_revision_detail_resource,
    workflow_revision_page_resource,
)
from atelier2.api.references import InvalidRevisionHash, parse_revision_hash
from atelier2.api.wire.requests import (
    AdmitCatalogMemberRequestResource,
    FoundCatalogLineageRequestResource,
    RevisionListingView,
)
from atelier2.api.wire.resources import (
    AnyWorkflowRevisionPageResource,
    BudgetRevisionResource,
    CatalogAdmissionResource,
    CatalogNameResolutionResource,
    SchemaRevisionResource,
    ToolGrantRevisionResource,
    VersionedWorkflowRevisionPageResource,
    WorkflowRevisionDetailResource,
    WorkflowRevisionPageResource,
    WorkflowRevisionSummaryResource,
)
from atelier2.application.admit_catalog_member import (
    CatalogAuthoredNameRestated,
    CatalogDisplayNameInvalid,
    CatalogExplicitNameRequired,
    CatalogRevisionUnpublished,
)
from atelier2.application.publish_budget_revision import (
    BudgetPublicationCollision,
    BudgetPublicationCreated,
    BudgetPublicationExisting,
    BudgetPublicationInvalid,
)
from atelier2.application.publish_schema_revision import (
    SchemaPublicationCollision,
    SchemaPublicationCreated,
    SchemaPublicationExisting,
    SchemaPublicationInvalid,
)
from atelier2.application.publish_tool_grant_revision import (
    ToolGrantPublicationCollision,
    ToolGrantPublicationCreated,
    ToolGrantPublicationExisting,
    ToolGrantPublicationInvalid,
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
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogAdmissionExisting,
    CatalogAdmissionKindMismatch,
    CatalogAdmissionLineageMissing,
    CatalogAdmissionNameHeld,
    CatalogAdmissionRetired,
    CatalogAdmissionRevisionOwned,
    CatalogAdmissionUnpublished,
    CatalogLineageDisplayName,
    CatalogLineageFounded,
    CatalogLineageId,
    CatalogLineageIdMismatch,
    CatalogMemberAdmitted,
    catalog_lineage_query,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflow_refusals import WorkflowRefusal

router = APIRouter()


@router.post(
    API_PREFIX + "/schema-revisions",
    response_model=SchemaRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": SchemaRevisionResource}},
)
async def publish_schema_revision_route(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    require_media_type(request, "application/json")
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_schema_revision(document),
    )
    match result:
        case SchemaPublicationCreated(revision):
            status = HTTPStatus.CREATED
        case SchemaPublicationExisting(revision):
            status = HTTPStatus.OK
        case SchemaPublicationInvalid(verdict):
            raise ApiProblem(
                schema_document_problem_code(verdict.refusal), str(verdict)
            )
        case SchemaPublicationCollision():
            raise ApiProblem("schema-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        SchemaRevisionResource(schema_revision_hash=revision.revision_hash.value),
        status,
    )


@router.post(
    API_PREFIX + "/budget-revisions",
    response_model=BudgetRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": BudgetRevisionResource}},
)
async def publish_budget_revision_route(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    require_media_type(request, "application/json")
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_budget_revision(document),
    )
    match result:
        case BudgetPublicationCreated(revision):
            status = HTTPStatus.CREATED
        case BudgetPublicationExisting(revision):
            status = HTTPStatus.OK
        case BudgetPublicationInvalid(verdict):
            raise ApiProblem(budget_document_problem_code(verdict.reason), str(verdict))
        case BudgetPublicationCollision():
            raise ApiProblem("budget-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        BudgetRevisionResource(budget_revision_hash=revision.revision_hash.value),
        status,
    )


@router.post(
    API_PREFIX + "/tool-grant-revisions",
    response_model=ToolGrantRevisionResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": ToolGrantRevisionResource}},
)
async def publish_tool_grant_revision_route(
    request: Request, context: ApiContext = api_context_dependency
) -> JSONResponse:
    require_media_type(request, "application/json")
    document = await request.body()
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.publish_tool_grant_revision(document),
    )
    match result:
        case ToolGrantPublicationCreated(revision):
            status = HTTPStatus.CREATED
        case ToolGrantPublicationExisting(revision):
            status = HTTPStatus.OK
        case ToolGrantPublicationInvalid(verdict):
            raise ApiProblem(
                tool_grant_document_problem_code(verdict.reason), str(verdict)
            )
        case ToolGrantPublicationCollision():
            raise ApiProblem("tool-grant-revision-collision")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        ToolGrantRevisionResource(
            tool_grant_revision_hash=revision.revision_hash.value
        ),
        status,
    )


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
                    WorkflowRevisionSummaryResource(workflow_revision_hash=value.value)
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


@router.post(
    API_PREFIX + "/workflow-lineages",
    response_model=CatalogAdmissionResource,
    status_code=201,
)
async def found_catalog_lineage_route(
    request: FoundCatalogLineageRequestResource,
    context: ApiContext = api_context_dependency,
) -> CatalogAdmissionResource:
    """Admit one revision under the name its authoring format owns."""

    try:
        display_name = (
            None
            if request.display_name is None
            else CatalogLineageDisplayName(request.display_name)
        )
        actor = CatalogActor(request.actor)
        activated_at = CatalogActivatedAt(request.activated_at)
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error

    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.found_catalog_lineage(
            RevisionKind.WORKFLOW,
            PublishedRevisionHash(request.workflow_revision_hash),
            display_name,
            actor,
            activated_at,
        ),
    )
    return _admission_resource(result)


@router.post(
    API_PREFIX + "/workflow-lineages/{lineage_id}/members",
    response_model=CatalogAdmissionResource,
    status_code=201,
)
async def admit_catalog_member_route(
    lineage_id: str,
    request: AdmitCatalogMemberRequestResource,
    context: ApiContext = api_context_dependency,
) -> CatalogAdmissionResource:
    """Admit one published revision into the lineage this path names."""

    try:
        identity = CatalogLineageId(lineage_id)
    except ValueError as error:
        raise ApiProblem("catalog-lineage-missing") from error
    try:
        actor = CatalogActor(request.actor)
        activated_at = CatalogActivatedAt(request.activated_at)
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.admit_catalog_member(
            RevisionKind.WORKFLOW,
            identity,
            PublishedRevisionHash(request.workflow_revision_hash),
            actor,
            activated_at,
        ),
    )
    return _admission_resource(result)


def _admission_resource(result: object) -> CatalogAdmissionResource:
    """One resource for both acts, because both answer the same question."""

    match result:
        case CatalogLineageFounded(lineage, revision, display_name):
            return CatalogAdmissionResource(
                display_name=display_name.value,
                lineage_id=lineage.lineage_id.value,
                workflow_revision_hash=revision.revision_hash.value,
                revision_number=1,
            )
        case CatalogMemberAdmitted(lineage, revision, revision_number, display_name):
            return CatalogAdmissionResource(
                display_name=display_name.value,
                lineage_id=lineage.lineage_id.value,
                workflow_revision_hash=revision.revision_hash.value,
                revision_number=revision_number,
            )
        case CatalogAdmissionExisting(lineage, revision, revision_number, display_name):
            # Admitting what is already admitted is the same answer, not a
            # conflict: the caller asked for a state the catalog is already in.
            return CatalogAdmissionResource(
                display_name=display_name.value,
                lineage_id=lineage.lineage_id.value,
                workflow_revision_hash=revision.revision_hash.value,
                revision_number=revision_number,
            )
        case CatalogRevisionUnpublished():
            raise ApiProblem("catalog-revision-unpublished")
        case CatalogAdmissionUnpublished():
            raise ApiProblem("catalog-revision-unpublished")
        case CatalogAdmissionNameHeld():
            raise ApiProblem("catalog-name-held")
        case CatalogAdmissionRevisionOwned():
            raise ApiProblem("catalog-revision-owned")
        case CatalogAdmissionLineageMissing():
            raise ApiProblem("catalog-lineage-missing")
        case CatalogAdmissionRetired():
            raise ApiProblem("catalog-lineage-retired")
        case (
            CatalogAuthoredNameRestated()
            | CatalogExplicitNameRequired()
            | CatalogDisplayNameInvalid()
        ):
            raise ApiProblem("invalid-request")
        case CatalogAdmissionKindMismatch() | CatalogLineageIdMismatch():
            raise ApiProblem("durable-state-corrupt")
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case WriteUnavailable():
            raise ApiProblem("temporarily-unavailable")
        case _:
            raise ApiProblem("internal-error")


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

    Registered before `/{workflow_revision_hash}` on purpose: that path would
    otherwise claim `by-name` and refuse every name as a malformed hash.
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
                workflow_revision_hash=revision.revision_hash.value,
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
    API_PREFIX + "/workflow-revisions/{workflow_revision_hash}",
    response_model=WorkflowRevisionDetailResource,
)
async def get_revision(
    workflow_revision_hash: str, context: ApiContext = api_context_dependency
) -> WorkflowRevisionDetailResource:
    try:
        parsed = parse_revision_hash(workflow_revision_hash)
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
