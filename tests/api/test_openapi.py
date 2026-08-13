from __future__ import annotations

import json
from typing import cast

import pytest
from fastapi.testclient import TestClient
from openapi_spec_validator import OpenAPIV31SpecValidator, validate

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api import openapi as openapi_module
from atelier2.api.app import ApiPorts, create_app
from atelier2.api.openapi import API_PREFIX, EVENT_NAMES, EVENT_NAMES_V2, EVENT_PATH
from atelier2.ports.agent_configurations import AgentConfigurationCatalog
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    TransactionalWaitAnswerer,
)
from atelier2.ports.effects import TransactionalEffectReconcileCommander
from atelier2.ports.run_events import RunEventQueries
from atelier2.ports.run_queries import RunQueries
from atelier2.ports.workflow_revisions import (
    WorkflowRevisionPublisher,
    WorkflowRevisionQueries,
)
from tests.scenarios.api import api_limits, event_poll_backoff


def empty_ports() -> ApiPorts:
    missing = object()

    return ApiPorts(
        workflow_revision_publisher=cast(WorkflowRevisionPublisher, missing),
        published_run_starter=cast(DurablePublishedRunStarter, missing),
        wait_answerer=cast(TransactionalWaitAnswerer, missing),
        reconcile_commander=cast(TransactionalEffectReconcileCommander, missing),
        workflow_revision_queries=cast(WorkflowRevisionQueries, missing),
        run_queries=cast(RunQueries, missing),
        run_event_queries=cast(RunEventQueries, missing),
        workflow_document_parser=parse_workflow_document,
        agent_configuration_catalog=cast(AgentConfigurationCatalog, missing),
    )


EXPECTED_PATHS = {
    API_PREFIX + "/health",
    API_PREFIX + "/auth-profile-revisions",
    API_PREFIX + "/agent-configuration-revisions",
    API_PREFIX + "/workflow-revisions",
    API_PREFIX + "/workflow-revisions/{revision_hash}",
    API_PREFIX + "/runs",
    API_PREFIX + "/runs/{public_ref}",
    API_PREFIX + "/runs/{public_ref}/answers",
    API_PREFIX + "/runs/{public_ref}/reconciliations",
    EVENT_PATH,
}

EXPECTED_SUCCESS_STATUSES = {
    (API_PREFIX + "/health", "get"): {"200"},
    (API_PREFIX + "/auth-profile-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/agent-configuration-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/workflow-revisions", "post"): {"200", "201"},
    (API_PREFIX + "/workflow-revisions", "get"): {"200"},
    (API_PREFIX + "/workflow-revisions/{revision_hash}", "get"): {"200"},
    (API_PREFIX + "/runs", "post"): {"200", "201"},
    (API_PREFIX + "/runs", "get"): {"200"},
    (API_PREFIX + "/runs/{public_ref}", "get"): {"200"},
    (API_PREFIX + "/runs/{public_ref}/answers", "post"): {"200", "202"},
    (API_PREFIX + "/runs/{public_ref}/reconciliations", "post"): {"200", "202"},
    (EVENT_PATH, "get"): {"200"},
}


def test_openapi_31_validates_and_describes_exact_r2_surface() -> None:
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=empty_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )

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
    app = create_app(
        source_commit="commit",
        source_tree="tree",
        ports=empty_ports(),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
    )
    schema = app.openapi()

    content = schema["paths"][EVENT_PATH]["get"]["responses"]["200"]["content"]
    assert set(content) == {"text/event-stream"}
    assert content["text/event-stream"]["schema"] == {"type": "string"}
    extension = content["text/event-stream"]["x-atelier2-sse-v1"]
    assert extension["id"] == {"$ref": "#/components/schemas/EventCursor"}
    assert extension["event"] == {"enum": list(EVENT_NAMES_V2)}
    assert extension["data"] == {
        "$ref": "#/components/schemas/VersionedRunEventResource"
    }
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
    schema = create_app(
        source_commit="commit",
        source_tree="tree",
        ports=empty_ports(),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
    ).openapi()

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
    schema = create_app(
        source_commit="commit",
        source_tree="tree",
        ports=empty_ports(),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
    ).openapi()

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
            "application/yaml": {"schema": {"type": "string", "format": "binary"}}
        },
    }

    for path in (
        API_PREFIX + "/auth-profile-revisions",
        API_PREFIX + "/agent-configuration-revisions",
        API_PREFIX + "/runs",
        API_PREFIX + "/runs/{public_ref}/answers",
        API_PREFIX + "/runs/{public_ref}/reconciliations",
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
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=empty_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )


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

    app = create_app(
        source_commit="commit",
        source_tree="tree",
        ports=empty_ports(),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
    )
    assert generated == 1

    response = TestClient(app).get(API_PREFIX + "/health")

    assert response.status_code == 200
    assert generated == 1
