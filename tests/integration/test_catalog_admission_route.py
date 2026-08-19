"""The write door beside the read one: authored names enter through the API.

Until this head `found_lineage` and `admit_member` had no production caller, so
`GET /workflow-revisions/by-name/{name}` answered over a catalog that nothing in
production could fill. These tests drive the real routes against the real store.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    catalog_lineage_aliases,
    catalog_lineage_members,
    catalog_lineage_retirements,
    catalog_lineages,
)
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
SECOND_NAME = "review-bounded-diff-again"
RENAMED_DOCUMENT = SECOND_DOCUMENT.replace(NAME.encode(), SECOND_NAME.encode())
V1_DOCUMENT = b"""format_version: 1
start: calculate
nodes:
  - id: calculate
    type: subworkflow
    operation: add
    operands: [1, 2]
    next:
"""
SECOND_V1_DOCUMENT = V1_DOCUMENT.replace(b"operands: [1, 2]", b"operands: [2, 3]")
V2_DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: implement, next: done}
"""
LINEAGES = f"{API_PREFIX}/workflow-lineages"
REVISIONS = f"{API_PREFIX}/workflow-revisions"


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


def founding(revision: PublishedRevision, name: str | None = None) -> dict[str, str]:
    request = {
        "workflow_revision_hash": revision.revision_hash.value,
        "actor": "operator",
        "activated_at": "2026-08-17T00:00:00Z",
    }
    if name is not None:
        request["display_name"] = name
    return request


