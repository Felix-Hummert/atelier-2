from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from atelier2.api.references import (
    MAXIMUM_INVALID_FIELD_PATH_CHARACTERS,
    MAXIMUM_INVALID_FIELD_REASON_CHARACTERS,
)
from atelier2.api.wire.resources import InvalidFieldResource, ProblemResource
from atelier2.contracts.schemas_v3 import SchemaDocumentRefusal

PROJECTION_LIMIT_DETAIL = "Durable projection exceeds configured API limits."
"""How the API words a stored projection that does not fit its configured bound.

The bound is the API's, so the sentence is too. It lived in `ports/` and made a
durable port speak of "API limits" — a layer explaining a decision it does not
make. The port now answers `ProjectionTooLarge` and this is what that becomes on
the wire.
"""

PROBLEM_TYPE_PREFIX = "urn:atelier2:problem:v1:"
_LOG = logging.getLogger("atelier2")


def schema_document_problem_code(refusal: SchemaDocumentRefusal) -> str:
    """The problem code one schema-profile refusal becomes on the wire.

    The profile already names each fault. The route must not collapse those
    names into `invalid-request`: the caller reads the reason from the type.
    """

    return f"schema-{refusal.value}"


def _schema_document_problems() -> dict[str, ProblemDefinition]:
    return {
        schema_document_problem_code(refusal): ProblemDefinition(
            422,
            "Invalid schema document",
            f"The document is not a schema this product enforces ({refusal.value}).",
        )
        for refusal in SchemaDocumentRefusal
    }


SCHEMA_DOCUMENT_PROBLEM_CODES = tuple(
    schema_document_problem_code(refusal) for refusal in SchemaDocumentRefusal
)


@dataclass(frozen=True)
class ProblemDefinition:
    status: int
    title: str
    detail: str


