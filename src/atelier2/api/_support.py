from __future__ import annotations

import re
from collections.abc import Callable
from http import HTTPStatus
from typing import assert_never

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from atelier2.api.limits import ApiLimitExceeded, ApiLimits
from atelier2.api.problems import ApiProblem
from atelier2.api.projection.runs import run_resource
from atelier2.api.references import (
    MAX_SIGNED_INT64,
    InvalidPublicRunReference,
    decode_canonical_base64,
    decode_public_run_reference,
)
from atelier2.api.stream import BoundedQueryRunner, QueryAdmissionTimeout
from atelier2.api.wire.resources import AnyRunResource
from atelier2.contracts.runs import RunId
from atelier2.ports.run_queries import (
    RunFound,
    RunProjection,
    RunQueries,
    RunQueryMissing,
)
from atelier2.ports.workflow_revisions import (
    PROJECTION_LIMIT_DETAIL,
    DurableProjectionLimit,
    QueryDurableStateCorrupt,
    ReadUnavailable,
)


def resource_response(resource: BaseModel, status: HTTPStatus) -> JSONResponse:
    return JSONResponse(resource.model_dump(mode="json"), status_code=status)


async def load_run_resource(
    run_id: RunId,
    queries: RunQueries,
    runner: BoundedQueryRunner,
    limits: ApiLimits,
    projection_limit: DurableProjectionLimit,
) -> AnyRunResource:
    return run_resource(
        await load_run_projection(run_id, queries, runner, limits, projection_limit)
    )


async def load_run_projection(
    run_id: RunId,
    queries: RunQueries,
    runner: BoundedQueryRunner,
    limits: ApiLimits,
    projection_limit: DurableProjectionLimit,
) -> RunProjection:
    result = await run_control_query(
        runner, lambda: queries.get_run(run_id, projection_limit)
    )
    match result:
        case RunFound(projection):
            require_run_projections((projection,), limits)
            return projection
        case RunQueryMissing():
            raise ApiProblem("run-not-found")
        case ReadUnavailable(detail):
            raise ApiProblem("temporarily-unavailable", detail)
        case QueryDurableStateCorrupt():
            raise ApiProblem("durable-state-corrupt")
        case _ as unreachable:
            assert_never(unreachable)


def require_run_projections(
    projections: tuple[RunProjection, ...], limits: ApiLimits
) -> None:
    try:
        for projection in projections:
            limits.require_run_projection(projection)
    except ValueError as error:
        raise ApiProblem("temporarily-unavailable", PROJECTION_LIMIT_DETAIL) from error


async def run_control_query[Result](
    runner: BoundedQueryRunner, query: Callable[[], Result]
) -> Result:
    try:
        return await runner.run(query)
    except QueryAdmissionTimeout as error:
        raise ApiProblem("temporarily-unavailable") from error


def decode_public_reference(value: str, limits: ApiLimits) -> RunId:
    try:
        limits.require_field(value)
        run_id = decode_public_run_reference(value)
        limits.require_field(run_id.value)
        return run_id
    except ApiLimitExceeded as error:
        raise ApiProblem("invalid-public-run-reference") from error
    except InvalidPublicRunReference as error:
        raise ApiProblem("invalid-public-run-reference") from error


def decode_base64(value: str, limits: ApiLimits) -> bytes:
    try:
        limits.require_base64(value)
        decoded = decode_canonical_base64(value)
        limits.require_payload(decoded)
        return decoded
    except ApiLimitExceeded as error:
        raise ApiProblem("invalid-request", str(error)) from error
    except ValueError as error:
        raise ApiProblem("invalid-base64") from error


def require_field(value: str, limits: ApiLimits) -> None:
    try:
        limits.require_field(value)
    except ApiLimitExceeded as error:
        raise ApiProblem("invalid-request", str(error)) from error


def require_fields(limits: ApiLimits, *values: str) -> None:
    for value in values:
        require_field(value, limits)


def require_new_run_identity(run_id: RunId, limits: ApiLimits) -> None:
    try:
        limits.require_field(run_id.value)
        limits.require_public_run_reference(run_id)
        limits.require_event_cursor(run_id, MAX_SIGNED_INT64)
    except ValueError as error:
        raise ApiProblem("invalid-request") from error


def parse_limit(value: str) -> int:
    if re.fullmatch(r"(?:[1-9]|[1-9][0-9]|100)", value) is None:
        raise ApiProblem("invalid-request")
    return int(value)


def require_media_type(request: Request, expected: str) -> None:
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


async def require_json_media_dependency(request: Request) -> None:
    require_media_type(request, "application/json")


def require_sse_accept(request: Request) -> None:
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
