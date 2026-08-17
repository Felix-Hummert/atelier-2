"""The write door beside the read one: a name enters the catalog over the API.

Until this head `found_lineage` and `admit_member` had no production caller, so
`GET /workflow-revisions/by-name/{name}` answered over a catalog that nothing in
production could fill. These tests drive the real routes against the real store.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineage,
    CatalogLineageDisplayName,
    CatalogRetirementState,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.ports.published_revisions import PublishedRevisionCreated
from tests.scenarios.api import durable_api_client
from tests.scenarios.runtime import exact_output_runtime

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
SECOND_DOCUMENT = DOCUMENT.replace(b"Review one bounded diff.", b"Review it again.")
LINEAGES = f"{API_PREFIX}/workflow-lineages"


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "admission-route-test"),
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


def published(runtime: DbosRuntime, document: bytes = DOCUMENT) -> PublishedRevision:
    """One revision the operator has published and not yet named."""

    revision = PublishedRevision(RevisionKind.WORKFLOW, document)
    store = DbosCatalogStore(runtime.engine)
    assert isinstance(store.publish_revision(revision), PublishedRevisionCreated)
    return revision


def client(runtime: DbosRuntime) -> TestClient:
    return durable_api_client(runtime)


def founding(revision: PublishedRevision, name: str = NAME) -> dict[str, str]:
    return {
        "revision_hash": revision.revision_hash.value,
        "display_name": name,
        "actor": "operator",
        "activated_at": "2026-08-17T00:00:00Z",
    }


@pytest.mark.proves("a-published-revision-becomes-a-named-lineage-over-the-api")
def test_a_published_revision_is_named_over_the_api(runtime: DbosRuntime) -> None:
    revision = published(runtime)

    response = client(runtime).post(LINEAGES, json=founding(revision))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["display_name"] == NAME
    assert body["revision_hash"] == revision.revision_hash.value
    assert body["revision_number"] == 1
    assert (
        body["lineage_id"]
        == CatalogLineage(revision.kind, revision.revision_hash).lineage_id.value
    )


@pytest.mark.proves("a-published-revision-becomes-a-named-lineage-over-the-api")
def test_the_name_the_api_founded_answers_the_read_door(runtime: DbosRuntime) -> None:
    """The whole point: the catalog fills, and the door #200 opened answers."""

    revision = published(runtime)
    api = client(runtime)

    api.post(LINEAGES, json=founding(revision))
    answered = api.get(f"{API_PREFIX}/workflow-revisions/by-name/{NAME}")

    assert answered.status_code == 200, answered.text
    assert answered.json()["revision_hash"] == revision.revision_hash.value


@pytest.mark.proves("a-later-revision-joins-the-lineage-that-already-holds-its-name")
def test_a_later_revision_is_admitted_into_the_lineage_that_holds_its_name(
    runtime: DbosRuntime,
) -> None:
    first = published(runtime)
    second = published(runtime, SECOND_DOCUMENT)
    api = client(runtime)
    lineage_id = api.post(LINEAGES, json=founding(first)).json()["lineage_id"]

    response = api.post(
        f"{LINEAGES}/{lineage_id}/members",
        json={
            "revision_hash": second.revision_hash.value,
            "actor": "operator",
            "activated_at": "2026-08-17T00:01:00Z",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["revision_number"] == 2
    assert body["display_name"] == NAME
    assert body["revision_hash"] == second.revision_hash.value
    assert (
        api.get(f"{API_PREFIX}/workflow-revisions/by-name/{NAME}").json()[
            "revision_hash"
        ]
        == second.revision_hash.value
    )


@pytest.mark.proves("an-admission-the-catalog-refuses-is-named-by-its-own-reason")
def test_a_revision_nobody_published_is_refused_by_name(runtime: DbosRuntime) -> None:
    unpublished = PublishedRevision(RevisionKind.WORKFLOW, SECOND_DOCUMENT)

    response = client(runtime).post(LINEAGES, json=founding(unpublished))

    assert response.status_code == 409
    assert response.json()["type"].endswith("catalog-revision-unpublished")


@pytest.mark.proves("an-admission-the-catalog-refuses-is-named-by-its-own-reason")
def test_a_name_another_lineage_holds_is_refused_by_name(
    runtime: DbosRuntime,
) -> None:
    first = published(runtime)
    second = published(runtime, SECOND_DOCUMENT)
    api = client(runtime)
    api.post(LINEAGES, json=founding(first))

    response = api.post(LINEAGES, json=founding(second))

    assert response.status_code == 409
    assert response.json()["type"].endswith("catalog-name-held")


@pytest.mark.proves("an-admission-the-catalog-refuses-is-named-by-its-own-reason")
def test_a_revision_another_lineage_owns_is_refused_by_name(
    runtime: DbosRuntime,
) -> None:
    revision = published(runtime)
    api = client(runtime)
    api.post(LINEAGES, json=founding(revision))

    response = api.post(LINEAGES, json=founding(revision, "another-name"))

    assert response.status_code == 409
    assert response.json()["type"].endswith("catalog-revision-owned")


@pytest.mark.proves("an-admission-the-catalog-refuses-is-named-by-its-own-reason")
def test_admission_into_a_lineage_that_does_not_exist_is_refused_by_name(
    runtime: DbosRuntime,
) -> None:
    revision = published(runtime)

    response = client(runtime).post(
        f"{LINEAGES}/{'a' * 64}/members",
        json={
            "revision_hash": revision.revision_hash.value,
            "actor": "operator",
            "activated_at": "2026-08-17T00:01:00Z",
        },
    )

    assert response.status_code == 404
    assert response.json()["type"].endswith("catalog-lineage-missing")


@pytest.mark.proves("an-admission-the-catalog-refuses-is-named-by-its-own-reason")
def test_admission_into_a_retired_lineage_is_refused_by_name(
    runtime: DbosRuntime,
) -> None:
    first = published(runtime)
    second = published(runtime, SECOND_DOCUMENT)
    api = client(runtime)
    lineage_id = api.post(LINEAGES, json=founding(first)).json()["lineage_id"]
    DbosCatalogStore(runtime.engine).retire_lineage(
        CatalogLineage(first.kind, first.revision_hash).lineage_id,
        CatalogRetirementState.RETIRED,
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-17T00:02:00Z"),
    )

    response = api.post(
        f"{LINEAGES}/{lineage_id}/members",
        json={
            "revision_hash": second.revision_hash.value,
            "actor": "operator",
            "activated_at": "2026-08-17T00:03:00Z",
        },
    )

    # 410, because the reason is the one #200 already named for a retired
    # lineage: one reason, one problem code.
    assert response.status_code == 410
    assert response.json()["type"].endswith("catalog-lineage-retired")
    assert (
        DbosCatalogStore(runtime.engine).resolve_name(
            RevisionKind.WORKFLOW, CatalogLineageDisplayName(NAME), "head"
        )
        is not None
    )
