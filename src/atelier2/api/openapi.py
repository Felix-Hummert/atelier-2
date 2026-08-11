from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.models import OpenAPI
from fastapi.openapi.utils import get_openapi

from atelier2.api.models import (
    ActionCompletedEventResource,
    ActionReconciliationRequiredEventResource,
    ActionReconciliationResolvedEventResource,
    AgentCompletedEventResource,
    SubworkflowCompletedEventResource,
    WaitAnsweredEventResource,
    WaitingInputEventResource,
)
from atelier2.api.problems import PROBLEM_DEFINITIONS, PROBLEM_TYPE_PREFIX
from atelier2.api.references import (
    EVENT_CURSOR_PATTERN,
    PUBLIC_RUN_REFERENCE_PATTERN,
    REVISION_HASH_PATTERN,
)
from atelier2.contracts.executions import RunEventKind

API_PREFIX = "/atelier/api/v1"
EVENT_PATH = API_PREFIX + "/runs/{public_ref}/events"

EVENT_MODELS = (
    AgentCompletedEventResource,
    ActionReconciliationRequiredEventResource,
    ActionReconciliationResolvedEventResource,
    ActionCompletedEventResource,
    WaitingInputEventResource,
    WaitAnsweredEventResource,
    SubworkflowCompletedEventResource,
)
EVENT_NAMES = tuple(kind.value for kind in RunEventKind)