class ApiProblem(Exception):
    def __init__(
        self,
        code: str,
        detail: str | None = None,
        invalid_fields: tuple[InvalidFieldResource, ...] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.invalid_fields = invalid_fields


PROBLEM_DEFINITIONS: dict[str, ProblemDefinition] = {
    "auth-profile-revision-conflict": ProblemDefinition(
        409,
        "Auth profile revision conflict",
        "Use a new revision_number or retry the exact original auth profile revision.",
    ),
    "auth-profile-revision-collision": ProblemDefinition(
        409,
        "Auth profile revision collision",
        "Stop mutation and inspect durable auth profile revision integrity.",
    ),
    "auth-profile-revision-not-found": ProblemDefinition(
        404,
        "Auth profile revision not found",
        "Publish the exact auth profile revision before publishing an agent configuration.",
    ),
    "agent-executor-binding-unavailable": ProblemDefinition(
        409,
        "Agent executor binding unavailable",
        "Register the exact provider and executor revision before publishing or starting this configuration.",
    ),
    "agent-configuration-revision-collision": ProblemDefinition(
        409,
        "Agent configuration revision collision",
        "Stop mutation and inspect durable agent configuration revision integrity.",
    ),
    "agent-configuration-revision-not-found": ProblemDefinition(
        404,
        "Agent configuration revision not found",
        "Publish every exact agent configuration revision before starting the run.",
    ),
    "invalid-agent-bindings": ProblemDefinition(
        422,
        "Invalid agent bindings",
        "Bind every workflow agent role exactly once and no other role.",
    ),
    "invalid-agent-attempt-id": ProblemDefinition(
        400,
        "Invalid agent attempt id",
        "Use exactly 64 lowercase hexadecimal characters.",
    ),
    "agent-attempt-not-found": ProblemDefinition(
        404,
        "Agent attempt not found",
        "Cancel an attempt that belongs to the referenced run.",
    ),
    "agent-attempt-not-current": ProblemDefinition(
        409,
        "Agent attempt is not current",
        "Reload the run and cancel only its current attempt.",
    ),
    "agent-attempt-cancellation-stale": ProblemDefinition(
        409,
        "Agent attempt cancellation is stale",
        "Reload the run and bind the command to its current attempt state version.",
    ),
    "agent-attempt-terminal": ProblemDefinition(
        409,
        "Agent attempt is terminal",
        "A completed attempt can no longer be cancelled.",
    ),
    "cancellation-command-conflict": ProblemDefinition(
        409,
        "Cancellation command conflict",
        "Use a new command_id or retry the exact original cancellation command.",
    ),
    "replacement-not-allowed": ProblemDefinition(
        409,
        "Replacement is not allowed",
        "Only ordinal one may request the single replacement attempt.",
    ),
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
    **_schema_document_problems(),
    "schema-revision-collision": ProblemDefinition(
        409,
        "Schema revision collision",
        "Stop and inspect durable schema revision integrity.",
    ),
    "unsupported-media-type": ProblemDefinition(
        415,
        "Unsupported media type",
        "Use the media type documented for this operation.",
    ),
    "not-acceptable": ProblemDefinition(
        406, "Not acceptable", "Accept text/event-stream or */*."
    ),
    "catalog-revision-unpublished": ProblemDefinition(
        409,
        "Catalog revision is unpublished",
        "Publish the revision through POST /atelier/api/v1/workflow-revisions before giving it a name.",
    ),
    "catalog-name-held": ProblemDefinition(
        409,
        "Catalog name is held",
        "Another lineage of this kind already holds that name.",
    ),
    "catalog-revision-owned": ProblemDefinition(
        409,
        "Catalog revision is owned",
        "That revision already belongs to another lineage.",
    ),
    "catalog-lineage-missing": ProblemDefinition(
        404,
        "Catalog lineage not found",
        "No lineage of this kind carries that id.",
    ),
    "catalog-name-not-found": ProblemDefinition(
        404,
        "Catalog name not found",
        "No lineage of this kind holds that name at that position.",
    ),
    "catalog-lineage-retired": ProblemDefinition(
        410,
        "Catalog lineage retired",
        "This name was retired; it resolves to no revision a run may use.",
    ),
    "catalog-revision-not-a-member": ProblemDefinition(
        409,
        "Catalog revision is not a member",
        "The name resolved to a revision its lineage does not admit.",
    ),
    "invalid-catalog-position": ProblemDefinition(
        400,
        "Invalid catalog position",
        "Ask for head or an exact positive member number.",
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
    "workflow-format-not-executable": ProblemDefinition(
        409,
        "Workflow format is not executable",
        "This revision is published and no runtime here runs its format version yet.",
    ),
    "run-input-refused": ProblemDefinition(
        422,
        "Run input refused",
        "Supply exactly the orders this workflow declares, each satisfying the "
        "schema its author pinned.",
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


def problem_resource(
    code: str,
    detail: str | None = None,
    invalid_fields: tuple[InvalidFieldResource, ...] | None = None,
) -> ProblemResource:
    definition = PROBLEM_DEFINITIONS[code]
    return ProblemResource(
        type=PROBLEM_TYPE_PREFIX + code,
        title=definition.title,
        status=definition.status,
        detail=definition.detail if detail is None else detail,
        invalid_fields=invalid_fields,
    )


def problem_response(
    code: str,
    detail: str | None = None,
    invalid_fields: tuple[InvalidFieldResource, ...] | None = None,
) -> JSONResponse:
    resource = problem_resource(code, detail, invalid_fields)
    return JSONResponse(
        resource.model_dump(mode="json", exclude_none=True),
        status_code=resource.status,
        media_type="application/problem+json",
    )


def _clipped(text: str, bound: int) -> str:
    return text if len(text) <= bound else text[:bound]


def invalid_fields_from_validation(
    error: RequestValidationError,
) -> tuple[InvalidFieldResource, ...]:
    """The loc and message the framework already named, as wire pointers."""
    fields: list[InvalidFieldResource] = []
    for item in error.errors():
        loc = item.get("loc", ())
        path = "/".join(str(part) for part in loc) or "request"
        reason = str(item.get("msg") or item.get("type") or "invalid")
        fields.append(
            InvalidFieldResource(
                path=_clipped(path, MAXIMUM_INVALID_FIELD_PATH_CHARACTERS),
                reason=_clipped(reason, MAXIMUM_INVALID_FIELD_REASON_CHARACTERS),
            )
        )
    return tuple(fields)


def install_problem_handlers(app: FastAPI, *, versioned_run_start_path: str) -> None:
    @app.exception_handler(ApiProblem)
    async def typed_problem(_request: Request, error: ApiProblem) -> JSONResponse:
        return problem_response(error.code, error.detail, error.invalid_fields)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        if (
            request.method == "POST"
            and request.url.path == versioned_run_start_path
            and isinstance(error.body, dict)
            and {"workflow_format_version", "agent_bindings"}.intersection(error.body)
        ):
            return problem_response("invalid-agent-bindings")
        fields = invalid_fields_from_validation(error)
        return problem_response(
            "invalid-request",
            invalid_fields=fields or None,
        )

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        if error.status_code == HTTPStatus.NOT_FOUND:
            return problem_response("route-not-found")
        if error.status_code == HTTPStatus.METHOD_NOT_ALLOWED:
            return problem_response("method-not-allowed")
        return problem_response("internal-error")

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, error: Exception) -> JSONResponse:
        detail = str(error).strip()
        sentence = (
            f"The HTTP request failed with an unhandled {type(error).__name__}: "
            f"{detail}."
            if detail
            else f"The HTTP request failed with an unhandled {type(error).__name__}."
        )
        _LOG.error(
            sentence,
            extra={
                "event": "http_internal_error",
                "exception": "".join(traceback.format_exception(error)),
            },
        )
        return problem_response("internal-error")
