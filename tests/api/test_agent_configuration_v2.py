from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.projection.events import run_event_resource
from atelier2.api.wire.events import RunEventResourceV2
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestHash,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCollision,
    AgentConfigurationRevisionCreated,
    AgentExecutorBindingUnavailable,
    AuthProfileRevisionCollision,
    AuthProfileRevisionConflict,
    AuthProfileRevisionCreated,
    AuthProfileRevisionMissing,
)
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    DurableStateCorrupt,
    DurableWriteUnavailable,
    StartPublishedRunRequestV2,
)
from atelier2.ports.run_events import PersistedRunEvent
from atelier2.ports.run_queries import RunFound, RunProjection
from tests.api.test_agent_attempts import SERVED_RAIL
from tests.scenarios.api import (
    SSE_COMPLETE_HISTORY,
    api_limits,
    api_ports,
    event_poll_backoff,
)

AUTH = AuthProfileRevision("max", 7, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
CONFIGURATION = AgentConfigurationRevision(
    "claude-opus-4-1",
    AUTH.revision_hash,
    AgentExecutorRevision("claude-cli/v1"),
    AgentExecutionCapability.HEADLESS,
    AgentConfigurationRevisionFormatVersion.V2,
)
INTERACTIVE_CONFIGURATION_HASH = (
    "d881bf2700c0d88b37704959cfb3c44a6bb575e9c1ec93c09650f95aa8ac9279"
)
V2_DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
"""


@dataclass
class RecordingCatalog:
    """A catalog that publishes exactly the revision the route hands it.

    A configured `configuration_result` answers instead, which is how a refusal
    is asked for; without one the response echoes the route's own construction,
    so what the caller reads back is the configuration that would be stored.
    """

    auth_result: object
    configuration_result: object | None = None
    auth_calls: int = 0
    configuration_calls: int = 0

    def publish_auth_profile_revision(self, revision: AuthProfileRevision) -> object:
        self.auth_calls += 1
        assert revision == AUTH
        return self.auth_result

    def publish_agent_configuration_revision(
        self, revision: AgentConfigurationRevision
    ) -> object:
        self.configuration_calls += 1
        if self.configuration_result is None:
            return AgentConfigurationRevisionCreated(revision, AUTH)
        return self.configuration_result

    def auth_profile_revision(self, _revision_hash: object) -> None:
        return None

    def agent_configuration_revision(self, _revision_hash: object) -> None:
        return None


def _client(catalog: RecordingCatalog) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                workflow_document_parser=parse_executable_workflow_document,
                agent_configuration_catalog=catalog,
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def _publish_auth(client: TestClient):
    return client.post(
        API_PREFIX + "/auth-profile-revisions",
        json={
            "profile_id": "max",
            "revision_number": 7,
            "provider_id": "anthropic",
            "auth_mode": "subscription",
        },
    )


def _publish_configuration(client: TestClient, **capability: object):
    return client.post(
        API_PREFIX + "/agent-configuration-revisions",
        json={
            "model": "claude-opus-4-1",
            "auth_profile_revision_hash": AUTH.revision_hash.value,
            "executor_revision": "claude-cli/v1",
            **capability,
        },
    )


def test_publish_resources_have_the_exact_secret_free_wire_shape() -> None:
    catalog = RecordingCatalog(AuthProfileRevisionCreated(AUTH))
    client = _client(catalog)

    auth = _publish_auth(client)
    configuration = _publish_configuration(client)

    assert auth.status_code == 201
    assert auth.json() == {
        "profile_id": "max",
        "revision_number": 7,
        "provider_id": "anthropic",
        "auth_mode": "subscription",
        "auth_profile_revision_hash": AUTH.revision_hash.value,
    }
    assert configuration.status_code == 201
    assert configuration.json() == {
        "model": "claude-opus-4-1",
        "auth_profile_revision_hash": AUTH.revision_hash.value,
        "executor_revision": "claude-cli/v1",
        "provider_id": "anthropic",
        "auth_mode": "subscription",
        "requested_capability": "headless",
        "agent_configuration_revision_hash": CONFIGURATION.revision_hash.value,
    }
    assert all(
        forbidden not in (auth.text + configuration.text).lower()
        for forbidden in ("secret", "credential", "handle", "api_key_value")
    )


@pytest.mark.parametrize(
    ("published", "echoed", "configuration_hash"),
    [
        ({}, "headless", CONFIGURATION.revision_hash.value),
        (
            {"requested_capability": "headless"},
            "headless",
            CONFIGURATION.revision_hash.value,
        ),
        (
            {"requested_capability": "interactive"},
            "interactive",
            INTERACTIVE_CONFIGURATION_HASH,
        ),
    ],
)
def test_the_requested_capability_decides_the_published_configuration(
    published: dict[str, object], echoed: str, configuration_hash: str
) -> None:
    """What a caller asks for is what is published, and what identifies it.

    Omitting the field and asking for `headless` are the same publication as
    the one this API made before the field existed, down to the hash; asking
    for `interactive` publishes a different configuration, which is why the
    interactive identity is authored here rather than derived.
    """

    client = _client(RecordingCatalog(AuthProfileRevisionCreated(AUTH)))

    response = _publish_configuration(client, **published)

    assert response.status_code == 201
    assert response.json()["requested_capability"] == echoed
    assert response.json()["agent_configuration_revision_hash"] == configuration_hash


@pytest.mark.parametrize(
    "requested_capability",
    [None, "Headless", "INTERACTIVE", "supervised", "", 1],
)
def test_an_uncontracted_capability_is_refused_before_any_catalog_effect(
    requested_capability: object,
) -> None:
    catalog = RecordingCatalog(AuthProfileRevisionCreated(AUTH))

    response = _publish_configuration(
        _client(catalog), requested_capability=requested_capability
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-request")
    assert catalog.configuration_calls == 0


@pytest.mark.parametrize(
    ("result", "status", "code"),
    [
        (AuthProfileRevisionConflict(), 409, "auth-profile-revision-conflict"),
        (AuthProfileRevisionCollision(), 409, "auth-profile-revision-collision"),
        (DurableWriteUnavailable(), 503, "temporarily-unavailable"),
        (DurableStateCorrupt(), 500, "durable-state-corrupt"),
    ],
)
def test_auth_publish_maps_every_refusal(
    result: object, status: int, code: str
) -> None:
    response = _publish_auth(_client(RecordingCatalog(result, object())))

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == f"urn:atelier2:problem:v1:{code}"


@pytest.mark.parametrize(
    ("result", "status", "code"),
    [
        (AuthProfileRevisionMissing(), 404, "auth-profile-revision-not-found"),
        (
            AgentExecutorBindingUnavailable(),
            409,
            "agent-executor-binding-unavailable",
        ),
        (
            AgentConfigurationRevisionCollision(),
            409,
            "agent-configuration-revision-collision",
        ),
        (DurableWriteUnavailable(), 503, "temporarily-unavailable"),
        (DurableStateCorrupt(), 500, "durable-state-corrupt"),
    ],
)
def test_configuration_publish_maps_every_refusal(
    result: object, status: int, code: str
) -> None:
    response = _publish_configuration(_client(RecordingCatalog(object(), result)))

    assert response.status_code == status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == f"urn:atelier2:problem:v1:{code}"


def test_invalid_provider_is_rejected_before_catalog_effect() -> None:
    catalog = RecordingCatalog(object(), object())
    response = _client(catalog).post(
        API_PREFIX + "/auth-profile-revisions",
        json={
            "profile_id": "max",
            "revision_number": 7,
            "provider_id": "Anthropic",
            "auth_mode": "subscription",
        },
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-request")
    assert catalog.auth_calls == 0


@pytest.mark.parametrize(
    "extra",
    [
        {"workflow_format_version": 2},
        {"agent_bindings": []},
    ],
)
def test_malformed_versioned_start_is_the_exact_agent_bindings_problem(
    extra: dict[str, object],
) -> None:
    client = _client(RecordingCatalog(object(), object()))
    body: dict[str, object] = {
        "run_id": "v2/malformed",
        "workflow_revision_hash": "0" * 64,
        **extra,
    }

    response = client.post(API_PREFIX + "/runs", json=body)

    assert response.status_code == 422
    assert response.json()["type"].endswith(":invalid-agent-bindings")


def test_openapi_names_both_publish_operations_and_exact_problem_sets() -> None:
    schema = cast(FastAPI, _client(RecordingCatalog(object(), object())).app).openapi()

    for path in (
        API_PREFIX + "/auth-profile-revisions",
        API_PREFIX + "/agent-configuration-revisions",
    ):
        operation = schema["paths"][path]["post"]
        assert set(operation["responses"]) >= {"200", "201", "409", "422", "500", "503"}
        assert set(operation["requestBody"]["content"]) == {"application/json"}
    configuration_responses = schema["paths"][
        API_PREFIX + "/agent-configuration-revisions"
    ]["post"]["responses"]
    assert "404" in configuration_responses

    start = schema["paths"][API_PREFIX + "/runs"]["post"]
    assert [
        item["$ref"]
        for item in start["requestBody"]["content"]["application/json"]["schema"][
            "oneOf"
        ]
    ] == [
        "#/components/schemas/StartRunRequestResource",
        "#/components/schemas/StartRunRequestResourceV2",
    ]
    assert [
        item["$ref"]
        for item in start["responses"]["201"]["content"]["application/json"]["schema"][
            "oneOf"
        ]
    ] == [
        "#/components/schemas/RunResource",
        "#/components/schemas/RunResourceV2",
    ]
    assert schema["paths"][API_PREFIX + "/runs"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/VersionedRunPageResource"}
    assert (
        "oneOf"
        in schema["components"]["schemas"]["VersionedRunPageResource"]["properties"][
            "items"
        ]["items"]
    )
    graph = schema["components"]["schemas"]["WorkflowRevisionDetailResource"][
        "properties"
    ]["graph"]
    assert set(graph["discriminator"]["mapping"]) == {"1", "2", "3"}
    assert len(graph["oneOf"]) == 3


def test_v2_start_binds_roles_and_returns_the_exact_versioned_run_shape() -> None:
    workflow = WorkflowRevision(V2_DOCUMENT)
    graph = parse_executable_workflow_document(V2_DOCUMENT)
    binding_set = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), CONFIGURATION.revision_hash),)
    )
    resolved = ResolvedAgentBinding(AgentRole("builder"), CONFIGURATION, AUTH)
    run = RunV2(
        RunId("v2/run"),
        workflow.revision_hash,
        binding_set.binding_set_hash,
        (resolved,),
        RunState.STARTED,
        "build",
        0,
        0,
    )

    @dataclass
    class Starter:
        request: object | None = None

        def start_published(self, request: object) -> DurableRunCreated:
            self.request = request
            return DurableRunCreated(run)

    class Queries:
        def get_run(self, _run_id: RunId, _projection_limit: object) -> RunFound:
            return RunFound(RunProjection(run, graph, None))

    starter = Starter()
    queries = Queries()
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                published_run_starter=starter,
                run_queries=queries,
                workflow_document_parser=parse_executable_workflow_document,
                agent_configuration_catalog=RecordingCatalog(object(), object()),
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )

    response = client.post(
        API_PREFIX + "/runs",
        json={
            "workflow_format_version": 2,
            "run_id": "v2/run",
            "workflow_revision_hash": workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": "builder",
                    "agent_configuration_revision_hash": CONFIGURATION.revision_hash.value,
                }
            ],
        },
    )

    assert response.status_code == 201
    assert isinstance(starter.request, StartPublishedRunRequestV2)
    assert starter.request.agent_bindings == binding_set
    assert response.json() == {
        "workflow_format_version": 2,
        "run_id": "v2/run",
        "public_run_reference": "run1.djIvcnVu",
        "workflow_revision_hash": workflow.revision_hash.value,
        "agent_binding_set_hash": binding_set.binding_set_hash.value,
        "agent_bindings": [
            {
                "role": "builder",
                "agent_configuration_revision_hash": CONFIGURATION.revision_hash.value,
                "auth_profile_revision_hash": AUTH.revision_hash.value,
                "profile_id": "max",
                "revision_number": 7,
                "provider_id": "anthropic",
                "auth_mode": "subscription",
                "model": "claude-opus-4-1",
                "executor_revision": "claude-cli/v1",
            }
        ],
        "state_version": 0,
        "state": "STARTED",
        "current_node": {
            "type": "agent",
            "node_id": "build",
            "role": "builder",
            "job": "build",
            "next_node_id": "done",
        },
        "node_rail": [
            {"node_id": "build", "state": "working", "attempt": None},
            {"node_id": "done", "state": "queued", "attempt": None},
        ],
        "agent_attempts": [],
        "waiting": {"type": "NONE"},
        "terminal_hash": None,
        "latest_event_cursor": None,
    }


def test_v2_agent_event_roundtrips_arbitrary_bytes_as_canonical_base64() -> None:
    workflow = WorkflowRevision(V2_DOCUMENT)
    run_id = RunId("v2/non-utf8")
    event = RunEvent(
        run_id,
        workflow.revision_hash,
        1,
        "build",
        NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
        RunEventKind.AGENT_COMPLETED,
        b"\x00\xffoutput",
        agent_attempt_id=AgentAttemptId.for_execution(
            NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
            AgentExecutionRequestHash("1" * 64),
            1,
        ).value,
        attempt_ordinal=1,
    )

    resource = run_event_resource(PersistedRunEvent(event, None, 2), SERVED_RAIL)
    api_limits().require_event_projection(PersistedRunEvent(event, None, 2))

    assert resource.model_dump(mode="json") == {
        "workflow_format_version": 2,
        "node_rail": [
            {"node_id": "build", "state": "working", "attempt": None},
            {"node_id": "done", "state": "queued", "attempt": None},
        ],
        "cursor": "event1.djIvbm9uLXV0Zjg.1",
        "sequence": 1,
        "public_run_reference": "run1.djIvbm9uLXV0Zjg",
        "workflow_revision_hash": workflow.revision_hash.value,
        "node_id": "build",
        "node_execution_id": event.node_execution_id.value,
        "event_hash": event.event_hash.value,
        "event": "AGENT_COMPLETED",
        "output_base64": "AP9vdXRwdXQ=",
        "output_hash": event.payload_hash.value,
        "attempt_id": event.agent_attempt_id,
        "attempt_ordinal": 1,
    }


def test_all_seven_v2_event_dtos_have_the_exact_closed_wire_shape() -> None:
    adapter = TypeAdapter(RunEventResourceV2)

    for envelope in SSE_COMPLETE_HISTORY:
        expected = {**cast(dict[str, object], envelope["data"])}
        expected["workflow_format_version"] = 2
        expected["node_rail"] = [entry.model_dump(mode="json") for entry in SERVED_RAIL]
        if expected["event"] == "AGENT_COMPLETED":
            output = cast(str, expected.pop("output"))
            expected["output_base64"] = base64.b64encode(output.encode()).decode()
            expected["output_hash"] = expected.pop("payload_hash")
            expected["attempt_id"] = "a" * 64
            expected["attempt_ordinal"] = 1

        resource = adapter.validate_python({**expected, "node_rail": SERVED_RAIL})

        assert resource.model_dump(mode="json") == expected


def test_openapi_sse_data_is_an_untagged_v1_v2_one_of() -> None:
    schema = cast(FastAPI, _client(RecordingCatalog(object(), object())).app).openapi()

    data = schema["paths"][API_PREFIX + "/runs/{public_ref}/events"]["get"][
        "responses"
    ]["200"]["content"]["text/event-stream"]["x-atelier2-sse-v1"]["durable_event"][
        "data"
    ]
    assert data == {"$ref": "#/components/schemas/VersionedRunEventResource"}
    union = schema["components"]["schemas"]["VersionedRunEventResource"]
    assert union == {
        "oneOf": [
            {"$ref": "#/components/schemas/RunEventResource"},
            {"$ref": "#/components/schemas/RunEventResourceV2"},
        ]
    }
    v2 = schema["components"]["schemas"]["RunEventResourceV2"]
    assert len(v2["oneOf"]) == 11
    assert set(v2["discriminator"]["mapping"]) == {kind.value for kind in RunEventKind}
    common = {
        "workflow_format_version",
        "cursor",
        "sequence",
        "public_run_reference",
        "workflow_revision_hash",
        "node_id",
        "node_execution_id",
        "event_hash",
        "event",
        "node_rail",
    }
    payloads = {
        "AGENT_COMPLETED": {
            "output_base64",
            "output_hash",
            "attempt_id",
            "attempt_ordinal",
        },
        "AGENT_FAILED": {"failure_code", "attempt_id", "attempt_ordinal"},
        "AGENT_CANCEL_REQUESTED": {
            "attempt_id",
            "attempt_ordinal",
            "command_id",
            "replacement",
        },
        "AGENT_CANCELLED": {
            "attempt_id",
            "attempt_ordinal",
            "command_id",
            "replacement",
            "disposition",
            "replacement_attempt_id",
        },
        "AGENT_INTERRUPTED": {
            "attempt_id",
            "attempt_ordinal",
            "command_id",
            "replacement",
            "disposition",
            "replacement_attempt_id",
        },
        "ACTION_RECONCILIATION_REQUIRED": {"request_base64", "request_hash"},
        "ACTION_RECONCILIATION_RESOLVED": {"receipt"},
        "ACTION_COMPLETED": {"receipt"},
        "WAITING_INPUT": {"answer_type"},
        "WAIT_ANSWERED": {"answer", "answer_hash"},
        "SUBWORKFLOW_COMPLETED": {"result", "result_hash"},
    }
    for event, reference in v2["discriminator"]["mapping"].items():
        component = schema["components"]["schemas"][reference.rsplit("/", 1)[-1]]
        expected_fields = common | payloads[event]
        assert set(component["properties"]) == expected_fields
        assert set(component["required"]) == expected_fields
        assert component["additionalProperties"] is False


def test_openapi_has_no_private_credential_channel() -> None:
    schema = cast(FastAPI, _client(RecordingCatalog(object(), object())).app).openapi()
    forbidden_properties = {
        "secret",
        "secret_value",
        "credential",
        "credentials",
        "credential_handle",
        "credential_ref",
        "credential_path",
        "api_key_value",
        "key_name",
        "lookup_key",
    }

    property_names = {
        property_name
        for component in schema["components"]["schemas"].values()
        for property_name in component.get("properties", {})
    }

    assert property_names.isdisjoint(forbidden_properties)
