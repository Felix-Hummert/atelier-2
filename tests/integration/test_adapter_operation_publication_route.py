"""The HTTP door kind `adapter_operation` was missing: bytes in, hash out.

A V3 Action pins an adapter operation by hash. These tests drive the real route
against the real store, then start a workflow that pins the answered hash --
because the sentence this door is worth anything for is not "the bytes were
stored" but "the start gets past the reference that used to refuse as
unpublished".
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.api.openapi import API_PREFIX, MODEL_REGISTRY_PATH
from atelier2.contracts.adapter_operations_v3 import (
    MAXIMUM_ADAPTER_OPERATION_DOCUMENT_BYTES,
    AdapterOperationName,
    AdapterOperationRefusal,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
)
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2, agent_scratch_root
from tests.scenarios.api import durable_api_client
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

OPERATION_PATH = f"{API_PREFIX}/adapter-operation-revisions"
SCHEMA_PATH = f"{API_PREFIX}/schema-revisions"
WORKFLOW_PATH = f"{API_PREFIX}/workflow-revisions"
RUNS_PATH = f"{API_PREFIX}/runs"
THE_OPERATION = json.dumps({"operation": AdapterOperationName.OPEN_PR.value}).encode(
    "utf-8"
)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, "adapter-operation-door-test", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        (RecordingAgentExecutorFactoryV2("exact", "exact/v1", "exact-op", b'"done"'),),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def client(runtime: DbosRuntime) -> TestClient:
    return durable_api_client(runtime)


def action_document(operation_revision: str) -> bytes:
    """One agent then one Action whose operation pin is exactly the published hash."""

    return (
        b"""format_version: 3
name: Land the tree
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Write the tree this chain lands.
"""
        + declared_output()
        + f"""  - id: publish
    type: action
    operation: {{ref: open-pr, revision: {operation_revision}}}
    depends_on: [implement]
    inputs:
      - name: body
        from: {{node: implement, output: result}}
""".encode()
    )


def bind_builder(api: TestClient) -> str:
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
    configuration_hash = str(configuration.json()["agent_configuration_revision_hash"])
    registry = api.put(
        MODEL_REGISTRY_PATH.replace("{provider_id}", "exact"),
        json={
            "revision_number": 1,
            "entries": [
                {
                    "model_id": "opus",
                    "agent_configuration_revision_hash": configuration_hash,
                }
            ],
        },
    )
    assert registry.status_code == 201, registry.text
    return configuration_hash


@pytest.mark.proves(
    "an-open-pr-adapter-operation-is-published-and-pinned-by-a-v3-action"
)
def test_an_operation_published_over_http_lets_a_start_pass_the_pinned_reference(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)

    created = api.post(
        OPERATION_PATH,
        content=THE_OPERATION,
        headers={"content-type": "application/json"},
    )
    retried = api.post(
        OPERATION_PATH,
        content=THE_OPERATION,
        headers={"content-type": "application/json"},
    )

    assert (created.status_code, retried.status_code) == (201, 200)
    assert created.json() == retried.json()
    operation_hash = created.json()["adapter_operation_revision_hash"]
    assert operation_hash == PublishedRevisionHash.of(THE_OPERATION).value
    resolved = DbosCatalogStore(runtime.engine).resolve(
        RevisionKind.ADAPTER_OPERATION, PublishedRevisionHash(operation_hash)
    )
    assert isinstance(resolved, PublishedRevisionFound)
    assert resolved.revision.kind is RevisionKind.ADAPTER_OPERATION
    assert resolved.revision.document == THE_OPERATION

    schema = api.post(
        SCHEMA_PATH,
        content=ANY_JSON_SCHEMA.document,
        headers={"content-type": "application/json"},
    )
    assert schema.status_code == 201, schema.text
    published = api.post(
        WORKFLOW_PATH,
        content=action_document(operation_hash),
        headers={"content-type": "application/yaml"},
    )
    assert published.status_code == 201, published.text

    started = api.post(
        RUNS_PATH,
        json={
            "workflow_format_version": 3,
            "run_id": "v3/open-pr-door",
            "workflow_revision_hash": published.json()["workflow_revision_hash"],
            "agent_bindings": [
                {
                    "role": "builder",
                    "agent_configuration_revision_hash": bind_builder(api),
                }
            ],
            "orders": [],
        },
    )

    assert started.status_code == 201, started.text
    assert started.json()["state"] == "STARTED"


@pytest.mark.parametrize(
    ("document", "refusal"),
    (
        (
            json.dumps(
                {"operation": "x" * MAXIMUM_ADAPTER_OPERATION_DOCUMENT_BYTES}
            ).encode(),
            AdapterOperationRefusal.DOCUMENT_TOO_LARGE,
        ),
        (b"\xff\xfe", AdapterOperationRefusal.DOCUMENT_NOT_UTF8),
        (b"open a pull request", AdapterOperationRefusal.NOT_AN_OPERATION_OBJECT),
        (
            b'{"operation": "open-pr", "repository": "atelier-2"}',
            AdapterOperationRefusal.UNKNOWN_FIELD,
        ),
        (b"{}", AdapterOperationRefusal.MISSING_OPERATION),
        (b'{"operation": "merge"}', AdapterOperationRefusal.UNKNOWN_OPERATION),
    ),
    ids=lambda value: value.value if isinstance(value, AdapterOperationRefusal) else "",
)
def test_an_operation_the_runtime_cannot_perform_is_named_by_its_own_reason(
    runtime: DbosRuntime, document: bytes, refusal: AdapterOperationRefusal
) -> None:
    refused = client(runtime).post(
        OPERATION_PATH, content=document, headers={"content-type": "application/json"}
    )

    assert refused.status_code == 422
    assert refused.json()["type"].endswith(
        f":adapter-operation-{refusal.value.replace('_', '-')}"
    )
    assert refusal.value in refused.json()["detail"]
    missing = DbosCatalogStore(runtime.engine).resolve(
        RevisionKind.ADAPTER_OPERATION, PublishedRevisionHash.of(document)
    )
    assert isinstance(missing, PublishedRevisionMissing)


def test_an_adapter_operation_publication_refuses_the_wrong_media_type(
    runtime: DbosRuntime,
) -> None:
    refused = client(runtime).post(
        OPERATION_PATH,
        content=THE_OPERATION,
        headers={"content-type": "application/yaml"},
    )

    assert refused.status_code == 415
    assert refused.json()["type"].endswith(":unsupported-media-type")
