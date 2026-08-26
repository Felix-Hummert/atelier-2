from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from inspect import isasyncgenfunction, iscoroutinefunction
from pathlib import Path
from typing import Any, get_args

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
    ATTENTION_EVENT_PATH,
    CANCELLATION_PATH,
    EVENT_NAMES,
    EVENT_PATH,
    LIBRARY_RECOGNITIONS_PATH,
    OCCUPANCY_PATH,
    PROJECT_PATH,
    PROJECT_ROOT_PATH,
    PROJECTS_PATH,
    RUN_CANCELLATION_PATH,
)
from atelier2.api.references import (
    CATALOG_LINEAGE_ID_PATTERN,
    MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    MAXIMUM_RUN_ORDERS,
    PUBLIC_PROJECT_REFERENCE_PATTERN,
)
from atelier2.api.wire import events as wire_events
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
    API_PREFIX + "/schema-revisions/{schema_revision_hash}",
    API_PREFIX + "/budget-revisions",
    API_PREFIX + "/tool-grant-revisions",
    API_PREFIX + "/adapter-operation-revisions",
    API_PREFIX + "/agent-definition-revisions",
    API_PREFIX + "/agent-definition-revisions/{agent_definition_revision_hash}",
    LIBRARY_RECOGNITIONS_PATH,
    API_PREFIX + "/workflow-revisions",
    API_PREFIX + "/workflow-revisions/by-name/{name}",
    API_PREFIX + "/workflow-revisions/{workflow_revision_hash}",
    API_PREFIX + "/workflow-lineages",
    API_PREFIX + "/workflow-lineages/{lineage_id}/members",
    PROJECTS_PATH,
    PROJECT_PATH,
    OCCUPANCY_PATH,
    PROJECT_ROOT_PATH,
    API_PREFIX + "/runs",
    API_PREFIX + "/runs/{public_ref}",
    NODE_DETAIL_PATH,
    RECEIPT_PATH,
    API_PREFIX + "/runs/{public_ref}/answers",
    API_PREFIX + "/runs/{public_ref}/reconciliations",
    CANCELLATION_PATH,
    RUN_CANCELLATION_PATH,
    EVENT_PATH,
    ATTENTION_EVENT_PATH,
    API_PREFIX + "/queue-admissions",
    API_PREFIX + "/queue-items",
    API_PREFIX + "/project-sources/import",
    API_PREFIX + "/observed-queue-items",
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
        "GET",
        API_PREFIX + "/schema-revisions/{schema_revision_hash}",
        "get_schema_revision_route",
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
    (
        "POST",
        API_PREFIX + "/agent-definition-revisions",
        "publish_agent_definition_revision_route",
    ),
    (
        "GET",
        API_PREFIX + "/agent-definition-revisions",
        "list_agent_definition_revisions_route",
    ),
    (
        "GET",
        API_PREFIX + "/agent-definition-revisions/{agent_definition_revision_hash}",
        "get_agent_definition_revision_route",
    ),
    ("POST", LIBRARY_RECOGNITIONS_PATH, "recognize_library_document_route"),
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
    ("GET", PROJECTS_PATH, "list_projects_route"),
    ("GET", PROJECT_PATH, "get_project_route"),
    ("PUT", OCCUPANCY_PATH, "put_occupancy_revision_route"),
    ("GET", OCCUPANCY_PATH, "get_occupancy_revision_route"),
    ("PUT", PROJECT_ROOT_PATH, "put_project_root_revision_route"),
    ("GET", PROJECT_ROOT_PATH, "get_project_root_revision_route"),
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
    ("POST", RUN_CANCELLATION_PATH, "cancel_run_route"),
    ("GET", EVENT_PATH, "event_stream_route"),
    ("GET", ATTENTION_EVENT_PATH, "attention_event_stream_route"),
    ("POST", API_PREFIX + "/queue-admissions", "admit_queue_item_route"),
    ("GET", API_PREFIX + "/queue-items", "list_queue_items_route"),
    (
        "POST",
        API_PREFIX + "/project-sources/import",
        "import_project_source_issues_route",
    ),
    (
        "GET",
        API_PREFIX + "/observed-queue-items",
        "list_observed_queue_items_route",
    ),
)

