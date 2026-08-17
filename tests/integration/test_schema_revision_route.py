"""The schema publication door: bytes in, hash out, over HTTP.

Until this head a live chain had to stop the unit and call
`DbosCatalogStore.publish_revision(PublishedRevision(SCHEMA, …))` in-process,
because OpenAPI had no schema path. These tests drive the real route against
the real catalog store.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import published_revisions
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.schemas_v3 import SchemaDocumentRefusal
from tests.scenarios.api import durable_api_client
from tests.scenarios.runtime import exact_output_runtime

SCHEMAS = f"{API_PREFIX}/schema-revisions"
JSON_SCHEMA = b'{"type": "object", "required": ["verdict"]}'
YAML_TYPED_JSON_SCHEMA = b'{"type":"object","required":["verdict"]}'
INVALID_SCHEMA = b"Guten Morgen"


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "schema-revision-route-test"),
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


def schema_row_count(runtime: DbosRuntime) -> int:
    with runtime.engine.connect() as connection:
        return int(
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(published_revisions)
                .where(published_revisions.c.kind == RevisionKind.SCHEMA.value)
            )
            or 0
        )


@pytest.mark.proves("a-schema-is-published-over-the-api")
@pytest.mark.parametrize(
    "media_type, document",
    [
        ("application/json", JSON_SCHEMA),
        ("application/yaml", YAML_TYPED_JSON_SCHEMA),
    ],
    ids=["json", "yaml"],
)
def test_a_schema_posted_over_the_api_is_published_under_its_hash(
    runtime: DbosRuntime, media_type: str, document: bytes
) -> None:
    """The live hole: POST /schema-revisions, bytes in, hash out."""

    response = client(runtime).post(
        SCHEMAS, content=document, headers={"content-type": media_type}
    )

    assert response.status_code == 201, response.text
    assert response.json() == {
        "revision_hash": PublishedRevisionHash.of(document).value
    }
    assert schema_row_count(runtime) == 1


@pytest.mark.proves("a-schema-is-published-over-the-api")
def test_an_identical_schema_post_is_the_same_answer_and_writes_nothing(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)
    created = api.post(
        SCHEMAS, content=JSON_SCHEMA, headers={"content-type": "application/json"}
    )
    assert created.status_code == 201, created.text

    repeated = api.post(
        SCHEMAS, content=JSON_SCHEMA, headers={"content-type": "application/json"}
    )

    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == created.json()
    assert schema_row_count(runtime) == 1


@pytest.mark.proves("a-schema-is-published-over-the-api")
def test_an_invalid_schema_is_refused_by_name_and_writes_nothing(
    runtime: DbosRuntime,
) -> None:
    response = client(runtime).post(
        SCHEMAS,
        content=INVALID_SCHEMA,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422, response.text
    body = response.json()
    assert body["type"].endswith("invalid-schema-document")
    assert SchemaDocumentRefusal.DOCUMENT_NOT_JSON.value in body["detail"]
    assert schema_row_count(runtime) == 0
