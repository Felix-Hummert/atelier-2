"""The queue's two HTTP doors: admit one observed item, list the admitted ones.

Phase A gave the queue an application caller and a read; nothing in production
could reach either without a door. These are those doors. Neither invents a
domain outcome: the POST maps exactly the outcomes `admit_queue_item` already
answers with, and the GET maps the page `list_admitted_queue_items` returns.
"""

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
from atelier2.api.wire.requests import AdmitQueueItemRequestResource
from atelier2.api.wire.resources import (
    AdmittedQueueItemResource,
    InvalidFieldResource,
    QueueItemPageResource,
)
from atelier2.application.admit_queue_item import AdmittedQueueItemsListed
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    AdmitQueueItem,
    QueueAdmission,
    QueueAdmissionAlreadyCurrent,
    QueueAdmissionAlreadyDecided,
    QueueAdmissionRationale,
    QueueAdmissionRevisionConflict,
    QueueItemAdmitted,
    QueueItemId,
    QueueItemSnapshot,
    QueueProjectionRevision,
    TrackerItemReference,
    WorkItemReference,
)

router = APIRouter()

QUEUE_ADMISSIONS_PATH = API_PREFIX + "/queue-admissions"
QUEUE_ITEMS_PATH = API_PREFIX + "/queue-items"


@router.post(
    QUEUE_ADMISSIONS_PATH,
    response_model=AdmittedQueueItemResource,
    status_code=HTTPStatus.CREATED,
    responses={HTTPStatus.OK: {"model": AdmittedQueueItemResource}},
)
async def admit_queue_item_route(
    request: AdmitQueueItemRequestResource,
    context: ApiContext = api_context_dependency,
    _media: None = Depends(require_json_media_dependency),
) -> JSONResponse:
    """Admit one observed item, or answer the queue's own refusal by name."""

    try:
        command = AdmitQueueItem(
            WorkItemReference(
                ProjectId(request.project_id),
                TrackerItemReference(request.tracker_item_reference),
            ),
            QueueAdmission(
                CatalogLineageId(request.workflow_lineage_id),
                QueueAdmissionRationale(request.rationale),
            ),
            QueueProjectionRevision(request.expected_revision),
        )
    except (TypeError, ValueError) as error:
        raise ApiProblem("invalid-request") from error

    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.admit_queue_item(command),
    )
    match result:
        case QueueItemAdmitted(item_reference, admission, revision):
            status = HTTPStatus.CREATED
        case QueueAdmissionAlreadyCurrent(item_reference, admission, revision):
            status = HTTPStatus.OK
        case QueueAdmissionRevisionConflict():
            raise ApiProblem("queue-admission-revision-conflict")
        case QueueAdmissionAlreadyDecided():
            raise ApiProblem("queue-admission-already-decided")
        case WriteUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)
    return resource_response(
        _admitted_item_resource(item_reference, admission, revision), status
    )


@router.get(QUEUE_ITEMS_PATH, response_model=QueueItemPageResource)
async def list_queue_items_route(
    after: str | None = None,
    limit: str = "50",
    context: ApiContext = api_context_dependency,
) -> QueueItemPageResource:
    """List admitted items, each with its binding and rationale; empty is a page."""

    after_item_id = None if after is None else _parse_after(after)
    parsed_limit = parse_limit(limit)
    result = await run_control_query(
        context.control_runner,
        lambda: context.use_cases.list_admitted_queue_items(
            after_item_id, parsed_limit
        ),
    )
    match result:
        case AdmittedQueueItemsListed(items, next_after):
            return QueueItemPageResource(
                items=tuple(_snapshot_resource(snapshot) for snapshot in items),
                next_after=None if next_after is None else next_after.value,
            )
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case DurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


def _parse_after(value: str) -> QueueItemId:
    try:
        return QueueItemId(value)
    except ValueError as error:
        raise ApiProblem(
            "invalid-request",
            invalid_fields=(
                InvalidFieldResource(
                    path="query/after",
                    reason="not a queue item id this list resumes from",
                ),
            ),
        ) from error


def _snapshot_resource(snapshot: QueueItemSnapshot) -> AdmittedQueueItemResource:
    admission = snapshot.admission
    if admission is None:
        # `list_admitted_items` answers only ADMITTED rows, and an ADMITTED
        # snapshot carries its admission by construction; a None here is durable
        # state that no sequence of writes could produce.
        raise ApiProblem("durable-state-corrupt")
    return _admitted_item_resource(
        snapshot.item_reference, admission, snapshot.revision
    )


def _admitted_item_resource(
    item_reference: WorkItemReference,
    admission: QueueAdmission,
    revision: QueueProjectionRevision,
) -> AdmittedQueueItemResource:
    return AdmittedQueueItemResource(
        project_id=item_reference.project.value,
        tracker_item_reference=item_reference.tracker_item.value,
        item_id=item_reference.item_id.value,
        revision=revision.value,
        workflow_lineage_id=admission.workflow_lineage_id.value,
        rationale=admission.rationale.value,
    )
