from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from inspect import isasyncgenfunction, iscoroutinefunction
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts
from fastapi.testclient import TestClient
from openapi_spec_validator import OpenAPIV31SpecValidator, validate

from atelier2.api import openapi as openapi_module
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.openapi import (
    API_PREFIX,
    CANCELLATION_PATH,
    EVENT_NAMES,
    EVENT_PATH,
)
from atelier2.contracts.run_projections import PublicAgentAttemptState
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

FROZEN_DOCUMENT_PATH = Path(__file__).with_name("openapi_frozen.json")


def empty_ports() -> ApiPorts:
    return api_ports()


def served_app() -> FastAPI:
    return create_app(
        source_commit="commit",
        source_tree="tree",
        ports=empty_ports(),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
    )


def api_routes(app: FastAPI) -> Iterator[RouteContext]:
    """Every endpoint the application answers, however it was registered.

    This is the enumeration the schema generator itself walks, so it is blind
    to whether a route was declared on the application or on an included
    router. The application's own `openapi.json` route and any static mount
    are not endpoints and drop out here.
    """

    for route in iter_route_contexts(app.routes):
        if isinstance(route.original_route, APIRoute):
            yield route


def rendered_document(document: dict[str, Any]) -> str:
    """The published document as the frozen artefact stores it.

    Byte equality is the point: it pins the order of the ``paths`` keys, which
    follows registration order, on top of every value the document carries.
    """

    return json.dumps(document, separators=(",", ":"))


NODE_DETAIL_PATH = API_PREFIX + "/runs/{public_ref}/nodes/{node_id}"
RECEIPT_PATH = API_PREFIX + "/runs/{public_ref}/receipt"

EXPECTED_PATHS = {
    API_PREFIX + "/health",
    API_PREFIX + "/artifacts",
    API_PREFIX + "/auth-profile-revisions",
    API_PREFIX + "/agent-configuration-revisions",
    API_PREFIX + "/schema-revisions",
    API_PREFIX + "/budget-revisions",
    API_PREFIX + "/tool-grant-revisions",
    API_PREFIX + "/adapter-operation-revisions",
    API_PREFIX + "/workflow-revisions",
    API_PREFIX + "/workflow-revisions/by-name/{name}",
    API_PREFIX + "/workflow-revisions/{workflow_revision_hash}",
    API_PREFIX + "/workflow-lineages",
    API_PREFIX + "/workflow-lineages/{lineage_id}/members",
    API_PREFIX + "/runs",
    API_PREFIX + "/runs/{public_ref}",
    NODE_DETAIL_PATH,
    RECEIPT_PATH,
    API_PREFIX + "/runs/{public_ref}/answers",
    API_PREFIX + "/runs/{public_ref}/reconciliations",
    CANCELLATION_PATH,
    EVENT_PATH,
}

