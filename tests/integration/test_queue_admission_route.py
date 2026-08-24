"""The queue's HTTP doors, driven against a real store.

Phase A gave the queue an application caller and a read; these tests drive the
POST that admits an observed item and the GET that lists the admitted ones
through the composed server, so every claim about an HTTP status or body is a
property of the real route over the real store, not of a caller invoked by hand.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import (
    API_PREFIX,
    QUEUE_ADMISSIONS_PATH,
    QUEUE_ITEMS_PATH,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from tests.scenarios.api import durable_api_client
from tests.scenarios.runtime import exact_output_runtime

WORKFLOW_DOCUMENT = b"""format_version: 3
name: triage-backlog
nodes:
  - id: review
    type: agent
    role: reviewer
    mode: headless
    instruction: Review one bounded diff.
"""
SECOND_WORKFLOW_DOCUMENT = WORKFLOW_DOCUMENT.replace(
    b"triage-backlog", b"triage-backlog-two"
).replace(b"Review one bounded diff.", b"Review it again.")
LINEAGES = f"{API_PREFIX}/workflow-lineages"
REVISIONS = f"{API_PREFIX}/workflow-revisions"


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "queue-route-test"),
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


def founded_workflow_lineage(
    api: TestClient, document: bytes = WORKFLOW_DOCUMENT
) -> str:
    """A workflow the operator has published and named: the binding a queue item runs under."""

    published = api.post(
        REVISIONS, content=document, headers={"content-type": "application/yaml"}
    )
    assert published.status_code == 201, published.text
    founded = api.post(
        LINEAGES,
        json={
            "workflow_revision_hash": published.json()["workflow_revision_hash"],
            "actor": "operator",
            "activated_at": "2026-08-24T00:00:00Z",
        },
    )
    assert founded.status_code == 201, founded.text
    return str(founded.json()["lineage_id"])


def admission_body(
    lineage_id: str,
    *,
    tracker_item_reference: str = "gh:79",
    rationale: str = "matches the triage rule",
    expected_revision: int = 0,
) -> dict[str, object]:
    return {
        "project_id": "project1",
        "tracker_item_reference": tracker_item_reference,
        "workflow_lineage_id": lineage_id,
        "rationale": rationale,
        "expected_revision": expected_revision,
    }


def test_posting_an_observed_item_admits_it(runtime: DbosRuntime) -> None:
    api = durable_api_client(runtime)
    lineage_id = founded_workflow_lineage(api)

    response = api.post(QUEUE_ADMISSIONS_PATH, json=admission_body(lineage_id))

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["project_id"] == "project1"
    assert body["tracker_item_reference"] == "gh:79"
    assert body["workflow_lineage_id"] == lineage_id
    assert body["rationale"] == "matches the triage rule"
    assert body["revision"] == 1
    assert len(body["item_id"]) == 64


def test_admitting_the_same_item_again_is_idempotent(runtime: DbosRuntime) -> None:
    api = durable_api_client(runtime)
    lineage_id = founded_workflow_lineage(api)
    first = api.post(QUEUE_ADMISSIONS_PATH, json=admission_body(lineage_id))
    assert first.status_code == 201, first.text

    repeated = api.post(QUEUE_ADMISSIONS_PATH, json=admission_body(lineage_id))

    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == first.json()


def test_admitting_against_a_stale_revision_is_a_conflict(runtime: DbosRuntime) -> None:
    api = durable_api_client(runtime)
    lineage_id = founded_workflow_lineage(api)

    response = api.post(
        QUEUE_ADMISSIONS_PATH, json=admission_body(lineage_id, expected_revision=1)
    )

    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("queue-admission-revision-conflict")


def test_changing_the_binding_after_admission_is_refused(runtime: DbosRuntime) -> None:
    api = durable_api_client(runtime)
    first_lineage = founded_workflow_lineage(api)
    second_lineage = founded_workflow_lineage(api, SECOND_WORKFLOW_DOCUMENT)
    admitted = api.post(QUEUE_ADMISSIONS_PATH, json=admission_body(first_lineage))
    assert admitted.status_code == 201, admitted.text

    response = api.post(QUEUE_ADMISSIONS_PATH, json=admission_body(second_lineage))

    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("queue-admission-already-decided")
    # No state corruption: the durable admission still carries the first binding.
    listed = api.get(QUEUE_ITEMS_PATH)
    assert listed.status_code == 200, listed.text
    (item,) = listed.json()["items"]
    assert item["workflow_lineage_id"] == first_lineage


def test_listing_returns_admitted_items_with_binding_and_rationale(
    runtime: DbosRuntime,
) -> None:
    api = durable_api_client(runtime)
    lineage_id = founded_workflow_lineage(api)
    api.post(QUEUE_ADMISSIONS_PATH, json=admission_body(lineage_id))

    response = api.get(QUEUE_ITEMS_PATH)

    assert response.status_code == 200, response.text
    page = response.json()
    assert page["next_after"] is None
    (item,) = page["items"]
    assert item["project_id"] == "project1"
    assert item["tracker_item_reference"] == "gh:79"
    assert item["workflow_lineage_id"] == lineage_id
    assert item["rationale"] == "matches the triage rule"
    assert item["revision"] == 1


def test_listing_an_empty_queue_is_an_empty_page_not_an_error(
    runtime: DbosRuntime,
) -> None:
    response = durable_api_client(runtime).get(QUEUE_ITEMS_PATH)

    assert response.status_code == 200, response.text
    assert response.json() == {"items": [], "next_after": None}
