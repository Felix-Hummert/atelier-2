"""What a listed revision says about itself, read from its own published bytes.

ADR 0007 decision 4 refuses a per-attribute column beside the document, so every
field a listing shows is parsed from the stored bytes by the one document parser.
These tests therefore publish real documents over the real HTTP boundary and read
them back, rather than asserting against a projection built by hand.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import workflow_revisions
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.ports.workflow_revisions import (
    DescribedWorkflowRevisionPage,
    EnrichedPageBudget,
    QueryDurableStateCorrupt,
    WorkflowRevisionPage,
)
from tests.scenarios.api import api_limits, durable_api_client
from tests.scenarios.runtime import exact_output_runtime
from tests.scenarios.workflows import V3_DOCUMENT, V3_NODE_COUNT

V1_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: agent, type: agent, job: test, output: payload, next: final}
"""

DESCRIBED_NAME = "Nightly regression sweep"
DESCRIBED_DESCRIPTION = "Runs the sweep and files what it finds."

V3_DESCRIBED_DOCUMENT = f"""format_version: 3
name: {DESCRIBED_NAME}
description: {DESCRIBED_DESCRIPTION}
graph_outputs:
  - name: verdict
    from: {{node: sweep, output: findings}}
nodes:
  - id: sweep
    type: agent
    role: builder
    mode: headless
    instruction: Sweep the suite and name what broke.
    outputs:
      - name: findings
        schema: {{ref: workspace_candidate, revision: schema-candidate}}
""".encode()


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    configured = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "catalog-description-tests"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    configured.initialize_storage()
    try:
        yield configured
    finally:
        configured.close()


def _publish(client: TestClient, document: bytes) -> str:
    response = client.post(
        API_PREFIX + "/workflow-revisions",
        content=document,
        headers={"content-type": "application/yaml"},
    )
    assert response.status_code in (200, 201), response.text
    return str(response.json()["revision_hash"])


def _listed(client: TestClient) -> dict[str, dict[str, object]]:
    response = client.get(API_PREFIX + "/workflow-revisions")
    assert response.status_code == 200, response.text
    return {str(item["revision_hash"]): item for item in response.json()["items"]}


@pytest.mark.proves("a-published-revision-is-listed-with-the-name-its-author-wrote")
def test_a_published_revision_is_listed_with_the_name_its_author_wrote(
    runtime: DbosRuntime,
) -> None:
    client = durable_api_client(runtime)
    revision_hash = _publish(client, V3_DESCRIBED_DOCUMENT)

    listed = _listed(client)[revision_hash]

    assert listed["name"] == DESCRIBED_NAME
    assert listed["format_version"] == 3
    assert listed["executable"] is False


@pytest.mark.proves("a-format-that-declares-no-name-is-listed-as-unnamed")
def test_a_format_that_declares_no_name_is_listed_as_unnamed(
    runtime: DbosRuntime,
) -> None:
    client = durable_api_client(runtime)
    revision_hash = _publish(client, V1_DOCUMENT)

    listed = _listed(client)[revision_hash]

    assert listed["name"] is None
    assert listed["description"] is None
    assert listed["format_version"] == 1
    assert listed["executable"] is True


@pytest.mark.proves(
    "the-description-is-read-from-the-published-bytes-and-from-nowhere-else"
)
def test_the_description_is_read_from_the_published_bytes_and_from_nowhere_else(
    runtime: DbosRuntime,
) -> None:
    """The store keeps bytes, not fields, so a description has one home only.

    The listing must agree with the detail view and with the bytes themselves,
    and the table must carry no column either could have been read from instead.
    """

    client = durable_api_client(runtime)
    described = _publish(client, V3_DESCRIBED_DOCUMENT)
    undescribed = _publish(client, V3_DOCUMENT)

    listed = _listed(client)
    detail = client.get(API_PREFIX + f"/workflow-revisions/{described}").json()

    assert listed[described]["description"] == DESCRIBED_DESCRIPTION
    assert detail["graph"]["description"] == DESCRIBED_DESCRIPTION
    assert listed[undescribed]["description"] is None
    assert listed[undescribed]["name"] is not None
    stored_columns = set(workflow_revisions.c.keys())
    assert "description" not in stored_columns
    assert "name" not in stored_columns


