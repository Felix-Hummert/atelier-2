"""A stdio MCP client lists the catalog, starts a named run, and reads it terminal."""

from __future__ import annotations

import base64
import json
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Thread

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_public_project_reference
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.runs import RunState
from atelier2.contracts.schemas_v3 import MAXIMUM_INSTANCE_DOCUMENT_BYTES
from atelier2.host.mcp_tools import McpToolName
from tests.host.test_mcp_server import StdioMcpSession
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.api import (
    api_limits,
    durable_ports,
    event_poll_backoff,
)
from tests.scenarios.projects import declaring_verification, git_project
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

SERVED_PROJECT = ProjectId("atelier")
WORKFLOW_NAME = "mcp-named-line"
ARTIFACT_WORKFLOW_NAME = "mcp-artifact-line"
ORDER_NAME = "order"
PROVIDER_OUTPUT = b'"the exact provider bytes"'
DOCUMENT = (
    f"""format_version: 3
name: {WORKFLOW_NAME}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this named run is for.
""".encode()
    + declared_output()
)
ORDERED_DOCUMENT = (
    f"""format_version: 3
name: {ARTIFACT_WORKFLOW_NAME}
graph_inputs:
  - name: {ORDER_NAME}
    schema:
      ref: order-schema
      revision: {ANY_JSON_SCHEMA.revision_hash.value}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Review the ordered material.
    inputs:
      - name: {ORDER_NAME}
        from:
          graph_input: {ORDER_NAME}
""".encode()
    + declared_output()
)


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    yield from _started_runtime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "mcp-named-run",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        tmp_path,
    )


@pytest.fixture
def served_runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    """The same runtime, serving one project whose occupancy a start may read."""
    project_root = tmp_path / "project"
    git_project(project_root, declaring_verification(["true"]))
    yield from _started_runtime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "mcp-named-run",
            agent_scratch_root=agent_scratch_root(tmp_path),
            project_id=SERVED_PROJECT,
            bootstrap_project_root=project_root,
        ),
        tmp_path,
    )


def _started_runtime(
    settings: DbosRuntimeSettings, tmp_path: Path
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    recording = RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
    )
    started = DbosRuntime(
        settings,
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (recording,),
    )
    started.initialize_storage()
    try:
        yield started, recording
    finally:
        started.close()


def application(runtime: DbosRuntime) -> FastAPI:
    return create_app(
        source_commit="commit",
        source_tree="tree",
        ports=durable_ports(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        ),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
        served_project_id=runtime.settings.project_id,
    )


@contextmanager
def live_server(app: FastAPI) -> Iterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=0,
                log_level="critical",
                access_log=False,
                lifespan="off",
            )
        )
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=5)
            raise AssertionError("Uvicorn did not start for the MCP named-run proof")
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


@dataclass(frozen=True)
class PublishedLine:
    """What a start needs of a published named line: its lineage, one binding."""

    lineage_id: str
    configuration_hash: str


def publish_named_line(app: FastAPI, document: bytes = DOCUMENT) -> PublishedLine:
    """Publish the schema, the named document, its lineage and one binding."""

    api = TestClient(app)
    schema = api.post(
        API_PREFIX + "/schema-revisions",
        content=ANY_JSON_SCHEMA.document,
        headers={"content-type": "application/json"},
    )
    assert schema.status_code == 201, schema.text
    workflow = api.post(
        API_PREFIX + "/workflow-revisions",
        content=document,
        headers={"content-type": "application/yaml"},
    )
    assert workflow.status_code == 201, workflow.text
    revision_hash = workflow.json()["workflow_revision_hash"]
    named = api.post(
        API_PREFIX + "/workflow-lineages",
        json={
            "workflow_revision_hash": revision_hash,
            "actor": "operator",
            "activated_at": "2026-08-17T00:00:00Z",
        },
    )
    assert named.status_code == 201, named.text
    auth = api.post(
        API_PREFIX + "/auth-profile-revisions",
        json={
            "profile_id": "max",
            "revision_number": 1,
            "provider_id": "exact",
            "auth_mode": "subscription",
        },
    )
    assert auth.status_code == 201, auth.text
    configuration = api.post(
        API_PREFIX + "/agent-configuration-revisions",
        json={
            "model": "opus",
            "auth_profile_revision_hash": auth.json()["auth_profile_revision_hash"],
            "executor_revision": "exact/v1",
        },
    )
    assert configuration.status_code == 201, configuration.text
    return PublishedLine(
        str(named.json()["lineage_id"]),
        str(configuration.json()["agent_configuration_revision_hash"]),
    )


def occupy(app: FastAPI, line: PublishedLine, role: str) -> None:
    """Cast this role on the served project, as the console's occupancy does."""
    project = encode_public_project_reference(SERVED_PROJECT)
    occupied = TestClient(app).put(
        f"{API_PREFIX}/projects/{project}/occupancy/{line.lineage_id}",
        json={
            "revision_number": 1,
            "bindings": [
                {
                    "role": role,
                    "agent_configuration_revision_hash": line.configuration_hash,
                }
            ],
        },
    )
    assert occupied.status_code == 201, occupied.text


