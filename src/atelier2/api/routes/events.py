from __future__ import annotations

from collections.abc import AsyncIterator
from typing import assert_never

from fastapi import APIRouter, Depends, Request
from fastapi.sse import EventSourceResponse, ServerSentEvent

from atelier2.api._support import (
    decode_public_reference,
    load_run_projection,
    require_sse_accept,
    run_control_query,
)
from atelier2.api.context import ApiContext, api_context_dependency
from atelier2.api.limits import ApiLimitExceeded
from atelier2.api.openapi import API_PREFIX
from atelier2.api.problems import ApiProblem
from atelier2.api.references import InvalidEventCursor, parse_event_cursor
from atelier2.api.stream import PreparedEventStream, stream_server_events
from atelier2.ports.run_events import CursorAhead, EventHistoryCorrupt, StreamReady
from atelier2.ports.run_queries import RunQueryMissing
from atelier2.ports.workflow_revisions import QueryDurableStateCorrupt, ReadUnavailable

router = APIRouter()


async def prepare_events(
    request: Request,
    public_ref: str,
    context: ApiContext = api_context_dependency,
) -> PreparedEventStream:
    require_sse_accept(request)
    run_id = decode_public_reference(public_ref, context.limits)
    cursor_headers = request.headers.getlist("last-event-id")
    if len(cursor_headers) > 1:
        raise ApiProblem("invalid-event-cursor")
    after_sequence = 0
    if cursor_headers:
        try:
            context.limits.require_field(cursor_headers[0])
            cursor = parse_event_cursor(cursor_headers[0])
        except (ApiLimitExceeded, InvalidEventCursor) as error:
            raise ApiProblem("invalid-event-cursor") from error
        if cursor.run_id != run_id:
            raise ApiProblem("event-cursor-run-mismatch")
        after_sequence = cursor.sequence
    result = await run_control_query(
        context.control_runner,
        lambda: context.ports.run_event_queries.prepare_run_event_stream(
            run_id, after_sequence
        ),
    )
    match result:
        case StreamReady(head_sequence, terminal, first_after):
            return PreparedEventStream(
                run_id,
                first_after,
                head_sequence,
                terminal,
                await load_run_projection(
                    run_id,
                    context.ports.run_queries,
                    context.control_runner,
                    context.limits,
                    context.workflow_projection_limit,
                ),
            )
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


@router.get(
    API_PREFIX + "/runs/{public_ref}/events",
    response_class=EventSourceResponse,
)
async def event_stream_route(
    prepared: PreparedEventStream = prepared_events_dependency,
    context: ApiContext = api_context_dependency,
) -> AsyncIterator[ServerSentEvent]:
    async for event in stream_server_events(
        prepared,
        context.ports.run_event_queries,
        context.event_runner,
        page_size=context.limits.event_page_size,
        limits=context.limits,
        projection_limit=context.workflow_projection_limit,
        poll_backoff=context.event_poll_backoff,
    ):
        yield event
