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
    CatalogLineageDisplayName,
    CatalogRetirementState,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.ports.published_revisions import (
    CatalogLineageFounded,
    CatalogNameFound,
    PublishedRevisionCreated,
)
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


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "catalog-route-test"),
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


def found(runtime: DbosRuntime, document: bytes = DOCUMENT) -> PublishedRevision:
    """One named lineage in the catalog, published the way the product does."""
    store = DbosCatalogStore(runtime.engine)
    revision = PublishedRevision(RevisionKind.WORKFLOW, document)
    assert isinstance(store.publish_revision(revision), PublishedRevisionCreated)
    founded = store.found_lineage(
        revision,
        CatalogLineageDisplayName(NAME),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-17T00:00:00Z"),
    )
    assert isinstance(founded, CatalogLineageFounded)
    return revision


def client(runtime: DbosRuntime) -> TestClient:
    return durable_api_client(runtime)


@pytest.mark.proves("a-name-is-answerable-over-the-api")
def test_a_name_answers_with_the_revision_it_resolves_to(
    runtime: DbosRuntime,
) -> None:
    revision = found(runtime)

    response = client(runtime).get(f"{API_PREFIX}/workflow-revisions/by-name/{NAME}")

    assert response.status_code == 200
    body = response.json()
    assert body["workflow_revision_hash"] == revision.revision_hash.value
    assert body["display_name"] == NAME
    assert body["revision_number"] == 1


@pytest.mark.proves("the-name-path-is-never-read-as-a-revision-hash")
def test_the_name_path_is_not_read_as_a_revision_hash(runtime: DbosRuntime) -> None:
    # `by-name` sits under the same prefix as `{revision_hash}`; if the hash
    # route claims it first, every name is refused as a malformed hash.
    found(runtime)

    response = client(runtime).get(f"{API_PREFIX}/workflow-revisions/by-name/{NAME}")

    assert response.status_code == 200


@pytest.mark.proves("a-name-the-catalog-cannot-honour-is-refused-by-its-own-reason")
def test_a_name_nobody_admitted_is_a_named_problem(runtime: DbosRuntime) -> None:
    found(runtime)

    response = client(runtime).get(
        f"{API_PREFIX}/workflow-revisions/by-name/no-such-workflow"
    )

    assert response.status_code == 404
    assert response.json()["type"].endswith("catalog-name-not-found")


@pytest.mark.proves("a-name-the-catalog-cannot-honour-is-refused-by-its-own-reason")
def test_a_retired_lineage_refuses_by_name_instead_of_answering(
    runtime: DbosRuntime,
) -> None:
    revision = found(runtime)
    store = DbosCatalogStore(runtime.engine)
    found_name = store.resolve_name(
        RevisionKind.WORKFLOW, CatalogLineageDisplayName(NAME), "head"
    )
    assert isinstance(found_name, CatalogNameFound)
    lineage_id = found_name.lineage_id
    store.retire_lineage(
        lineage_id,
        CatalogRetirementState.RETIRED,
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-17T00:01:00Z"),
    )

    response = client(runtime).get(f"{API_PREFIX}/workflow-revisions/by-name/{NAME}")

    assert response.status_code == 410
    assert response.json()["type"].endswith("catalog-lineage-retired")
    # A retirement discloses neither the bytes it once held nor which lineage
    # held them: the refusal is the whole answer.
    assert revision.revision_hash.value not in response.text
    assert lineage_id.value not in response.text


@pytest.mark.proves("a-name-is-answerable-over-the-api")
def test_a_position_answers_the_member_the_caller_asked_for(
    runtime: DbosRuntime,
) -> None:
    first = found(runtime)
    store = DbosCatalogStore(runtime.engine)
    second = PublishedRevision(RevisionKind.WORKFLOW, SECOND_DOCUMENT)
    store.publish_revision(second)
    found_name = store.resolve_name(
        RevisionKind.WORKFLOW, CatalogLineageDisplayName(NAME), "head"
    )
    assert isinstance(found_name, CatalogNameFound)
    lineage_id = found_name.lineage_id
    store.admit_member(
        lineage_id,
        second,
        CatalogLineageDisplayName(NAME),
        CatalogActor("operator"),
        CatalogActivatedAt("2026-08-17T00:02:00Z"),
    )
    api = client(runtime)

    head = api.get(f"{API_PREFIX}/workflow-revisions/by-name/{NAME}")
    first_member = api.get(f"{API_PREFIX}/workflow-revisions/by-name/{NAME}?position=1")

    assert head.json()["workflow_revision_hash"] == second.revision_hash.value
    assert first_member.json()["workflow_revision_hash"] == first.revision_hash.value


@pytest.mark.proves("a-name-the-catalog-cannot-honour-is-refused-by-its-own-reason")
def test_a_position_that_is_not_a_member_is_a_named_problem(
    runtime: DbosRuntime,
) -> None:
    found(runtime)

    response = client(runtime).get(
        f"{API_PREFIX}/workflow-revisions/by-name/{NAME}?position=7"
    )

    assert response.status_code == 404
    assert response.json()["type"].endswith("catalog-name-not-found")


@pytest.mark.proves("a-name-the-catalog-cannot-honour-is-refused-by-its-own-reason")
def test_a_position_that_is_neither_a_number_nor_head_is_refused(
    runtime: DbosRuntime,
) -> None:
    found(runtime)

    response = client(runtime).get(
        f"{API_PREFIX}/workflow-revisions/by-name/{NAME}?position=later"
    )

    assert response.status_code == 400
    assert response.json()["type"].endswith("invalid-catalog-position")