EXPECTED_ROUTE_SEQUENCE = (
    ("GET", API_PREFIX + "/health", "health"),
    (
        "POST",
        API_PREFIX + "/auth-profile-revisions",
        "publish_auth_profile_revision_route",
    ),
    (
        "GET",
        API_PREFIX + "/auth-profile-revisions",
        "list_auth_profile_revisions_route",
    ),
    (
        "POST",
        API_PREFIX + "/agent-configuration-revisions",
        "publish_agent_configuration_revision_route",
    ),
    (
        "GET",
        API_PREFIX + "/agent-configuration-revisions",
        "list_agent_configuration_revisions_route",
    ),
    (
        "POST",
        API_PREFIX + "/artifacts",
        "publish_artifact_route",
    ),
    (
        "POST",
        API_PREFIX + "/schema-revisions",
        "publish_schema_revision_route",
    ),
    (
        "POST",
        API_PREFIX + "/budget-revisions",
        "publish_budget_revision_route",
    ),
    (
        "POST",
        API_PREFIX + "/tool-grant-revisions",
        "publish_tool_grant_revision_route",
    ),
    (
        "POST",
        API_PREFIX + "/adapter-operation-revisions",
        "publish_adapter_operation_revision_route",
    ),
    ("POST", API_PREFIX + "/workflow-revisions", "publish_revision"),
    ("GET", API_PREFIX + "/workflow-revisions", "list_revisions"),
    (
        "POST",
        API_PREFIX + "/workflow-lineages",
        "found_catalog_lineage_route",
    ),
    (
        "POST",
        API_PREFIX + "/workflow-lineages/{lineage_id}/members",
        "admit_catalog_member_route",
    ),
    (
        "GET",
        API_PREFIX + "/workflow-revisions/by-name/{name}",
        "get_revision_by_name",
    ),
    (
        "GET",
        API_PREFIX + "/workflow-revisions/{workflow_revision_hash}",
        "get_revision",
    ),
    ("POST", API_PREFIX + "/runs", "start_run_route"),
    ("GET", API_PREFIX + "/runs", "list_runs"),
    ("GET", API_PREFIX + "/runs/{public_ref}", "get_run_route"),
    ("GET", NODE_DETAIL_PATH, "get_node_detail_route"),
    ("GET", RECEIPT_PATH, "get_run_receipt_route"),
    ("POST", CANCELLATION_PATH, "cancel_agent_attempt_route"),
    ("POST", API_PREFIX + "/runs/{public_ref}/answers", "answer_run_route"),
    (
        "POST",
        API_PREFIX + "/runs/{public_ref}/reconciliations",
        "reconcile_run_route",
    ),
    ("GET", EVENT_PATH, "event_stream_route"),
)

EXPECTED_SUCCESS_STATUSES = {
    (API_PREFIX + "/health", "get"): {"200"},
    (API_PREFIX + "/auth-profile-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/auth-profile-revisions", "get"): {"200"},
    (API_PREFIX + "/agent-configuration-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/agent-configuration-revisions", "get"): {"200"},
    (API_PREFIX + "/artifacts", "post"): {"200", "201"},
    (API_PREFIX + "/schema-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/tool-grant-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/adapter-operation-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/workflow-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/workflow-revisions", "get"): {"200"},
    (API_PREFIX + "/workflow-revisions/{workflow_revision_hash}", "get"): {"200"},
    (API_PREFIX + "/runs", "post"): {"200", "201"},
    (API_PREFIX + "/runs", "get"): {"200"},
    (API_PREFIX + "/runs/{public_ref}", "get"): {"200"},
    (RECEIPT_PATH, "get"): {"200"},
    (API_PREFIX + "/runs/{public_ref}/answers", "post"): {"200", "202"},
    (API_PREFIX + "/runs/{public_ref}/reconciliations", "post"): {"200", "202"},
    (CANCELLATION_PATH, "post"): {"200", "202"},
    (EVENT_PATH, "get"): {"200"},
}


def test_schema_routes_keep_their_endpoint_names_in_registration_order() -> None:
    """The document derives ``operationId`` and ``summary`` from these names.

    Renaming an endpoint function or registering it elsewhere in the sequence
    rewrites the published document, so both are pinned here where the failure
    reads as the one line that moved. The routes are walked with the same
    enumeration the schema generator uses, so the inventory is blind to
    whether a route was registered on the application or on a router.
    """

    registered = tuple(
        (method, route.path, route.name)
        for route in api_routes(served_app())
        if route.include_in_schema
        for method in sorted(route.methods or ())
    )

    assert registered == EXPECTED_ROUTE_SEQUENCE


def test_no_endpoint_or_dependency_sends_the_request_path_through_a_thread() -> None:
    """The request path stays on the event loop.

    FastAPI runs any non-coroutine endpoint or dependency through
    `run_in_threadpool`, which costs a worker-thread hop and a slot of the
    process-wide thread limiter on every request. The only blocking work this
    API does is a durable query, and that already goes through
    `BoundedQueryRunner` under its own bound.
    """

    def nested_dependencies(dependant: Dependant) -> Iterator[Dependant]:
        yield dependant
        for nested in dependant.dependencies:
            yield from nested_dependencies(nested)

    def runs_on_the_event_loop(called: Callable[..., Any]) -> bool:
        return iscoroutinefunction(called) or isasyncgenfunction(called)

    threaded = sorted(
        dependency.call.__name__
        for route in api_routes(served_app())
        for dependency in nested_dependencies(route.dependant)
        if dependency.call is not None and not runs_on_the_event_loop(dependency.call)
    )

    assert threaded == []


