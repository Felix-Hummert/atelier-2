"""The HTTP door the catalog store already had: a schema revision, bytes in, hash out.

A chain workflow pins every declared output to a published schema. Until this
door the operator had to stop the unit and call publish_revision in-process.
These tests drive the real route against the real store.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.openapi import API_PREFIX
from atelier2.application.evaluate_executability import (
    ExecutableDocument,
    resolve_document_references,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_configuration_v3 import ReferenceSite, ResolvedReference
from atelier2.contracts.schemas_v3 import SchemaDocumentRefusal
from atelier2.contracts.workflows_v3 import VersionedReference, WorkflowGraphV3
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
)
from tests.scenarios.api import durable_api_client
from tests.scenarios.runtime import exact_output_runtime

SCHEMA_PATH = f"{API_PREFIX}/schema-revisions"
WORKFLOW_PATH = f"{API_PREFIX}/workflow-revisions"
MEAL_SCHEMA = json.dumps(
    {"title": "the meal a cook produced", "type": "object"}
).encode()


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "schema-door-test"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def client(runtime: DbosRuntime) -> TestClient:
    return durable_api_client(runtime)


def chain_document(schema_revision: str) -> bytes:
    """Two agents, the second reading the first: the live chain that needs a schema."""

    return f"""format_version: 3
name: Cook then serve
nodes:
  - id: cook
    type: agent
    role: cook
    mode: headless
    instruction: Cook the meal this chain is for.
    outputs:
      - name: meal
        schema: {{ref: meal-schema, revision: {schema_revision}}}
  - id: serve
    type: agent
    role: waiter
    mode: headless
    instruction: Serve the meal the cook produced.
    depends_on: [cook]
    inputs:
      - name: meal
        from: {{node: cook, output: meal}}
""".encode()


@pytest.mark.proves("a-schema-published-over-the-api-is-the-hash-a-chain-names")
def test_a_schema_published_over_http_lets_a_chain_workflow_name_that_hash(
    runtime: DbosRuntime,
) -> None:
    """The live hole: POST the schema, then POST the chain that pins it."""

    api = client(runtime)

    created = api.post(
        SCHEMA_PATH, content=MEAL_SCHEMA, headers={"content-type": "application/json"}
    )
    retried = api.post(
        SCHEMA_PATH, content=MEAL_SCHEMA, headers={"content-type": "application/json"}
    )

    assert (created.status_code, retried.status_code) == (201, 200)
    assert created.json() == retried.json()
    schema_hash = created.json()["schema_revision_hash"]
    assert schema_hash == PublishedRevisionHash.of(MEAL_SCHEMA).value
    resolved = DbosCatalogStore(runtime.engine).resolve(
        RevisionKind.SCHEMA, PublishedRevisionHash(schema_hash)
    )
    assert isinstance(resolved, PublishedRevisionFound)
    assert resolved.revision.document == MEAL_SCHEMA

    published = api.post(
        WORKFLOW_PATH,
        content=chain_document(schema_hash),
        headers={"content-type": "application/yaml"},
    )

    assert published.status_code == 201, published.text
    graph = parse_workflow_document(chain_document(schema_hash))
    assert isinstance(graph, WorkflowGraphV3)
    snapshot = resolve_document_references(graph, DbosCatalogStore(runtime.engine))
    assert isinstance(snapshot, ExecutableDocument), snapshot
    assert snapshot.resolutions == (
        ResolvedReference(
            ReferenceSite("outputs.schema", "cook", "meal"),
            RevisionKind.SCHEMA,
            VersionedReference(ref="meal-schema", revision=schema_hash),
            PublishedRevisionHash(schema_hash),
        ),
    )


@pytest.mark.parametrize(
    ("document", "code"),
    (
        (b"Guten Morgen", "schema-document-not-json"),
        (b"\xff\xfe", "schema-document-not-utf8"),
        (b'{"type": 17}', "schema-not-a-schema"),
        (
            b'{"$ref": "https://example.com/verdict.json"}',
            "schema-nonlocal-reference",
        ),
    ),
)
def test_a_schema_the_profile_refuses_is_named_by_its_own_reason(
    runtime: DbosRuntime, document: bytes, code: str
) -> None:
    refused = client(runtime).post(
        SCHEMA_PATH, content=document, headers={"content-type": "application/json"}
    )

    assert refused.status_code == 422
    assert refused.json()["type"].endswith(":" + code)
    assert (
        SchemaDocumentRefusal(code.removeprefix("schema-")).value
        in refused.json()["detail"]
    )
    missing = DbosCatalogStore(runtime.engine).resolve(
        RevisionKind.SCHEMA, PublishedRevisionHash.of(document)
    )
    assert isinstance(missing, PublishedRevisionMissing)


def test_a_schema_publication_refuses_the_wrong_media_type(
    runtime: DbosRuntime,
) -> None:
    refused = client(runtime).post(
        SCHEMA_PATH, content=MEAL_SCHEMA, headers={"content-type": "application/yaml"}
    )

    assert refused.status_code == 415
    assert refused.json()["type"].endswith(":unsupported-media-type")


def test_a_published_schema_is_read_back_byte_identical_over_get(
    runtime: DbosRuntime,
) -> None:
    """A caller holding only the hash (#678) reads back what the publisher wrote.

    Before this route the wire's own comment on `orders.schema` said as much: a
    caller that wants the bytes already holds the published revision -- a
    promise about the publisher, never about a browser. This is the door that
    makes it true for one too.
    """
    api = client(runtime)
    published = api.post(
        SCHEMA_PATH, content=MEAL_SCHEMA, headers={"content-type": "application/json"}
    )
    schema_hash = published.json()["schema_revision_hash"]

    read = api.get(f"{SCHEMA_PATH}/{schema_hash}")

    assert read.status_code == 200
    assert read.headers["content-type"] == "application/json"
    assert read.content == MEAL_SCHEMA


def test_reading_an_unpublished_schema_hash_answers_not_found(
    runtime: DbosRuntime,
) -> None:
    never_published = PublishedRevisionHash.of(b"nobody published this").value

    read = client(runtime).get(f"{SCHEMA_PATH}/{never_published}")

    assert read.status_code == 404
    assert read.json()["type"].endswith(":schema-revision-not-found")


def test_reading_a_malformed_hash_answers_invalid_revision_hash(
    runtime: DbosRuntime,
) -> None:
    read = client(runtime).get(f"{SCHEMA_PATH}/not-a-sha-256-hash")

    assert read.status_code == 400
    assert read.json()["type"].endswith(":invalid-revision-hash")
