"""One call, one library entry -- against the real store and the real route.

The two acts this door joins were two HTTP calls and three transactions. What
these tests watch is the join: that the entry the caller is answered with is the
one the catalog's own read door answers, that a second identical addition writes
nothing, and that a refusal after the bytes were handed over leaves no revision
published under no name.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    catalog_lineage_aliases,
    catalog_lineage_members,
    catalog_lineages,
    published_revisions,
    workflow_revisions,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX, LIBRARY_ADDITIONS_PATH
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
    CatalogLineageId,
    CatalogRetirementState,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from tests.scenarios.api import api_limits, durable_api_client
from tests.scenarios.runtime import exact_output_runtime

ACTOR = "operator"
ACTIVATED_AT = "2026-08-26T00:00:00Z"
NAME = "review-bounded-diff"
DOCUMENT = b"""format_version: 3
name: review-bounded-diff
nodes:
  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Review one bounded diff.
"""
LATER_DOCUMENT = DOCUMENT.replace(
    b"Review one bounded diff.", b"Review one bounded diff, twice."
)
TWO_NODE_DOCUMENT = (
    DOCUMENT
    + b"""  - id: report
    type: agent
    role: reviewer
    mode: headless
    instruction: Say what the review found.
"""
)
"""A recognised, properly named workflow that only a node bound turns away."""
AGENT_DOCUMENT = (
    b"---\n"
    b"name: stage-name-witness\n"
    b"description: Watches the stage and names what it sees.\n"
    b"---\n"
    b"You watch the stage and name what you see.\n"
)


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "library-addition-test"),
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


def add(api: TestClient, document: bytes, file_name: str | None = None) -> Response:
    parameters = {"actor": ACTOR, "activated_at": ACTIVATED_AT}
    if file_name is not None:
        parameters["file_name"] = file_name
    return api.post(
        LIBRARY_ADDITIONS_PATH,
        content=document,
        params=parameters,
        headers={"content-type": "application/octet-stream"},
    )


def catalog_snapshot(runtime: DbosRuntime) -> dict[str, tuple[tuple[object, ...], ...]]:
    tables = (
        workflow_revisions,
        published_revisions,
        catalog_lineages,
        catalog_lineage_members,
        catalog_lineage_aliases,
    )
    with runtime.engine.connect() as connection:
        return {
            table.name: tuple(
                sorted(tuple(row) for row in connection.execute(sa.select(table)))
            )
            for table in tables
        }


def lineage_id_of(document: bytes) -> str:
    revision = PublishedRevision(RevisionKind.WORKFLOW, document)
    return CatalogLineage(revision.kind, revision.revision_hash).lineage_id.value


@pytest.mark.proves("a-workflow-document-becomes-a-named-library-entry-in-one-call")
def test_a_workflow_document_becomes_a_named_entry_the_catalog_answers_for(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)

    answered = add(api, DOCUMENT, "workflows/review.yaml")

    assert answered.status_code == 201, answered.text
    entry = answered.json()
    assert entry == {
        "kind": "workflow",
        "name": NAME,
        "description": None,
        "lineage_id": lineage_id_of(DOCUMENT),
        "workflow_revision_hash": entry["workflow_revision_hash"],
        "revision_number": 1,
    }

    named = api.get(f"{API_PREFIX}/workflow-revisions/by-name/{NAME}")
    assert named.status_code == 200, named.text
    assert named.json()["workflow_revision_hash"] == entry["workflow_revision_hash"]

    read_back = api.get(
        f"{API_PREFIX}/workflow-revisions/{entry['workflow_revision_hash']}"
    )
    assert read_back.status_code == 200, read_back.text


@pytest.mark.proves("the-same-document-added-twice-leaves-the-library-unchanged")
def test_the_same_document_added_twice_leaves_the_library_unchanged(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)
    first = add(api, DOCUMENT)
    assert first.status_code == 201, first.text
    after_first = catalog_snapshot(runtime)

    repeated = add(api, DOCUMENT)

    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == first.json()
    assert catalog_snapshot(runtime) == after_first


@pytest.mark.proves("a-later-revision-joins-the-name-its-own-bytes-author")
def test_a_later_revision_joins_the_lineage_its_authored_name_holds(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)
    founded = add(api, DOCUMENT)
    assert founded.status_code == 201, founded.text

    joined = add(api, LATER_DOCUMENT)

    assert joined.status_code == 201, joined.text
    entry = joined.json()
    assert entry["name"] == NAME
    assert entry["lineage_id"] == founded.json()["lineage_id"]
    assert entry["revision_number"] == 2
    assert entry["workflow_revision_hash"] != founded.json()["workflow_revision_hash"]

    named = api.get(f"{API_PREFIX}/workflow-revisions/by-name/{NAME}")
    assert named.json()["workflow_revision_hash"] == entry["workflow_revision_hash"]


@pytest.mark.proves("a-refused-addition-publishes-nothing")
def test_an_addition_a_retired_name_refuses_leaves_no_revision_behind(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)
    founded = add(api, DOCUMENT)
    assert founded.status_code == 201, founded.text
    DbosCatalogStore(runtime.engine).retire_lineage(
        CatalogLineageId(founded.json()["lineage_id"]),
        CatalogRetirementState.RETIRED,
        CatalogActor(ACTOR),
        CatalogActivatedAt(ACTIVATED_AT),
    )
    before = catalog_snapshot(runtime)

    refused = add(api, LATER_DOCUMENT)

    assert refused.status_code == 410, refused.text
    assert str(refused.json()["type"]).endswith("catalog-lineage-retired")
    assert catalog_snapshot(runtime) == before


@pytest.mark.proves("a-refused-addition-publishes-nothing")
def test_a_workflow_this_build_will_not_store_leaves_every_table_untouched(
    runtime: DbosRuntime,
) -> None:
    """The refusal that gets furthest: recognised, named, and still not storable.

    Every other refusal is decided before the document is a candidate for the
    store at all. This one is turned away by the same bound the publication door
    enforces, after the kind and the name were already read, so it is where a
    door that published first and admitted second would leave the revision
    behind.
    """
    api = durable_api_client(runtime, api_limits(maximum_workflow_nodes=1))
    before = catalog_snapshot(runtime)

    refused = add(api, TWO_NODE_DOCUMENT)

    assert refused.status_code == 422, refused.text
    assert str(refused.json()["type"]).endswith("invalid-workflow-document")
    assert catalog_snapshot(runtime) == before


@pytest.mark.proves("an-authored-agent-file-becomes-a-library-entry-in-the-same-call")
def test_an_authored_agent_file_becomes_a_library_entry(runtime: DbosRuntime) -> None:
    api = durable_api_client(runtime)

    answered = add(api, AGENT_DOCUMENT, "agents/stage-name-witness.md")

    assert answered.status_code == 201, answered.text
    entry = answered.json()
    assert entry == {
        "kind": "agent_definition",
        "name": "stage-name-witness",
        "description": "Watches the stage and names what it sees.",
        "provider_id": "anthropic",
        "agent_definition_revision_hash": entry["agent_definition_revision_hash"],
    }

    listed = api.get(f"{API_PREFIX}/agent-definition-revisions")
    assert listed.status_code == 200, listed.text
    assert [item["name"] for item in listed.json()["items"]] == ["stage-name-witness"]

    repeated = add(api, AGENT_DOCUMENT, "agents/stage-name-witness.md")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == entry