def test_served_document_is_byte_identical_to_the_frozen_artefact() -> None:
    """The published document is frozen; nothing below it may rewrite a byte.

    The artefact carries the declared wire changes of the heads that regenerated
    it. This head names the workflow-revision path parameter
    `workflow_revision_hash` and answers a declared order with the author's
    `schema: {ref, revision}` hull. Refreshing the artefact alongside a refactor
    is what this test still refuses.
    """

    assert rendered_document(served_app().openapi()) == FROZEN_DOCUMENT_PATH.read_text()


def test_openapi_31_validates_and_describes_exact_r2_surface() -> None:
    client = TestClient(served_app())

    response = client.get(API_PREFIX + "/openapi.json")
    schema = response.json()

    validate(schema, cls=OpenAPIV31SpecValidator)
    assert schema["openapi"] == "3.1.0"
    assert set(schema["paths"]) == EXPECTED_PATHS
    encoded = json.dumps(schema)
    assert "itemSchema" not in encoded
    assert "contentSchema" not in encoded
    assert "/docs" not in schema["paths"]
    assert "/redoc" not in schema["paths"]


def test_openapi_sse_extension_names_exact_wire_fields_and_closed_events() -> None:
    schema = served_app().openapi()

    content = schema["paths"][EVENT_PATH]["get"]["responses"]["200"]["content"]
    assert set(content) == {"text/event-stream"}
    assert content["text/event-stream"]["schema"] == {"type": "string"}
    extension = content["text/event-stream"]["x-atelier2-sse-v1"]
    assert extension == {
        "durable_event": {
            "id": {"$ref": "#/components/schemas/EventCursor"},
            "data": {"$ref": "#/components/schemas/VersionedRunEventResource"},
        },
        "terminal_failure": {
            "data": {"$ref": "#/components/schemas/StreamFailureResource"}
        },
    }
    failure_frame = schema["components"]["schemas"]["StreamFailureResource"]
    assert failure_frame["properties"]["event"]["const"] == "STREAM_FAILED"
    assert failure_frame["properties"]["problem"] == {
        "oneOf": [
            {"$ref": "#/components/schemas/ProblemDurableStateCorrupt"},
            {"$ref": "#/components/schemas/ProblemTemporarilyUnavailable"},
            {"$ref": "#/components/schemas/ProblemInternalError"},
        ]
    }
    assert "ProblemResource" not in schema["components"]["schemas"]
    event_union = schema["components"]["schemas"]["RunEventResource"]
    assert len(event_union["oneOf"]) == 7
    assert set(event_union["discriminator"]["mapping"]) == set(EVENT_NAMES)
    parameters = {
        (parameter["name"], parameter["in"]): parameter
        for parameter in schema["paths"][EVENT_PATH]["get"]["parameters"]
    }
    assert parameters[("Last-Event-ID", "header")]["schema"] == {
        "$ref": "#/components/schemas/EventCursor"
    }
    assert parameters[("public_ref", "path")]["schema"] == {
        "$ref": "#/components/schemas/PublicRunReference"
    }


def test_every_declared_error_response_is_problem_json_one_of() -> None:
    schema = served_app().openapi()

    for path in schema["paths"].values():
        for operation in path.values():
            for status, response in operation["responses"].items():
                if int(status) < 400:
                    continue
                assert set(response["content"]) == {"application/problem+json"}
                assert response["content"]["application/problem+json"]["schema"][
                    "oneOf"
                ]