OPERATION_PROBLEMS: dict[tuple[str, str], tuple[str, ...]] = {
    (API_PREFIX + "/health", "get"): ("internal-error",),
    (API_PREFIX + "/workflow-revisions", "post"): (
        "invalid-workflow-document",
        "revision-collision",
        "unsupported-media-type",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/workflow-revisions", "get"): (
        "invalid-revision-hash",
        "invalid-request",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/workflow-revisions/{revision_hash}", "get"): (
        "invalid-revision-hash",
        "workflow-revision-not-found",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/runs", "post"): (
        "invalid-revision-hash",
        "invalid-request",
        "unsupported-media-type",
        "workflow-revision-not-found",
        "run-identity-conflict",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/runs", "get"): (
        "invalid-public-run-reference",
        "invalid-request",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/runs/{public_ref}", "get"): (
        "invalid-public-run-reference",
        "run-not-found",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/runs/{public_ref}/answers", "post"): (
        "invalid-public-run-reference",
        "invalid-revision-hash",
        "invalid-base64",
        "invalid-request",
        "unsupported-media-type",
        "run-not-found",
        "node-not-found",
        "answer-revision-conflict",
        "answer-state-conflict",
        "answer-bytes-conflict",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (API_PREFIX + "/runs/{public_ref}/reconciliations", "post"): (
        "invalid-public-run-reference",
        "invalid-base64",
        "invalid-request",
        "unsupported-media-type",
        "run-not-found",
        "reconciliation-target-missing",
        "reconciliation-stale",
        "reconciliation-command-conflict",
        "reconciliation-determination-conflict",
        "reconciliation-rejected",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
    (EVENT_PATH, "get"): (
        "invalid-public-run-reference",
        "invalid-event-cursor",
        "event-cursor-run-mismatch",
        "event-cursor-ahead",
        "not-acceptable",
        "run-not-found",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    ),
}


def install_custom_openapi(app: FastAPI) -> None:
    def custom_openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        generated = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version="3.1.0",
            routes=app.routes,
        )
        schema = deepcopy(generated)
        schema["openapi"] = "3.1.0"
        _remove_32_sse_fields(schema)
        _install_problem_components(schema)
        _install_problem_responses(schema)
        _install_publication_request_body(schema)
        _install_event_components(schema)
        _install_parameter_contracts(schema)
        _install_sse_contract(schema)
        OpenAPI.model_validate(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi
    custom_openapi()


def _install_publication_request_body(schema: dict[str, Any]) -> None:
    schema["paths"][API_PREFIX + "/workflow-revisions"]["post"]["requestBody"] = {
        "required": True,
        "content": {
            "application/yaml": {"schema": {"type": "string", "format": "binary"}}
        },
    }


def _remove_32_sse_fields(value: object) -> None:
    if isinstance(value, dict):
        value.pop("itemSchema", None)
        value.pop("contentSchema", None)
        for nested in value.values():
            _remove_32_sse_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _remove_32_sse_fields(nested)


def _problem_component_name(code: str) -> str:
    return "Problem" + "".join(part.title() for part in code.split("-"))


def _install_problem_components(schema: dict[str, Any]) -> None:
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for code, definition in PROBLEM_DEFINITIONS.items():
        components[_problem_component_name(code)] = {
            "type": "object",
            "additionalProperties": False,
            "required": ["type", "title", "status", "detail"],
            "properties": {
                "type": {"type": "string", "const": PROBLEM_TYPE_PREFIX + code},
                "title": {"type": "string", "const": definition.title},
                "status": {"type": "integer", "const": definition.status},
                "detail": {"type": "string"},
            },
        }


def _install_problem_responses(schema: dict[str, Any]) -> None:
    for (path, method), codes in OPERATION_PROBLEMS.items():
        operation = schema["paths"][path][method]
        operation["responses"].pop("422", None)
        grouped: defaultdict[int, list[str]] = defaultdict(list)
        for code in codes:
            grouped[PROBLEM_DEFINITIONS[code].status].append(code)
        for status, status_codes in grouped.items():
            operation["responses"][str(status)] = {
                "description": "RFC 9457 problem",
                "content": {
                    "application/problem+json": {
                        "schema": {
                            "oneOf": [
                                {
                                    "$ref": "#/components/schemas/"
                                    + _problem_component_name(code)
                                }
                                for code in status_codes
                            ]
                        }
                    }
                },
            }


def _install_event_components(schema: dict[str, Any]) -> None:
    components = schema.setdefault("components", {}).setdefault("schemas", {})
    for model in EVENT_MODELS:
        generated = model.model_json_schema(
            mode="serialization", ref_template="#/components/schemas/{model}"
        )
        definitions = generated.pop("$defs", {})
        components.update(definitions)
        components[model.__name__] = generated
    components["RunEventResource"] = {
        "oneOf": [
            {"$ref": f"#/components/schemas/{model.__name__}"} for model in EVENT_MODELS
        ],
        "discriminator": {
            "propertyName": "event",
            "mapping": {
                name: f"#/components/schemas/{model.__name__}"
                for name, model in zip(EVENT_NAMES, EVENT_MODELS, strict=True)
            },
        },
    }
    components["EventCursor"] = {
        "type": "string",
        "pattern": EVENT_CURSOR_PATTERN,
    }
    components["PublicRunReference"] = {
        "type": "string",
        "pattern": PUBLIC_RUN_REFERENCE_PATTERN,
    }
    components["RevisionHash"] = {
        "type": "string",
        "pattern": REVISION_HASH_PATTERN,
    }


def _install_parameter_contracts(schema: dict[str, Any]) -> None:
    references = {
        "PublicRunReference": (
            (API_PREFIX + "/runs", "get", "after", "query"),
            (API_PREFIX + "/runs/{public_ref}", "get", "public_ref", "path"),
            (
                API_PREFIX + "/runs/{public_ref}/answers",
                "post",
                "public_ref",
                "path",
            ),
            (
                API_PREFIX + "/runs/{public_ref}/reconciliations",
                "post",
                "public_ref",
                "path",
            ),
            (EVENT_PATH, "get", "public_ref", "path"),
        ),
        "RevisionHash": (
            (
                API_PREFIX + "/workflow-revisions",
                "get",
                "after_revision_hash",
                "query",
            ),
            (
                API_PREFIX + "/workflow-revisions/{revision_hash}",
                "get",
                "revision_hash",
                "path",
            ),
        ),
    }
    for component, parameters in references.items():
        for path, method, name, location in parameters:
            _replace_parameter_schema(
                schema["paths"][path][method],
                name,
                location,
                {"$ref": f"#/components/schemas/{component}"},
            )
    limit_schema = {
        "type": "integer",
        "minimum": 1,
        "maximum": 100,
        "default": 50,
    }
    for path in (API_PREFIX + "/workflow-revisions", API_PREFIX + "/runs"):
        _replace_parameter_schema(
            schema["paths"][path]["get"], "limit", "query", limit_schema
        )


def _replace_parameter_schema(
    operation: dict[str, Any],
    name: str,
    location: str,
    parameter_schema: dict[str, Any],
) -> None:
    for parameter in operation.get("parameters", []):
        if parameter.get("name") == name and parameter.get("in") == location:
            parameter["schema"] = parameter_schema
            return
    raise RuntimeError(f"generated OpenAPI omitted {location} parameter {name}")


def _install_sse_contract(schema: dict[str, Any]) -> None:
    operation = schema["paths"][EVENT_PATH]["get"]
    operation.setdefault("parameters", []).append(
        {
            "name": "Last-Event-ID",
            "in": "header",
            "required": False,
            "schema": {"$ref": "#/components/schemas/EventCursor"},
            "description": "Cursor of the last event fully processed by the client.",
        }
    )
    response = operation["responses"]["200"]
    response["content"] = {
        "text/event-stream": {
            "schema": {"type": "string"},
            "x-atelier2-sse-v1": {
                "id": {"$ref": "#/components/schemas/EventCursor"},
                "event": {"enum": list(EVENT_NAMES)},
                "data": {"$ref": "#/components/schemas/RunEventResource"},
            },
        }
    }