def wait_for_terminal(client: StdioMcpSession, reference: str) -> dict[str, object]:
    deadline = time.monotonic() + 8
    observed: dict[str, object] = {}
    while time.monotonic() < deadline:
        payload, is_error = client.call_tool(
            McpToolName.RUN_STATUS.value, {"public_run_reference": reference}
        )
        assert not is_error
        assert isinstance(payload, dict)
        observed = payload
        if payload.get("state") in {
            RunState.COMPLETED.value,
            RunState.FAILED.value,
        }:
            return payload
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed.get('state')!r}, expected terminal")


@pytest.mark.proves("a-stdio-client-lists-starts-and-reads-a-named-run")
def test_a_stdio_client_lists_the_catalog_starts_by_name_and_reads_terminal(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    started_runtime, _recording = runtime
    app = application(started_runtime)
    configuration_hash = publish_named_line(app).configuration_hash

    with live_server(app) as service_url:
        client = StdioMcpSession(service_url)
        try:
            listed, list_failed = client.call_tool(McpToolName.LIST_WORKFLOWS.value, {})
            assert not list_failed
            assert isinstance(listed, dict)
            names = [item["display_name"] for item in listed["items"]]
            assert names == [WORKFLOW_NAME]
            assert listed["items"][0]["revision_number"] == 1

            started, start_failed = client.call_tool(
                McpToolName.START_RUN.value,
                {
                    "name": WORKFLOW_NAME,
                    "run_id": "mcp/named-line",
                    "agent_bindings": [
                        {
                            "role": "builder",
                            "agent_configuration_revision_hash": configuration_hash,
                        }
                    ],
                },
            )
            assert not start_failed
            assert isinstance(started, dict)
            assert started["state"] == RunState.STARTED.value
            reference = str(started["public_run_reference"])

            started_runtime.launch()
            ended = wait_for_terminal(client, reference)
        finally:
            client.close()

    assert ended["state"] == RunState.COMPLETED.value
    assert ended["terminal_hash"] is not None
    assert (
        ended["workflow_revision_hash"] == listed["items"][0]["workflow_revision_hash"]
    )


@pytest.mark.proves("a-stdio-client-lists-starts-and-reads-a-named-run")
def test_a_stdio_client_publishes_an_artifact_starts_by_address_and_reads_terminal(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    started_runtime, recording = runtime
    app = application(started_runtime)
    configuration_hash = publish_named_line(app, ORDERED_DOCUMENT).configuration_hash
    ordered_material = json.dumps({"diff": "x" * 20_000}).encode()
    assert len(ordered_material) > MAXIMUM_INSTANCE_DOCUMENT_BYTES

    with live_server(app) as service_url:
        client = StdioMcpSession(service_url)
        try:
            published, publish_failed = client.call_tool(
                McpToolName.PUBLISH_ARTIFACT.value,
                {
                    "content_base64": base64.standard_b64encode(
                        ordered_material
                    ).decode("ascii")
                },
            )
            assert not publish_failed
            assert isinstance(published, dict)
            address = str(published["artifact_hash"])

            started, start_failed = client.call_tool(
                McpToolName.START_RUN.value,
                {
                    "name": ARTIFACT_WORKFLOW_NAME,
                    "run_id": "mcp/artifact-order",
                    "agent_bindings": [
                        {
                            "role": "builder",
                            "agent_configuration_revision_hash": configuration_hash,
                        }
                    ],
                    "orders": [{"name": ORDER_NAME, "artifact_hash": address}],
                },
            )
            assert not start_failed
            assert isinstance(started, dict)
            assert started["state"] == RunState.STARTED.value
            reference = str(started["public_run_reference"])

            started_runtime.launch()
            ended = wait_for_terminal(client, reference)
        finally:
            client.close()

    assert ended["state"] == RunState.COMPLETED.value
    assert ended["terminal_hash"] is not None
    assert recording.opened is not None
    assert ordered_material in recording.opened.requests[0].job_bytes


@pytest.mark.proves("mcp-and-http-never-diverge")
def test_a_stdio_start_without_bindings_runs_on_the_projects_occupancy(
    served_runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The conductor's start names no binding; the served project's occupancy does.

    Both doors are one start: the MCP tool posts the body the HTTP door takes,
    and the starter casts the roles nobody bound from the occupancy (#680). A
    revision the described listing calls executable therefore starts from the
    stdio door exactly as it does from the console (#701).
    """
    started_runtime, _recording = served_runtime
    app = application(started_runtime)
    line = publish_named_line(app)
    occupy(app, line, "builder")

    with live_server(app) as service_url:
        client = StdioMcpSession(service_url)
        try:
            listed, list_failed = client.call_tool(McpToolName.LIST_WORKFLOWS.value, {})
            assert not list_failed
            assert isinstance(listed, dict)
            assert [item["display_name"] for item in listed["items"]] == [WORKFLOW_NAME]

            started, start_failed = client.call_tool(
                McpToolName.START_RUN.value,
                {"name": WORKFLOW_NAME, "run_id": "mcp/occupied-line"},
            )
            assert not start_failed, started
            assert isinstance(started, dict)
            assert started["state"] == RunState.STARTED.value
            assert [
                (binding["role"], binding["agent_configuration_revision_hash"])
                for binding in started["agent_bindings"]
            ] == [("builder", line.configuration_hash)]

            started_runtime.launch()
            ended = wait_for_terminal(client, str(started["public_run_reference"]))
        finally:
            client.close()

    assert ended["state"] == RunState.COMPLETED.value