def published_over_http(api: TestClient, document: bytes = DOCUMENT) -> str:
    """The operator door: YAML in, hash out. Not the catalog's second table."""

    response = api.post(
        REVISIONS,
        content=document,
        headers={"content-type": "application/yaml"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["workflow_revision_hash"])


def catalog_snapshot(runtime: DbosRuntime) -> dict[str, tuple[tuple[object, ...], ...]]:
    tables = (
        catalog_lineages,
        catalog_lineage_members,
        catalog_lineage_aliases,
        catalog_lineage_retirements,
    )
    with runtime.engine.connect() as connection:
        return {
            table.name: tuple(
                sorted(tuple(row) for row in connection.execute(sa.select(table)))
            )
            for table in tables
        }


@pytest.mark.proves("a-workflow-published-over-the-api-is-named-over-the-api")
def test_a_workflow_published_over_the_api_is_named_over_the_api(
    runtime: DbosRuntime,
) -> None:
    """The live hole: POST /workflow-revisions then POST /workflow-lineages."""

    api = client(runtime)
    revision_hash = published_over_http(api)
    request = {
        "workflow_revision_hash": revision_hash,
        "actor": "operator",
        "activated_at": "2026-08-17T00:00:00Z",
    }

    response = api.post(LINEAGES, json=request)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["display_name"] == NAME
    assert body["workflow_revision_hash"] == revision_hash
    assert body["revision_number"] == 1

    repeated = api.post(LINEAGES, json=request)
    assert repeated.status_code == 201, repeated.text
    assert repeated.json() == body


@pytest.mark.proves("a-published-revision-becomes-a-named-lineage-over-the-api")
def test_a_published_revision_is_named_over_the_api(runtime: DbosRuntime) -> None:
    revision = published(runtime)

    response = client(runtime).post(LINEAGES, json=founding(revision))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["display_name"] == NAME
    assert body["workflow_revision_hash"] == revision.revision_hash.value
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
    assert answered.json()["workflow_revision_hash"] == revision.revision_hash.value


@pytest.mark.proves("a-later-revision-joins-the-lineage-that-already-holds-its-name")
def test_a_later_http_published_revision_joins_the_named_lineage(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)
    first_hash = published_over_http(api)
    founded = api.post(
        LINEAGES,
        json={
            "workflow_revision_hash": first_hash,
            "actor": "operator",
            "activated_at": "2026-08-17T00:00:00Z",
        },
    )
    assert founded.status_code == 201, founded.text
    second_hash = published_over_http(api, RENAMED_DOCUMENT)

    response = api.post(
        f"{LINEAGES}/{founded.json()['lineage_id']}/members",
        json={
            "workflow_revision_hash": second_hash,
            "actor": "operator",
            "activated_at": "2026-08-17T00:01:00Z",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["revision_number"] == 2
    assert body["display_name"] == SECOND_NAME
    assert body["workflow_revision_hash"] == second_hash


@pytest.mark.proves("a-later-revision-joins-the-lineage-that-already-holds-its-name")
def test_a_later_revision_appends_its_authored_name_and_both_names_survive_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    external_path = tmp_path / "external.sqlite"
    runtime = exact_output_runtime(
        DbosRuntimeSettings(database_path, "admission-route-test"),
        LoopbackEffectAdapterFactory(
            external_path,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    try:
        runtime.initialize_storage()
        first = published(runtime)
        second = published(runtime, RENAMED_DOCUMENT)
        api = client(runtime)
        founded = api.post(LINEAGES, json=founding(first))
        assert founded.status_code == 201, founded.text
        lineage_id = founded.json()["lineage_id"]

        response = api.post(
            f"{LINEAGES}/{lineage_id}/members",
            json={
                "workflow_revision_hash": second.revision_hash.value,
                "actor": "operator",
                "activated_at": "2026-08-17T00:01:00Z",
            },
        )

        assert response.status_code == 201, response.text
        body = response.json()
        assert body["revision_number"] == 2
        assert body["display_name"] == SECOND_NAME
        assert body["workflow_revision_hash"] == second.revision_hash.value
    finally:
        runtime.close()

    restarted = exact_output_runtime(
        DbosRuntimeSettings(database_path, "admission-route-test"),
        LoopbackEffectAdapterFactory(
            external_path,
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    restarted.initialize_storage()
    try:
        restarted_api = client(restarted)
        for name in (NAME, SECOND_NAME):
            answered = restarted_api.get(
                f"{API_PREFIX}/workflow-revisions/by-name/{name}"
            )
            assert answered.status_code == 200, answered.text
            assert answered.json() == {
                "display_name": SECOND_NAME,
                "lineage_id": lineage_id,
                "workflow_revision_hash": second.revision_hash.value,
                "revision_number": 2,
            }
        assert (
            restarted_api.get(
                f"{API_PREFIX}/workflow-revisions/by-name/caller-other"
            ).status_code
            == 404
        )
    finally:
        restarted.close()


@pytest.mark.parametrize("caller_name", [NAME, "caller-other"])
def test_a_v3_name_cannot_be_restated_by_the_api(
    runtime: DbosRuntime, caller_name: str
) -> None:
    revision = published(runtime)
    before = catalog_snapshot(runtime)

    response = client(runtime).post(LINEAGES, json=founding(revision, caller_name))

    assert response.status_code == 422
    assert response.json()["type"].endswith("invalid-request")
    assert catalog_snapshot(runtime) == before


@pytest.mark.proves(
    "a-v3-workflow-with-a-64-hex-authored-name-is-refused-before-any-catalog-write"
)
@pytest.mark.parametrize("authored_name", ["a" * 64, "Invalid Name"])
def test_an_invalid_authored_v3_name_writes_no_catalog_row(
    runtime: DbosRuntime, authored_name: str
) -> None:
    revision = published(
        runtime, DOCUMENT.replace(NAME.encode(), authored_name.encode())
    )
    before = catalog_snapshot(runtime)

    response = client(runtime).post(LINEAGES, json=founding(revision))

    assert response.status_code == 422
    assert response.json()["type"].endswith("invalid-request")
    assert catalog_snapshot(runtime) == before


@pytest.mark.parametrize("explicit_name", ["a" * 64, "Invalid Name"])
@pytest.mark.parametrize("document", [V1_DOCUMENT, V2_DOCUMENT], ids=["v1", "v2"])
def test_an_invalid_explicit_legacy_name_writes_no_catalog_row(
    runtime: DbosRuntime, explicit_name: str, document: bytes
) -> None:
    revision = published(runtime, document)
    before = catalog_snapshot(runtime)

    response = client(runtime).post(LINEAGES, json=founding(revision, explicit_name))

    assert response.status_code == 422
    assert response.json()["type"].endswith("invalid-request")
    assert catalog_snapshot(runtime) == before


@pytest.mark.parametrize("document", [V1_DOCUMENT, V2_DOCUMENT], ids=["v1", "v2"])
def test_a_legacy_format_requires_and_accepts_one_valid_explicit_name(
    runtime: DbosRuntime, document: bytes
) -> None:
    missing_name = published(runtime, document)
    before = catalog_snapshot(runtime)

    refused = client(runtime).post(LINEAGES, json=founding(missing_name))

    assert refused.status_code == 422
    assert refused.json()["type"].endswith("invalid-request")
    assert catalog_snapshot(runtime) == before

    accepted = client(runtime).post(LINEAGES, json=founding(missing_name, NAME))
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["display_name"] == NAME


def test_an_impossible_founding_time_writes_no_catalog_row(
    runtime: DbosRuntime,
) -> None:
    revision = published(runtime, V1_DOCUMENT)
    before = catalog_snapshot(runtime)
    request = founding(revision, NAME)
    request["activated_at"] = "2026-13-17T00:00:00Z"

    response = client(runtime).post(LINEAGES, json=request)

    assert response.status_code == 422
    assert response.json()["type"].endswith("invalid-request")
    assert catalog_snapshot(runtime) == before


def test_an_impossible_admission_time_changes_no_catalog_row(
    runtime: DbosRuntime,
) -> None:
    first = published(runtime, V1_DOCUMENT)
    second = published(runtime, SECOND_V1_DOCUMENT)
    api = client(runtime)
    lineage_id = api.post(LINEAGES, json=founding(first, NAME)).json()["lineage_id"]
    before = catalog_snapshot(runtime)

    response = api.post(
        f"{LINEAGES}/{lineage_id}/members",
        json={
            "workflow_revision_hash": second.revision_hash.value,
            "actor": "operator",
            "activated_at": "2026-13-17T00:01:00Z",
        },
    )

    assert response.status_code == 422
    assert response.json()["type"].endswith("invalid-request")
    assert catalog_snapshot(runtime) == before


@pytest.mark.proves("an-admission-the-catalog-refuses-is-named-by-its-own-reason")
def test_a_revision_nobody_published_is_refused_by_name(runtime: DbosRuntime) -> None:
    unpublished = PublishedRevision(RevisionKind.WORKFLOW, SECOND_DOCUMENT)

    response = client(runtime).post(LINEAGES, json=founding(unpublished))

    assert response.status_code == 409
    problem = response.json()
    assert problem["type"].endswith("catalog-revision-unpublished")
    assert problem["detail"] == (
        "Publish the revision through POST /atelier/api/v1/workflow-revisions "
        "before giving it a name."
    )


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
    revision = published(runtime, V1_DOCUMENT)
    api = client(runtime)
    api.post(LINEAGES, json=founding(revision, NAME))

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
            "workflow_revision_hash": revision.revision_hash.value,
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
            "workflow_revision_hash": second.revision_hash.value,
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
