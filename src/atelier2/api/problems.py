from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from atelier2.api.models import ProblemResource

PROBLEM_TYPE_PREFIX = "urn:atelier2:problem:v1:"


@dataclass(frozen=True)
class ProblemDefinition:
    status: int
    title: str
    detail: str


class ApiProblem(Exception):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


PROBLEM_DEFINITIONS: dict[str, ProblemDefinition] = {
    "invalid-public-run-reference": ProblemDefinition(
        400, "Invalid public run reference", "Use a canonical run1 public reference."
    ),
    "invalid-event-cursor": ProblemDefinition(
        400,
        "Invalid event cursor",
        "Use a canonical event1 cursor returned by this API.",
    ),
    "invalid-revision-hash": ProblemDefinition(
        400, "Invalid revision hash", "Use exactly 64 lowercase hexadecimal characters."
    ),
    "event-cursor-run-mismatch": ProblemDefinition(
        409,
        "Event cursor belongs to another run",
        "Reconnect with a cursor returned for this run.",
    ),
    "event-cursor-ahead": ProblemDefinition(
        409,
        "Event cursor is ahead of durable history",
        "Reconnect from a cursor at or below the durable head.",
    ),
    "invalid-request": ProblemDefinition(
        422, "Invalid request", "Correct the request fields and submit it again."
    ),
    "invalid-base64": ProblemDefinition(
        422, "Invalid base64", "Use canonical RFC 4648 base64 with required padding."
    ),
    "invalid-workflow-document": ProblemDefinition(
        422,
        "Invalid workflow document",
        "Submit exact bytes for a safe closed workflow graph.",
    ),
    "unsupported-media-type": ProblemDefinition(
        415,
        "Unsupported media type",
        "Use the media type documented for this operation.",
    ),
    "not-acceptable": ProblemDefinition(
        406, "Not acceptable", "Accept text/event-stream or */*."
    ),
    "workflow-revision-not-found": ProblemDefinition(
        404,
        "Workflow revision not found",
        "Publish the exact workflow revision before starting a run.",
    ),
    "run-not-found": ProblemDefinition(
        404, "Run not found", "Use a public reference for a durable run that exists."
    ),
    "node-not-found": ProblemDefinition(
        404, "Node not found", "Answer a node in the referenced workflow graph."
    ),
    "revision-collision": ProblemDefinition(
        409,
        "Workflow revision collision",
        "Stop and inspect durable revision integrity.",
    ),
    "run-identity-conflict": ProblemDefinition(
        409,
        "Run identity conflict",
        "Use a new run_id or retry the exact original revision.",
    ),
    "answer-revision-conflict": ProblemDefinition(
        409, "Answer revision conflict", "Retry with the run's exact workflow revision."
    ),
    "answer-state-conflict": ProblemDefinition(
        409,
        "Answer state conflict",
        "Answer only the run's current waiting input node.",
    ),
    "answer-bytes-conflict": ProblemDefinition(
        409,
        "Answer bytes conflict",
        "Retry the exact original answer bytes or use another command.",
    ),
    "reconciliation-target-missing": ProblemDefinition(
        409,
        "Reconciliation target missing",
        "Reconcile only the run's current unresolved Action.",
    ),
    "reconciliation-stale": ProblemDefinition(
        409,
        "Reconciliation is stale",
        "Reload the run and bind a command to its current intent state version.",
    ),
    "reconciliation-command-conflict": ProblemDefinition(
        409,
        "Reconciliation command conflict",
        "Use a new command_id or retry the exact original command.",
    ),
    "reconciliation-determination-conflict": ProblemDefinition(
        409,
        "Reconciliation determination conflict",
        "Use a new command_id for a changed determination.",
    ),
    "reconciliation-rejected": ProblemDefinition(
        409,
        "Reconciliation was rejected",
        "Reload the run before issuing another accountable command.",
    ),
    "route-not-found": ProblemDefinition(
        404, "Route not found", "Use a path described by this API's OpenAPI document."
    ),
    "method-not-allowed": ProblemDefinition(
        405, "Method not allowed", "Use the HTTP method described for this path."
    ),
    "temporarily-unavailable": ProblemDefinition(
        503,
        "Temporarily unavailable",
        "Retry after the durable store becomes available.",
    ),
    "durable-state-corrupt": ProblemDefinition(
        500, "Durable state is corrupt", "Stop mutation and inspect the durable store."
    ),
    "internal-error": ProblemDefinition(
        500, "Internal error", "Retry only after the server fault has been inspected."
    ),
}


def problem_resource(code: str, detail: str | None = None) -> ProblemResource:
    definition = PROBLEM_DEFINITIONS[code]
    return ProblemResource(
        type=PROBLEM_TYPE_PREFIX + code,
        title=definition.title,
        status=definition.status,
        detail=definition.detail if detail is None else detail,
    )


def problem_response(code: str, detail: str | None = None) -> JSONResponse:
    resource = problem_resource(code, detail)
    return JSONResponse(
        resource.model_dump(mode="json"),
        status_code=resource.status,
        media_type="application/problem+json",
    )


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiProblem)
    async def typed_problem(_request: Request, error: ApiProblem) -> JSONResponse:
        return problem_response(error.code, error.detail)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        _request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return problem_response("invalid-request")

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        if error.status_code == HTTPStatus.NOT_FOUND:
            return problem_response("route-not-found")
        if error.status_code == HTTPStatus.METHOD_NOT_ALLOWED:
            return problem_response("method-not-allowed")
        return problem_response("internal-error")

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, _error: Exception) -> JSONResponse:
        return problem_response("internal-error")