@pytest.mark.proves("an-enriched-page-stops-at-its-derived-bound-and-pages-on")
def test_an_enriched_page_stops_at_its_derived_bound_and_pages_on(
    runtime: DbosRuntime,
) -> None:
    """A page spends a node budget and a byte budget, and reports where it ended.

    The budget is exercised at the port, because that is where a page decides how
    much of the store it is allowed to read; a bound proven only through HTTP
    would be proven one layer above the one that has to honour it.
    """

    client = durable_api_client(runtime)
    published = sorted(
        {
            _publish(client, V3_DESCRIBED_DOCUMENT),
            _publish(client, V3_DOCUMENT),
            _publish(client, V1_DOCUMENT),
        }
    )
    queries = DbosQueries(runtime.engine)
    budget = EnrichedPageBudget(maximum_nodes=V3_NODE_COUNT, maximum_document_bytes=1)

    page = queries.list_described_workflow_revisions(None, 50, budget)

    assert isinstance(page, DescribedWorkflowRevisionPage)
    assert len(page.items) == 1
    assert page.items[0].revision.revision_hash.value == published[0]
    assert page.next_after == WorkflowRevisionHash(published[0])

    rest = queries.list_described_workflow_revisions(page.next_after, 50, budget)

    assert isinstance(rest, DescribedWorkflowRevisionPage)
    assert rest.items[0].revision.revision_hash.value == published[1]


def test_a_generous_budget_lists_every_revision_and_ends_the_page(
    runtime: DbosRuntime,
) -> None:
    client = durable_api_client(runtime)
    for document in (V3_DESCRIBED_DOCUMENT, V3_DOCUMENT, V1_DOCUMENT):
        _publish(client, document)
    queries = DbosQueries(runtime.engine)

    page = queries.list_described_workflow_revisions(
        None,
        50,
        EnrichedPageBudget(maximum_nodes=1_000, maximum_document_bytes=1 << 20),
    )

    assert isinstance(page, DescribedWorkflowRevisionPage)
    assert len(page.items) == 3
    assert page.next_after is None


def test_the_frozen_hash_only_listing_still_answers_beside_the_enriched_one(
    runtime: DbosRuntime,
) -> None:
    """The V1 page query keeps its own shape; the enriched page is a second read."""

    client = durable_api_client(runtime)
    for document in (V1_DOCUMENT, V3_DESCRIBED_DOCUMENT):
        _publish(client, document)
    queries = DbosQueries(runtime.engine)

    frozen = queries.list_workflow_revisions(None, 50)
    enriched = queries.list_described_workflow_revisions(
        None,
        50,
        EnrichedPageBudget(maximum_nodes=1_000, maximum_document_bytes=1 << 20),
    )

    assert isinstance(frozen, WorkflowRevisionPage)
    assert isinstance(enriched, DescribedWorkflowRevisionPage)
    assert frozen.revision_hashes == tuple(
        item.revision.revision_hash for item in enriched.items
    )


def test_a_page_limit_outside_its_range_is_refused(runtime: DbosRuntime) -> None:
    queries = DbosQueries(runtime.engine)
    budget = EnrichedPageBudget(maximum_nodes=100, maximum_document_bytes=65_536)

    with pytest.raises(ValueError):
        queries.list_described_workflow_revisions(None, 0, budget)


def test_a_budget_must_be_positive_integers() -> None:
    with pytest.raises(ValueError):
        EnrichedPageBudget(maximum_nodes=0, maximum_document_bytes=1)
    with pytest.raises(ValueError):
        EnrichedPageBudget(maximum_nodes=1, maximum_document_bytes=-1)


def test_a_stored_document_that_denies_its_own_hash_is_named_not_listed(
    runtime: DbosRuntime,
) -> None:
    """A row whose bytes do not hash to its key is corruption, never a listing.

    Publication and the table's own triggers make this unreachable through the
    product -- a revision cannot be updated or deleted -- so the row is written
    directly, which is the shape damage at rest would take.
    """

    with runtime.engine.begin() as connection:
        connection.execute(
            sa.insert(workflow_revisions).values(
                revision_hash="0" * 64, document=V1_DOCUMENT
            )
        )
    queries = DbosQueries(runtime.engine)

    result = queries.list_described_workflow_revisions(
        None,
        50,
        EnrichedPageBudget(maximum_nodes=1_000, maximum_document_bytes=1 << 20),
    )

    assert isinstance(result, QueryDurableStateCorrupt)


def test_the_enriched_page_limits_reach_the_api_from_one_owner() -> None:
    """The two bounds are derivations, so the edge cannot drift from the document."""

    limits = api_limits()

    assert limits.maximum_enriched_page_nodes == limits.maximum_workflow_nodes
    assert (
        limits.maximum_enriched_page_document_bytes == limits.maximum_request_body_bytes
    )