def test_openapi_declares_every_success_and_exact_request_media_type() -> None:
    schema = served_app().openapi()

    for (path, method), expected_statuses in EXPECTED_SUCCESS_STATUSES.items():
        responses = schema["paths"][path][method]["responses"]
        assert {
            status for status in responses if int(status) < 400
        } == expected_statuses

    publication_body = schema["paths"][API_PREFIX + "/workflow-revisions"]["post"][
        "requestBody"
    ]
    assert publication_body == {
        "required": True,
        "content": {
            "application/yaml": {
                "schema": {"$ref": "#/components/schemas/WorkflowDocument"}
            }
        },
    }
    schema_publication_body = schema["paths"][API_PREFIX + "/schema-revisions"]["post"][
        "requestBody"
    ]
    assert schema_publication_body == {
        "required": True,
        "content": {
            "application/json": {"schema": {"type": "string", "format": "binary"}}
        },
    }
    grant_publication_body = schema["paths"][API_PREFIX + "/tool-grant-revisions"][
        "post"
    ]["requestBody"]
    assert grant_publication_body == schema_publication_body
    operation_publication_body = schema["paths"][
        API_PREFIX + "/adapter-operation-revisions"
    ]["post"]["requestBody"]
    assert operation_publication_body == schema_publication_body

    for path in (
        API_PREFIX + "/auth-profile-revisions",
        API_PREFIX + "/agent-configuration-revisions",
        API_PREFIX + "/runs",
        API_PREFIX + "/runs/{public_ref}/answers",
        API_PREFIX + "/runs/{public_ref}/reconciliations",
        CANCELLATION_PATH,
    ):
        assert set(schema["paths"][path]["post"]["requestBody"]["content"]) == {
            "application/json"
        }

    for path in (API_PREFIX + "/workflow-revisions", API_PREFIX + "/runs"):
        parameters = {
            (parameter["name"], parameter["in"]): parameter
            for parameter in schema["paths"][path]["get"]["parameters"]
        }
        assert parameters[("limit", "query")]["schema"] == {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": 50,
        }


def _referenced_schema(schema: dict[str, Any], content: dict[str, Any]) -> Any:
    reference = content["application/json"]["schema"]["$ref"]
    return schema["components"]["schemas"][reference.rsplit("/", 1)[1]]


def test_openapi_offers_the_capability_and_demands_it_back() -> None:
    """A caller may leave the capability out; a reader may not.

    The generated document is what a client is built against, so the request
    default and the mandatory echo are the contract, not an implementation
    detail of the models behind it.
    """

    schema = served_app().openapi()

    operation = schema["paths"][API_PREFIX + "/agent-configuration-revisions"]["post"]
    request = _referenced_schema(schema, operation["requestBody"]["content"])
    capability = {
        "type": "string",
        "enum": ["headless", "headless_with_tools", "interactive"],
        "title": "Requested Capability",
    }

    assert request["properties"]["requested_capability"] == capability | {
        "default": "headless"
    }
    assert "requested_capability" not in request["required"]
    assert request["additionalProperties"] is False
    for status, response in operation["responses"].items():
        if int(status) >= 400:
            continue
        echoed = _referenced_schema(schema, response["content"])
        assert echoed["properties"]["requested_capability"] == capability
        assert "requested_capability" in echoed["required"]
        assert echoed["additionalProperties"] is False


def test_invalid_openapi_fails_during_app_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class InvalidGeneratedSchema(ValueError):
        pass

    def reject_schema(_model: type[object], _schema: object) -> None:
        raise InvalidGeneratedSchema

    monkeypatch.setattr(
        openapi_module.OpenAPI,
        "model_validate",
        classmethod(reject_schema),
    )

    with pytest.raises(InvalidGeneratedSchema):
        served_app()


def test_first_request_reuses_schema_built_during_app_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    generated = 0
    original_get_openapi = openapi_module.get_openapi

    def counted_get_openapi(*args: object, **kwargs: object):
        nonlocal generated
        generated += 1
        return original_get_openapi(*args, **kwargs)

    monkeypatch.setattr(openapi_module, "get_openapi", counted_get_openapi)

    app = served_app()
    assert generated == 1

    response = TestClient(app).get(API_PREFIX + "/health")

    assert response.status_code == 200
    assert generated == 1


def test_served_agent_attempt_state_is_exactly_the_public_vocabulary() -> None:
    schema = served_app().openapi()
    served = schema["components"]["schemas"]["AgentAttemptResourceV2"]["properties"]

    assert served["state"] == {
        "enum": [state.value for state in PublicAgentAttemptState],
        "title": "State",
        "type": "string",
    }
