"""The operator's import door, driven through the composed server over a real store.

The acceptance of #652: after one import every open issue of the connected
repository is exactly one OBSERVED row, listable; a repeated import adds
nothing; and admitting an observed item through the existing admission door
works unchanged. GitHub is the same `httpx.MockTransport` stand-in the
observation source's own tests use -- no test reaches the real network.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.github.composition import live_github_issue_source
from atelier2.adapters.github.observation import LiveGitHubIssueSource
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import (
    OBSERVED_QUEUE_ITEMS_PATH,
    PROJECT_SOURCE_IMPORT_PATH,
    QUEUE_ADMISSIONS_PATH,
    QUEUE_ITEMS_PATH,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.host_configuration import (
    ConnectionActor,
    ProjectId,
    ProjectSourceConnectionRevision,
    SourceAddress,
    SourceConnectionAuthMethod,
    SourceKind,
)
from tests.integration.test_queue_admission_route import founded_workflow_lineage
from tests.scenarios.api import durable_api_client
from tests.scenarios.runtime import exact_output_runtime

PROJECT = ProjectId("studio")
OWNER = "atelier2-operator"
REPO = "atelier2-target"


class _FakeGitHubIssueListing:
    def __init__(self, issues: list[dict[str, Any]], status: int = 200) -> None:
        self.issues = issues
        self.status = status

    def handle(self, request: httpx.Request) -> httpx.Response:
        if self.status != 200:
            return httpx.Response(self.status, json={"message": "refused"})
        per_page = int(request.url.params.get("per_page", "30"))
        page = int(request.url.params.get("page", "1"))
        start = (page - 1) * per_page
        return httpx.Response(200, json=self.issues[start : start + per_page])


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "queue-import-route-test"),
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


def issue_source(
    tmp_path: Path, listing: _FakeGitHubIssueListing
) -> LiveGitHubIssueSource:
    """The source exactly as serve composes it: from the connection record."""

    credential_directory = tmp_path / "github-credential"
    credential_directory.mkdir(exist_ok=True)
    (credential_directory / "token").write_text("gho_test_token", encoding="utf-8")
    composed = live_github_issue_source(
        ProjectSourceConnectionRevision(
            PROJECT,
            1,
            SourceKind("github"),
            SourceAddress(f"{OWNER}/{REPO}@main"),
            credential_directory,
            SourceConnectionAuthMethod.PERSONAL_ACCESS_TOKEN,
            ConnectionActor("felix"),
        )
    )
    return replace(composed, transport=httpx.MockTransport(listing.handle))


def connected_api(
    runtime: DbosRuntime, tmp_path: Path, listing: _FakeGitHubIssueListing
) -> TestClient:
    return durable_api_client(
        runtime,
        served_project_id=PROJECT,
        tracker_item_source=issue_source(tmp_path, listing),
    )


def test_importing_turns_every_open_issue_into_exactly_one_listable_observed_row(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    api = connected_api(
        runtime, tmp_path, _FakeGitHubIssueListing([{"number": 79}, {"number": 652}])
    )

    imported = api.post(PROJECT_SOURCE_IMPORT_PATH)

    assert imported.status_code == 200, imported.text
    assert imported.json() == {"observed": 2, "newly_observed": 2}
    listed = api.get(OBSERVED_QUEUE_ITEMS_PATH)
    assert listed.status_code == 200, listed.text
    page = listed.json()
    assert page["next_after"] is None
    assert {item["tracker_item_reference"] for item in page["items"]} == {
        "gh:79",
        "gh:652",
    }
    assert all(
        item["project_id"] == "studio"
        and item["revision"] == 0
        and len(item["item_id"]) == 64
        for item in page["items"]
    )


def test_a_repeated_import_adds_nothing(runtime: DbosRuntime, tmp_path: Path) -> None:
    api = connected_api(
        runtime, tmp_path, _FakeGitHubIssueListing([{"number": 79}, {"number": 652}])
    )
    assert api.post(PROJECT_SOURCE_IMPORT_PATH).status_code == 200
    first_list = api.get(OBSERVED_QUEUE_ITEMS_PATH).json()

    repeated = api.post(PROJECT_SOURCE_IMPORT_PATH)

    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {"observed": 2, "newly_observed": 0}
    assert api.get(OBSERVED_QUEUE_ITEMS_PATH).json() == first_list


def test_an_imported_item_admits_through_the_existing_door_unchanged(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    api = connected_api(runtime, tmp_path, _FakeGitHubIssueListing([{"number": 79}]))
    assert api.post(PROJECT_SOURCE_IMPORT_PATH).status_code == 200
    (observed,) = api.get(OBSERVED_QUEUE_ITEMS_PATH).json()["items"]
    lineage_id = founded_workflow_lineage(api)

    admitted = api.post(
        QUEUE_ADMISSIONS_PATH,
        json={
            "project_id": observed["project_id"],
            "tracker_item_reference": observed["tracker_item_reference"],
            "workflow_lineage_id": lineage_id,
            "rationale": "matches the triage rule",
            "expected_revision": observed["revision"],
        },
    )

    assert admitted.status_code == 201, admitted.text
    assert admitted.json()["item_id"] == observed["item_id"]
    # The item moved out of the observed list and into the admitted one.
    assert api.get(OBSERVED_QUEUE_ITEMS_PATH).json()["items"] == []
    (admitted_item,) = api.get(QUEUE_ITEMS_PATH).json()["items"]
    assert admitted_item["item_id"] == observed["item_id"]


def test_importing_on_an_unconnected_instance_is_refused_by_name(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)

    refused = api.post(PROJECT_SOURCE_IMPORT_PATH)

    assert refused.status_code == 409, refused.text
    assert refused.json()["type"].endswith(":project-source-not-connected")


def test_an_unanswering_platform_is_a_named_unavailability_writing_nothing(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    api = connected_api(runtime, tmp_path, _FakeGitHubIssueListing([], status=503))

    refused = api.post(PROJECT_SOURCE_IMPORT_PATH)

    assert refused.status_code == 503, refused.text
    assert refused.json()["type"].endswith(":project-source-unavailable")
    assert api.get(OBSERVED_QUEUE_ITEMS_PATH).json()["items"] == []


def test_a_malformed_platform_payload_is_a_named_refusal_writing_nothing(
    runtime: DbosRuntime, tmp_path: Path
) -> None:
    api = connected_api(
        runtime, tmp_path, _FakeGitHubIssueListing([{"title": "no number"}])
    )

    refused = api.post(PROJECT_SOURCE_IMPORT_PATH)

    assert refused.status_code == 502, refused.text
    assert refused.json()["type"].endswith(":project-source-payload-malformed")
    assert api.get(OBSERVED_QUEUE_ITEMS_PATH).json()["items"] == []


def test_listing_observed_items_on_an_empty_queue_is_an_empty_page(
    runtime: DbosRuntime,
) -> None:
    listed = durable_api_client(runtime).get(OBSERVED_QUEUE_ITEMS_PATH)

    assert listed.status_code == 200, listed.text
    assert listed.json() == {"items": [], "next_after": None}
