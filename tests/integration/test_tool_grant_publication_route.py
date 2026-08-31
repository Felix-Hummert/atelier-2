"""The HTTP door kind `tool` was missing: a grant revision, bytes in, hash out.

A V3 node pins a tool grant by hash. Until this door the operator had to stop
the unit and call publish_revision in-process. These tests drive the real route
against the real store, then start a workflow that pins the answered hash --
because the sentence this door is worth anything for is not "the bytes were
stored" but "the start gets past the reference that used to refuse as
unpublished". Redemption itself is already built and is not re-proven here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX, MODEL_REGISTRY_PATH
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.tool_grants_v3 import (
    MAXIMUM_TOOL_GRANT_DOCUMENT_BYTES,
    ToolGrantCapability,
    ToolGrantRefusal,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
)
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2, agent_scratch_root
from tests.scenarios.api import durable_api_client
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

GRANT_PATH = f"{API_PREFIX}/tool-grant-revisions"
SCHEMA_PATH = f"{API_PREFIX}/schema-revisions"
WORKFLOW_PATH = f"{API_PREFIX}/workflow-revisions"
RUNS_PATH = f"{API_PREFIX}/runs"
THE_GRANT = json.dumps(
    {"capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION.value}
).encode("utf-8")


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "tool-grant-door-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (RecordingAgentExecutorFactoryV2("exact", "exact/v1", "exact-op", b'"done"'),),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def client(runtime: DbosRuntime) -> TestClient:
    return durable_api_client(runtime)


def granted_document(grant_revision: str) -> bytes:
    """One agent whose tools entry pins exactly the published grant."""

    return (
        f"""format_version: 3
name: One agent that must verify the project
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
    tools:
      - {{ref: project-verification, revision: {grant_revision}}}
""".encode()
        + declared_output()
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


def start_granted(
    api: TestClient,
    run_id: str,
    workflow_revision_hash: str,
    configuration_hash: str,
) -> Response:
    return api.post(
        RUNS_PATH,
        json={
            "workflow_format_version": 3,
            "run_id": run_id,
            "workflow_revision_hash": workflow_revision_hash,
            "agent_bindings": [
                {
                    "role": "builder",
                    "agent_configuration_revision_hash": configuration_hash,
                }
            ],
            "orders": [],
        },
    )


@pytest.mark.proves("a-tool-grant-published-over-the-api-is-the-hash-a-node-pins")
def test_a_grant_published_over_http_lets_a_start_pass_the_pinned_reference(
    runtime: DbosRuntime,
) -> None:
    """The live hole: POST the grant, then start the workflow that pins it.

    The start used to refuse as `workflow-format-not-executable` because the
    reference never resolved. With the door, it creates the run. The attempt
    that would redeem the grant is not launched here -- that vertical already
    exists.
    """

    api = client(runtime)

    created = api.post(
        GRANT_PATH, content=THE_GRANT, headers={"content-type": "application/json"}
    )
    retried = api.post(
        GRANT_PATH, content=THE_GRANT, headers={"content-type": "application/json"}
    )

    assert (created.status_code, retried.status_code) == (201, 200)
    assert created.json() == retried.json()
    grant_hash = created.json()["tool_grant_revision_hash"]
    assert grant_hash == PublishedRevisionHash.of(THE_GRANT).value
    resolved = DbosCatalogStore(runtime.engine).resolve(
        RevisionKind.TOOL, PublishedRevisionHash(grant_hash)
    )
    assert isinstance(resolved, PublishedRevisionFound)
    assert resolved.revision.kind is RevisionKind.TOOL
    assert resolved.revision.document == THE_GRANT

    schema = api.post(
        SCHEMA_PATH,
        content=ANY_JSON_SCHEMA.document,
        headers={"content-type": "application/json"},
    )
    assert schema.status_code == 201, schema.text
    published = api.post(
        WORKFLOW_PATH,
        content=granted_document(grant_hash),
        headers={"content-type": "application/yaml"},
    )
    assert published.status_code == 201, published.text

    started = start_granted(
        api,
        "v3/grant-door",
        published.json()["workflow_revision_hash"],
        bind_builder(api),
    )

    assert started.status_code == 201, started.text
    assert started.json()["state"] == "STARTED"
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(runs.c.run_id).where(runs.c.run_id == "v3/grant-door")
            )
            == "v3/grant-door"
        )


@pytest.mark.proves("a-tool-grant-published-over-the-api-is-the-hash-a-node-pins")
def test_a_start_still_refuses_when_the_pinned_grant_was_never_published(
    runtime: DbosRuntime,
) -> None:
    """The hole this door fills, still named, so the mutation has a second side."""

    api = client(runtime)
    schema = api.post(
        SCHEMA_PATH,
        content=ANY_JSON_SCHEMA.document,
        headers={"content-type": "application/json"},
    )
    assert schema.status_code == 201, schema.text
    published = api.post(
        WORKFLOW_PATH,
        content=granted_document("f0" * 32),
        headers={"content-type": "application/yaml"},
    )
    assert published.status_code == 201, published.text

    refused = start_granted(
        api,
        "v3/unpublished-grant",
        published.json()["workflow_revision_hash"],
        bind_builder(api),
    )

    assert refused.status_code == 409
    assert refused.json()["type"].endswith(":workflow-format-not-executable")
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


@pytest.mark.parametrize(
    ("document", "refusal"),
    (
        (
            json.dumps(
                {"capability": "x" * MAXIMUM_TOOL_GRANT_DOCUMENT_BYTES}
            ).encode(),
            ToolGrantRefusal.DOCUMENT_TOO_LARGE,
        ),
        (b"\xff\xfe", ToolGrantRefusal.DOCUMENT_NOT_UTF8),
        (b"you may run the tests", ToolGrantRefusal.NOT_A_GRANT_OBJECT),
        (
            b'{"capability": "run-project-verification", "network": "all"}',
            ToolGrantRefusal.UNKNOWN_FIELD,
        ),
        (b"{}", ToolGrantRefusal.MISSING_CAPABILITY),
        (
            b'{"capability": "delete-the-repository"}',
            ToolGrantRefusal.UNKNOWN_CAPABILITY,
        ),
    ),
    ids=lambda value: value.value if isinstance(value, ToolGrantRefusal) else "",
)
def test_a_grant_the_runtime_cannot_redeem_is_named_by_its_own_reason(
    runtime: DbosRuntime, document: bytes, refusal: ToolGrantRefusal
) -> None:
    refused = client(runtime).post(
        GRANT_PATH, content=document, headers={"content-type": "application/json"}
    )

    assert refused.status_code == 422
    assert refused.json()["type"].endswith(f":tool-{refusal.value.replace('_', '-')}")
    assert refusal.value in refused.json()["detail"]
    missing = DbosCatalogStore(runtime.engine).resolve(
        RevisionKind.TOOL, PublishedRevisionHash.of(document)
    )
    assert isinstance(missing, PublishedRevisionMissing)


def test_a_tool_grant_publication_refuses_the_wrong_media_type(
    runtime: DbosRuntime,
) -> None:
    refused = client(runtime).post(
        GRANT_PATH, content=THE_GRANT, headers={"content-type": "application/yaml"}
    )

    assert refused.status_code == 415
    assert refused.json()["type"].endswith(":unsupported-media-type")
