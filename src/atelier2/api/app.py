from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from http import HTTPStatus
from typing import assert_never

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel

from atelier2.api.limits import (
    ApiLimitExceeded,
    ApiLimits,
    RequestBodyLimitMiddleware,
)
from atelier2.api.models import (
    AnswerWaitRequestResource,
    HealthResource,
    OperatorFoundDeterminationResource,
    ReconcileRunRequestResource,
    RunPageResource,
    RunResource,
    StartRunRequestResource,
    WorkflowRevisionDetailResource,
    WorkflowRevisionPageResource,
    WorkflowRevisionSummaryResource,
    run_resource,
    workflow_revision_detail_resource,
)
from atelier2.api.openapi import API_PREFIX, install_custom_openapi
from atelier2.api.problems import ApiProblem, install_problem_handlers
from atelier2.api.references import (
    MAX_SIGNED_INT64,
    InvalidEventCursor,
    InvalidPublicRunReference,
    InvalidRevisionHash,
    decode_canonical_base64,
    decode_public_run_reference,
    encode_public_run_reference,
    parse_event_cursor,
    parse_revision_hash,
)
from atelier2.api.stream import (
    BoundedQueryRunner,
    EventPollBackoff,
    PreparedEventStream,
    QueryAdmissionTimeout,
    stream_server_events,
)
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    AnswerBytesConflict,
    AnswerExistingApplied,
    AnswerExistingPending,
    AnswerRevisionConflict,
    AnswerStateConflict,
    NodeMissing,
    RunMissing,
    answer_wait_result,
)
from atelier2.application.publish_workflow_revision import (
    DurableStateCorrupt,
    PublicationCollision,
    PublicationCreated,
    PublicationExisting,
    PublicationInvalid,
    WorkflowPublicationLimits,
    WriteUnavailable,
    publish_workflow_revision,
)
from atelier2.application.reconcile_effect import (
    ReconciliationAcceptedPending,
    ReconciliationCommandConflict,
    ReconciliationDeterminationConflict,
    ReconciliationExistingApplied,
    ReconciliationExistingPending,
    ReconciliationExistingRejected,
    ReconciliationStale,
    ReconciliationTargetMissing,
)
from atelier2.application.reconcile_run import ReconcileRunRequest, reconcile_run
from atelier2.application.start_published_run import (
    RevisionMissing,
    RunCreated,
    RunExisting,
    RunIdentityConflict,
    start_published_run,
)
from atelier2.contracts.effects import (
    EffectId,
    EffectIntentStateVersion,
    EffectResult,
    OperatorAuthoritativeAbsence,
    OperatorFoundEffect,
    ReconcileActor,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    SubmitWaitAnswerRequest,
    is_canonical_integer_bytes,
)
from atelier2.contracts.runs import RunId
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    StartPublishedRunRequest,
    TransactionalWaitAnswerer,
)
from atelier2.ports.effects import TransactionalEffectReconcileCommander
from atelier2.ports.run_events import (
    CursorAhead,
    EventHistoryCorrupt,
    RunEventQueries,
    StreamReady,
)
from atelier2.ports.run_queries import (
    RunFound,
    RunPage,
    RunProjection,
    RunQueries,
    RunQueryMissing,
)
from atelier2.ports.workflow_revisions import (
    PROJECTION_LIMIT_DETAIL,
    DurableProjectionLimit,
    QueryDurableStateCorrupt,
    ReadUnavailable,
    WorkflowDocumentParser,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
    WorkflowRevisionPage,
    WorkflowRevisionPublisher,
    WorkflowRevisionQueries,
)


@dataclass(frozen=True)
class ApiPorts:
    workflow_revision_publisher: WorkflowRevisionPublisher
    published_run_starter: DurablePublishedRunStarter
    wait_answerer: TransactionalWaitAnswerer
    reconcile_commander: TransactionalEffectReconcileCommander
    workflow_revision_queries: WorkflowRevisionQueries
    run_queries: RunQueries
    run_event_queries: RunEventQueries
    workflow_document_parser: WorkflowDocumentParser