EXPECTED_SUCCESS_STATUSES = {
    (API_PREFIX + "/health", "get"): {"200"},
    (API_PREFIX + "/auth-profile-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/auth-profile-revisions", "get"): {"200"},
    (API_PREFIX + "/agent-configuration-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/agent-configuration-revisions", "get"): {"200"},
    (API_PREFIX + "/artifacts", "post"): {"200", "201"},
    (API_PREFIX + "/schema-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/schema-revisions/{schema_revision_hash}", "get"): {"200"},
    (API_PREFIX + "/tool-grant-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/adapter-operation-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/agent-definition-revisions", "post"): {"200", "201"},
    (
        API_PREFIX + "/agent-definition-revisions/{agent_definition_revision_hash}",
        "get",
    ): {"200"},
    (LIBRARY_RECOGNITIONS_PATH, "post"): {"200"},
    (API_PREFIX + "/workflow-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/workflow-revisions", "get"): {"200"},
    (API_PREFIX + "/workflow-revisions/{workflow_revision_hash}", "get"): {"200"},
    (PROJECTS_PATH, "get"): {"200"},
    (PROJECT_PATH, "get"): {"200"},
    (OCCUPANCY_PATH, "put"): {"200", "201"},
    (OCCUPANCY_PATH, "get"): {"200"},
    (PROJECT_ROOT_PATH, "put"): {"200", "201"},
    (PROJECT_ROOT_PATH, "get"): {"200"},
    (API_PREFIX + "/runs", "post"): {"200", "201"},
    (API_PREFIX + "/runs", "get"): {"200"},
    (API_PREFIX + "/runs/{public_ref}", "get"): {"200"},
    (RECEIPT_PATH, "get"): {"200"},
    (API_PREFIX + "/runs/{public_ref}/answers", "post"): {"200", "202"},
    (API_PREFIX + "/runs/{public_ref}/reconciliations", "post"): {"200", "202"},
    (CANCELLATION_PATH, "post"): {"200", "202"},
    (RUN_CANCELLATION_PATH, "post"): {"200", "202"},
    (EVENT_PATH, "get"): {"200"},
    (ATTENTION_EVENT_PATH, "get"): {"200"},
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
    it. This head adds `WorkItemOrderResource` to the orders a V3 start accepts
    and publishes the three project-source problems on that operation: a caller
    names an item in the connected project's tracker, the start reads it, and
    the ways that read can fail are answers this door can now give (#712).
    Refreshing the artefact alongside a refactor is what this test still refuses.
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
    for private_runner_field in (
        "runner_manifest_id",
        "runner_generation_id",
        "runner_invocation_id",
        "runner_terminal_evidence_hash",
        "runner_evidence_acceptance_phase",
    ):
        assert private_runner_field not in encoded
    assert "/docs" not in schema["paths"]
    assert "/redoc" not in schema["paths"]


def test_openapi_sse_extension_names_exact_wire_fields_and_closed_events() -> None:
    schema = served_app().openapi()

    envelope = {
        "durable_event": {
            "id": {"$ref": "#/components/schemas/EventCursor"},
            "data": {"$ref": "#/components/schemas/VersionedRunEventResource"},
        },
        "terminal_failure": {
            "data": {"$ref": "#/components/schemas/StreamFailureResource"}
        },
    }
    for path in (EVENT_PATH, ATTENTION_EVENT_PATH):
        content = schema["paths"][path]["get"]["responses"]["200"]["content"]
        assert set(content) == {"text/event-stream"}
        assert content["text/event-stream"]["schema"] == {"type": "string"}
        assert content["text/event-stream"]["x-atelier2-sse-v1"] == envelope
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
    attention_parameters = {
        (parameter["name"], parameter["in"]): parameter
        for parameter in schema["paths"][ATTENTION_EVENT_PATH]["get"]["parameters"]
    }
    assert attention_parameters[("Last-Event-ID", "header")]["schema"] == {
        "$ref": "#/components/schemas/EventCursor"
    }


def test_openapi_v3_event_union_names_every_wire_v3_event_resource() -> None:
    """`RunEventResourceV3` may not silently drop a resource the wire emits.

    `atelier2.api.wire.events.RunEventResourceV3` is the wire's own event
    union -- the type every format-3 durable event is actually written and
    read as. This derives the expected published resources from that union,
    never from a hand list, so a kind added there without a matching entry in
    the published `RunEventResourceV3` schema fails here instead of shipping
    unseen.
    """

    wire_resource_names = {
        model.__name__ for model in get_args(wire_events.RunEventResourceV3)
    }
    schema = served_app().openapi()
    published_resource_names = {
        ref["$ref"].rsplit("/", 1)[-1]
        for ref in schema["components"]["schemas"]["RunEventResourceV3"]["oneOf"]
    }
    assert published_resource_names == wire_resource_names


def test_occupancy_path_parameters_use_owned_project_and_lineage_components() -> None:
    schema = served_app().openapi()
    project_component = schema["components"]["schemas"]["PublicProjectReference"]
    lineage_component = schema["components"]["schemas"]["CatalogLineageId"]

    assert project_component == {
        "type": "string",
        "pattern": PUBLIC_PROJECT_REFERENCE_PATTERN,
        "maxLength": MAXIMUM_PUBLIC_PROJECT_REFERENCE_CHARACTERS,
    }
    assert lineage_component == {
        "type": "string",
        "pattern": CATALOG_LINEAGE_ID_PATTERN,
    }
    for method in ("get", "put"):
        parameters = {
            (parameter["name"], parameter["in"]): parameter
            for parameter in schema["paths"][OCCUPANCY_PATH][method]["parameters"]
        }
        assert parameters[("public_project_reference", "path")]["schema"] == {
            "$ref": "#/components/schemas/PublicProjectReference"
        }
        assert parameters[("lineage_id", "path")]["schema"] == {
            "$ref": "#/components/schemas/CatalogLineageId"
        }
        assert parameters[("lineage_id", "path")]["schema"] != {
            "$ref": "#/components/schemas/RevisionHash"
        }


def test_project_paths_publish_one_opaque_resource_without_pagination() -> None:
    schema = served_app().openapi()
    project = schema["components"]["schemas"]["ProjectResource"]
    collection = schema["components"]["schemas"]["ProjectListResource"]
    detail_parameters = {
        (parameter["name"], parameter["in"]): parameter
        for parameter in schema["paths"][PROJECT_PATH]["get"]["parameters"]
    }

    assert project["required"] == ["public_project_reference"]
    assert set(project["properties"]) == {"public_project_reference"}
    assert project["properties"]["public_project_reference"] == {
        "$ref": "#/components/schemas/PublicProjectReference"
    }
    assert collection["properties"]["items"]["maxItems"] == 1
    assert collection["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/ProjectResource"
    }
    assert set(schema["paths"][PROJECTS_PATH]["get"].get("parameters", ())) == set()
    assert detail_parameters[("public_project_reference", "path")]["schema"] == {
        "$ref": "#/components/schemas/PublicProjectReference"
    }
    assert set(openapi_module.OPERATION_PROBLEMS[(PROJECTS_PATH, "get")]) == {
        "project-unknown",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    }
    assert set(openapi_module.OPERATION_PROBLEMS[(PROJECT_PATH, "get")]) == {
        "invalid-public-project-reference",
        "project-unknown",
        "temporarily-unavailable",
        "durable-state-corrupt",
        "internal-error",
    }


def test_the_start_door_publishes_the_tracker_problems_a_work_item_order_earns() -> (
    None
):
    """A start whose order names a work item reads the project's own tracker.

    A caller writing against the document has to see the three answers that
    read can give, or it learns them from a 503 nobody described.
    """

    schema = served_app().openapi()
    responses = schema["paths"][API_PREFIX + "/runs"]["post"]["responses"]

    assert {
        "project-source-not-connected",
        "project-source-unavailable",
        "project-source-payload-malformed",
    } <= set(openapi_module.OPERATION_PROBLEMS[(API_PREFIX + "/runs", "post")])
    for status, problem in (
        ("409", "ProblemProjectSourceNotConnected"),
        ("503", "ProblemProjectSourceUnavailable"),
        ("502", "ProblemProjectSourcePayloadMalformed"),
    ):
        assert {"$ref": f"#/components/schemas/{problem}"} in responses[status][
            "content"
        ]["application/problem+json"]["schema"]["oneOf"]


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
    definition_publication_body = schema["paths"][
        API_PREFIX + "/agent-definition-revisions"
    ]["post"]["requestBody"]
    assert definition_publication_body == {
        "required": True,
        "content": {
            "text/markdown": {"schema": {"type": "string", "format": "binary"}}
        },
    }

    recognition = schema["paths"][LIBRARY_RECOGNITIONS_PATH]["post"]
    assert recognition["requestBody"] == {
        "required": True,
        "content": {
            "application/octet-stream": {
                "schema": {
                    "type": "string",
                    "format": "binary",
                    "maxLength": api_limits().maximum_request_body_bytes,
                }
            }
        },
    }
    assert recognition["parameters"] == [
        {
            "name": "file_name",
            "in": "query",
            "required": False,
            "schema": {
                "anyOf": [
                    {
                        "type": "string",
                        "maxLength": api_limits().maximum_field_characters,
                    },
                    {"type": "null"},
                ],
                "title": "File Name",
            },
        }
    ]

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


def test_openapi_list_item_names_the_closed_startability_pair() -> None:
    schema = served_app().openapi()
    listing = schema["paths"][API_PREFIX + "/agent-configuration-revisions"]["get"]
    page = _referenced_schema(schema, listing["responses"]["200"]["content"])
    item_ref = page["properties"]["items"]["items"]["$ref"]
    assert item_ref.endswith("AgentConfigurationRevisionListItemResource")
    item = schema["components"]["schemas"]["AgentConfigurationRevisionListItemResource"]
    properties = item.get("properties", {})
    if "allOf" in item:
        properties = {}
        for part in item["allOf"]:
            if "$ref" in part:
                parent = schema["components"]["schemas"][
                    part["$ref"].rsplit("/", 1)[-1]
                ]
                properties.update(parent.get("properties", {}))
            properties.update(part.get("properties", {}))
    assert "startable" in properties
    assert properties["startable"]["type"] == "boolean"
    assert properties["not_startable_reason"]["anyOf"] == [
        {"type": "string", "const": "agent-executor-binding-unavailable"},
        {"type": "null"},
    ]
    publication = schema["components"]["schemas"]["AgentConfigurationRevisionResource"]
    assert "startable" not in publication.get("properties", {})


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


def test_openapi_pins_the_run_order_bounds() -> None:
    """A run resource never echoes an order's own bytes -- the served document
    names the shape and the page bound that make that true, not only the
    running server.
    """
    schema = served_app().openapi()
    order = schema["components"]["schemas"]["RunOrderResource"]
    run_v3 = schema["components"]["schemas"]["RunResourceV3"]

    assert set(order["properties"]) == {"name", "bytes", "schema_revision_hash"}
    assert "value_base64" not in order["properties"]
    assert "preview" not in order["properties"]
    assert run_v3["properties"]["orders"]["maxItems"] == MAXIMUM_RUN_ORDERS