def create_app(
    *,
    source_commit: str,
    source_tree: str,
    ports: ApiPorts,
    limits: ApiLimits,
    event_poll_backoff: EventPollBackoff,
) -> FastAPI:
    if not source_commit:
        raise ValueError("source_commit must be injected at application construction")
    if not source_tree:
        raise ValueError("source_tree must be injected at application construction")
    app = FastAPI(
        title="Atelier 2 durable workflow API",
        version="1",
        openapi_url=API_PREFIX + "/openapi.json",
        docs_url=None,
        redoc_url=None,
    )
    install_problem_handlers(app)
    app.add_middleware(
        RequestBodyLimitMiddleware,
        maximum_body_bytes=limits.maximum_request_body_bytes,
        api_prefix=API_PREFIX,
    )
    admission_timeout_seconds = limits.maximum_query_admission_wait_milliseconds / 1_000
    runner = BoundedQueryRunner(
        limits.maximum_control_queries,
        admission_timeout_seconds=admission_timeout_seconds,
    )
    event_runner = BoundedQueryRunner(
        limits.maximum_event_poll_queries,
        admission_timeout_seconds=admission_timeout_seconds,
    )
    workflow_projection_limit = WorkflowPublicationLimits(
        maximum_document_bytes=min(
            limits.maximum_request_body_bytes,
            limits.maximum_base64_decoded_bytes,
        ),
        maximum_nodes=limits.maximum_workflow_nodes,
        maximum_string_characters=limits.maximum_field_characters,
        maximum_payload_bytes=min(
            limits.maximum_decoded_payload_bytes,
            limits.maximum_base64_decoded_bytes,
        ),
    )

    @app.get(API_PREFIX + "/health", response_model=HealthResource)
    async def health() -> HealthResource:
        return HealthResource(
            status="serving", source_commit=source_commit, source_tree=source_tree
        )

    @app.post(
        API_PREFIX + "/workflow-revisions",
        response_model=WorkflowRevisionDetailResource,
        status_code=HTTPStatus.CREATED,
        responses={HTTPStatus.OK: {"model": WorkflowRevisionDetailResource}},
    )
    async def publish_revision(request: Request) -> JSONResponse:
        _require_media_type(request, "application/yaml")
        document = await request.body()
        result = await _run_control_query(
            runner,
            lambda: publish_workflow_revision(
                document,
                ports.workflow_revision_publisher,
                ports.workflow_document_parser,
                workflow_projection_limit,
            ),
        )
        match result:
            case PublicationCreated(projection):
                status = HTTPStatus.CREATED
            case PublicationExisting(projection):
                status = HTTPStatus.OK
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
        return _resource_response(resource, status)

    @app.get(
        API_PREFIX + "/workflow-revisions",
        response_model=WorkflowRevisionPageResource,
    )
    async def list_revisions(
        after_revision_hash: str | None = None, limit: str = "50"
    ) -> WorkflowRevisionPageResource:
        after = None
        if after_revision_hash is not None:
            try:
                after = parse_revision_hash(after_revision_hash)
            except InvalidRevisionHash as error:
                raise ApiProblem("invalid-revision-hash") from error
        parsed_limit = _parse_limit(limit)
        result = await _run_control_query(
            runner,
            lambda: ports.workflow_revision_queries.list_workflow_revisions(
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

    @app.get(
        API_PREFIX + "/workflow-revisions/{revision_hash}",
        response_model=WorkflowRevisionDetailResource,
    )
    async def get_revision(revision_hash: str) -> WorkflowRevisionDetailResource:
        try:
            parsed = parse_revision_hash(revision_hash)
        except InvalidRevisionHash as error:
            raise ApiProblem("invalid-revision-hash") from error
        result = await _run_control_query(
            runner,
            lambda: ports.workflow_revision_queries.get_workflow_revision(
                parsed, workflow_projection_limit
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

    @app.post(
        API_PREFIX + "/runs",
        response_model=RunResource,
        status_code=HTTPStatus.CREATED,
        responses={HTTPStatus.OK: {"model": RunResource}},
    )
    async def start_run_route(
        body: StartRunRequestResource,
        _media: None = Depends(_require_json_media_dependency),
    ) -> JSONResponse:
        run_id = RunId(body.run_id)
        _require_new_run_identity(run_id, limits)
        try:
            revision_hash = parse_revision_hash(body.workflow_revision_hash)
        except InvalidRevisionHash as error:
            raise ApiProblem("invalid-revision-hash") from error
        request = StartPublishedRunRequest(run_id, revision_hash)
        result = await _run_control_query(
            runner, lambda: start_published_run(request, ports.published_run_starter)
        )
        match result:
            case RunCreated():
                status = HTTPStatus.CREATED
            case RunExisting():
                status = HTTPStatus.OK
            case RevisionMissing():
                raise ApiProblem("workflow-revision-not-found")
            case RunIdentityConflict():
                raise ApiProblem("run-identity-conflict")
            case WriteUnavailable(detail):
                raise ApiProblem("temporarily-unavailable", detail)
            case DurableStateCorrupt():
                raise ApiProblem("durable-state-corrupt")
            case _ as unreachable:
                assert_never(unreachable)
        return _resource_response(
            await _load_run_resource(
                request.run_id,
                ports.run_queries,
                runner,
                limits,
                workflow_projection_limit,
            ),
            status,
        )

    @app.get(API_PREFIX + "/runs", response_model=RunPageResource)
    async def list_runs(after: str | None = None, limit: str = "50") -> RunPageResource:
        boundary = None
        if after is not None:
            boundary = _decode_public_reference(after, limits)
        parsed_limit = _parse_limit(limit)
        result = await _run_control_query(
            runner,
            lambda: ports.run_queries.list_runs(
                boundary, parsed_limit, workflow_projection_limit
            ),
        )
        match result:
            case RunPage(runs, next_after):
                _require_run_projections(runs, limits)
                return RunPageResource(
                    items=tuple(run_resource(run) for run in runs),
                    next_after=(
                        None
                        if next_after is None
                        else encode_public_run_reference(next_after)
                    ),
                )
            case ReadUnavailable(detail):
                raise ApiProblem("temporarily-unavailable", detail)
            case QueryDurableStateCorrupt():
                raise ApiProblem("durable-state-corrupt")
            case _ as unreachable:
                assert_never(unreachable)

    @app.get(API_PREFIX + "/runs/{public_ref}", response_model=RunResource)
    async def get_run_route(public_ref: str) -> RunResource:
        return await _load_run_resource(
            _decode_public_reference(public_ref, limits),
            ports.run_queries,
            runner,
            limits,
            workflow_projection_limit,
        )

    @app.post(
        API_PREFIX + "/runs/{public_ref}/answers",
        response_model=RunResource,
        status_code=HTTPStatus.ACCEPTED,
        responses={HTTPStatus.OK: {"model": RunResource}},
    )
    async def answer_run_route(
        public_ref: str,
        body: AnswerWaitRequestResource,
        _media: None = Depends(_require_json_media_dependency),
    ) -> JSONResponse:
        run_id = _decode_public_reference(public_ref, limits)
        _require_field(body.node_id, limits)
        try:
            revision_hash = parse_revision_hash(body.revision_hash)
        except InvalidRevisionHash as error:
            raise ApiProblem("invalid-revision-hash") from error
        answer_bytes = _decode_base64(body.answer_base64, limits)
        if not is_canonical_integer_bytes(answer_bytes):
            raise ApiProblem("invalid-request")
        answer_request = SubmitWaitAnswerRequest(
            run_id, revision_hash, body.node_id, answer_bytes
        )
        result = await _run_control_query(
            runner, lambda: answer_wait_result(answer_request, ports.wait_answerer)
        )
        match result:
            case AnswerAcceptedPending() | AnswerExistingPending():
                status = HTTPStatus.ACCEPTED
            case AnswerExistingApplied():
                status = HTTPStatus.OK
            case RunMissing():
                raise ApiProblem("run-not-found")
            case NodeMissing():
                raise ApiProblem("node-not-found")
            case AnswerRevisionConflict():
                raise ApiProblem("answer-revision-conflict")
            case AnswerStateConflict():
                raise ApiProblem("answer-state-conflict")
            case AnswerBytesConflict():
                raise ApiProblem("answer-bytes-conflict")
            case WriteUnavailable(detail):
                raise ApiProblem("temporarily-unavailable", detail)
            case DurableStateCorrupt():
                raise ApiProblem("durable-state-corrupt")
            case _ as unreachable:
                assert_never(unreachable)
        return _resource_response(
            await _load_run_resource(
                run_id,
                ports.run_queries,
                runner,
                limits,
                workflow_projection_limit,
            ),
            status,
        )

    @app.post(
        API_PREFIX + "/runs/{public_ref}/reconciliations",
        response_model=RunResource,
        status_code=HTTPStatus.ACCEPTED,
        responses={HTTPStatus.OK: {"model": RunResource}},
    )
    async def reconcile_run_route(
        public_ref: str,
        body: ReconcileRunRequestResource,
        _media: None = Depends(_require_json_media_dependency),
    ) -> JSONResponse:
        run_id = _decode_public_reference(public_ref, limits)
        _require_fields(
            limits,
            body.command_id,
            body.actor,
            body.evidence,
        )
        determination_body = body.determination
        if isinstance(determination_body, OperatorFoundDeterminationResource):
            _require_field(determination_body.effect_id, limits)
            determination = OperatorFoundEffect(
                EffectId(determination_body.effect_id),
                EffectResult(_decode_base64(determination_body.result_base64, limits)),
            )
        else:
            determination = OperatorAuthoritativeAbsence()
        reconciliation_request = ReconcileRunRequest(
            run_id,
            ReconcileCommandId(body.command_id),
            EffectIntentStateVersion(body.expected_intent_state_version),
            ReconcileActor(body.actor),
            body.evidence,
            determination,
        )
        result = await _run_control_query(
            runner,
            lambda: reconcile_run(
                reconciliation_request,
                ports.run_queries,
                ports.reconcile_commander,
                workflow_projection_limit,
            ),
        )
        match result:
            case ReconciliationAcceptedPending() | ReconciliationExistingPending():
                status = HTTPStatus.ACCEPTED
            case ReconciliationExistingApplied():
                status = HTTPStatus.OK
            case RunMissing():
                raise ApiProblem("run-not-found")
            case ReconciliationTargetMissing():
                raise ApiProblem("reconciliation-target-missing")
            case ReconciliationStale():
                raise ApiProblem("reconciliation-stale")
            case ReconciliationCommandConflict():
                raise ApiProblem("reconciliation-command-conflict")
            case ReconciliationDeterminationConflict():
                raise ApiProblem("reconciliation-determination-conflict")
            case ReconciliationExistingRejected():
                raise ApiProblem("reconciliation-rejected")
            case WriteUnavailable(detail):
                raise ApiProblem("temporarily-unavailable", detail)
            case DurableStateCorrupt():
                raise ApiProblem("durable-state-corrupt")
            case _ as unreachable:
                assert_never(unreachable)
        return _resource_response(
            await _load_run_resource(
                run_id,
                ports.run_queries,
                runner,
                limits,
                workflow_projection_limit,
            ),
            status,
        )

    async def prepare_events(request: Request, public_ref: str) -> PreparedEventStream:
        _require_sse_accept(request)
        run_id = _decode_public_reference(public_ref, limits)
        cursor_headers = request.headers.getlist("last-event-id")
        if len(cursor_headers) > 1:
            raise ApiProblem("invalid-event-cursor")
        after_sequence = 0
        if cursor_headers:
            try:
                limits.require_field(cursor_headers[0])
                cursor = parse_event_cursor(cursor_headers[0])
            except (ApiLimitExceeded, InvalidEventCursor) as error:
                raise ApiProblem("invalid-event-cursor") from error
            if cursor.run_id != run_id:
                raise ApiProblem("event-cursor-run-mismatch")
            after_sequence = cursor.sequence
        result = await _run_control_query(
            runner,
            lambda: ports.run_event_queries.prepare_run_event_stream(
                run_id, after_sequence
            ),
        )
        match result:
            case StreamReady(head_sequence, terminal, first_after):
                return PreparedEventStream(run_id, first_after, head_sequence, terminal)
            case RunQueryMissing():
                raise ApiProblem("run-not-found")
            case CursorAhead():
                raise ApiProblem("event-cursor-ahead")
            case EventHistoryCorrupt() | QueryDurableStateCorrupt():
                raise ApiProblem("durable-state-corrupt")
            case ReadUnavailable(detail):
                raise ApiProblem("temporarily-unavailable", detail)
            case _ as unreachable:
                assert_never(unreachable)

    prepared_events_dependency = Depends(prepare_events)

    @app.get(
        API_PREFIX + "/runs/{public_ref}/events",
        response_class=EventSourceResponse,
    )
    async def event_stream_route(
        prepared: PreparedEventStream = prepared_events_dependency,
    ) -> AsyncIterator[ServerSentEvent]:
        async for event in stream_server_events(
            prepared,
            ports.run_event_queries,
            event_runner,
            page_size=limits.event_page_size,
            limits=limits,
            projection_limit=workflow_projection_limit,
            poll_backoff=event_poll_backoff,
        ):
            yield event

    install_custom_openapi(app)
    return app


def _resource_response(resource: BaseModel, status: HTTPStatus) -> JSONResponse:
    return JSONResponse(resource.model_dump(mode="json"), status_code=status)


async def _load_run_resource(
    run_id: RunId,
    queries: RunQueries,
    runner: BoundedQueryRunner,
    limits: ApiLimits,
    projection_limit: DurableProjectionLimit,
) -> RunResource:
    result = await _run_control_query(
        runner, lambda: queries.get_run(run_id, projection_limit)
    )
    match result:
        case RunFound(projection):
            _require_run_projections((projection,), limits)
            return run_resource(projection)
        case RunQueryMissing():
            raise ApiProblem("run-not-found")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case QueryDurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


def _require_run_projections(
    projections: tuple[RunProjection, ...], limits: ApiLimits
) -> None:
    try:
        for projection in projections:
            limits.require_run_projection(projection)
    except ValueError as error:
        raise ApiProblem("temporarily-unavailable", PROJECTION_LIMIT_DETAIL) from error


async def _run_control_query[Result](
    runner: BoundedQueryRunner, query: Callable[[], Result]
) -> Result:
    try:
        return await runner.run(query)
    except QueryAdmissionTimeout as error:
        raise ApiProblem("temporarily-unavailable") from error


def _decode_public_reference(value: str, limits: ApiLimits) -> RunId:
    try:
        limits.require_field(value)
        run_id = decode_public_run_reference(value)
        limits.require_field(run_id.value)
        return run_id
    except ApiLimitExceeded as error:
        raise ApiProblem("invalid-public-run-reference") from error
    except InvalidPublicRunReference as error:
        raise ApiProblem("invalid-public-run-reference") from error


def _decode_base64(value: str, limits: ApiLimits) -> bytes:
    try:
        limits.require_base64(value)
        decoded = decode_canonical_base64(value)
        limits.require_payload(decoded)
        return decoded
    except ApiLimitExceeded as error:
        raise ApiProblem("invalid-request", str(error)) from error
    except ValueError as error:
        raise ApiProblem("invalid-base64") from error


def _require_field(value: str, limits: ApiLimits) -> None:
    try:
        limits.require_field(value)
    except ApiLimitExceeded as error:
        raise ApiProblem("invalid-request", str(error)) from error


def _require_fields(limits: ApiLimits, *values: str) -> None:
    for value in values:
        _require_field(value, limits)


def _require_new_run_identity(run_id: RunId, limits: ApiLimits) -> None:
    try:
        limits.require_field(run_id.value)
        limits.require_public_run_reference(run_id)
        limits.require_event_cursor(run_id, MAX_SIGNED_INT64)
    except ValueError as error:
        raise ApiProblem("invalid-request") from error


def _parse_limit(value: str) -> int:
    if re.fullmatch(r"(?:[1-9]|[1-9][0-9]|100)", value) is None:
        raise ApiProblem("invalid-request")
    return int(value)


def _require_media_type(request: Request, expected: str) -> None:
    header = request.headers.get("content-type")
    if header is None:
        raise ApiProblem("unsupported-media-type")
    parts = [part.strip().lower() for part in header.split(";")]
    if (
        parts[0] != expected
        or (len(parts) == 2 and parts[1] != "charset=utf-8")
        or len(parts) > 2
    ):
        raise ApiProblem("unsupported-media-type")


async def _require_json_media_dependency(request: Request) -> None:
    _require_media_type(request, "application/json")


def _require_sse_accept(request: Request) -> None:
    header = request.headers.get("accept")
    if header is None:
        return
    for item in header.lower().split(","):
        pieces = [piece.strip() for piece in item.split(";")]
        if pieces[0] not in {"*/*", "text/event-stream"}:
            continue
        quality_parameters = [piece for piece in pieces[1:] if piece.startswith("q=")]
        if not quality_parameters:
            return
        if len(quality_parameters) > 1:
            continue
        quality = quality_parameters[0]
        if re.fullmatch(r"q=(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)", quality) is None:
            continue
        if re.fullmatch(r"q=0(?:\.0{0,3})?", quality) is None:
            return
    raise ApiProblem("not-acceptable")
