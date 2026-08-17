"""The HTTP door for a budget revision: bounds in, hash out, and a node that pins it.

ADR 0008 makes publication the first point where a budget is judged. These tests
drive the real route against the real store, and then bind a real V3 agent node
to the hash it answered -- because the sentence this door is worth anything for
is not "the bytes were stored" but "a node's budget reference resolves to bounds
this runtime read". A budget that bounds nothing never reaches either.
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
from atelier2.application.bind_run_configuration import bind_run_configuration
from atelier2.contracts.agents import AgentBindingSetHash
from atelier2.contracts.budgets_v3 import BudgetField, BudgetRevisionRefusal
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_configuration_v3 import ReferenceSite, ResolvedReference
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.contracts.workflow_bindings_v3 import SubworkflowBinding
from atelier2.contracts.workflows_v3 import VersionedReference, WorkflowGraphV3
from atelier2.ports.published_revisions import PublishedRevisionMissing
from tests.scenarios.api import durable_api_client
from tests.scenarios.runtime import exact_output_runtime

BUDGET_PATH = f"{API_PREFIX}/budget-revisions"
WORKFLOW_PATH = f"{API_PREFIX}/workflow-revisions"
BUILD_BUDGET = json.dumps(
    {
        BudgetField.ATTEMPT_DEADLINE_SECONDS.value: 900,
        BudgetField.MAXIMUM_ASSISTANT_TURNS.value: 8,
        BudgetField.REPORTED_OUTPUT_TOKEN_THRESHOLD.value: 64_000,
    }
).encode()


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "budget-door-test"),
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


def bounded_document(budget_revision: str) -> bytes:
    """One agent node whose budget reference pins exactly the published bounds."""

    return f"""format_version: 3
name: Cook, within what one attempt may spend
nodes:
  - id: cook
    type: agent
    role: cook
    mode: headless
    instruction: Cook the meal this chain is for.
    budget: {{ref: build-budget, revision: {budget_revision}}}
""".encode()


@pytest.mark.proves("a-published-budget-bounds-an-attempt-or-is-refused-by-name")
def test_a_budget_published_over_http_is_the_hash_an_agent_node_binds(
    runtime: DbosRuntime,
) -> None:
    api = client(runtime)

    created = api.post(
        BUDGET_PATH, content=BUILD_BUDGET, headers={"content-type": "application/json"}
    )
    retried = api.post(
        BUDGET_PATH, content=BUILD_BUDGET, headers={"content-type": "application/json"}
    )

    assert (created.status_code, retried.status_code) == (201, 200)
    assert created.json() == retried.json()
    budget_hash = created.json()["revision_hash"]
    assert budget_hash == PublishedRevisionHash.of(BUILD_BUDGET).value

    published = api.post(
        WORKFLOW_PATH,
        content=bounded_document(budget_hash),
        headers={"content-type": "application/yaml"},
    )

    assert published.status_code == 201, published.text
    graph = parse_workflow_document(bounded_document(budget_hash))
    assert isinstance(graph, WorkflowGraphV3)
    snapshot = bind_run_configuration(
        WorkflowRevisionHash(published.json()["revision_hash"]),
        graph,
        SubworkflowBinding(),
        AgentBindingSetHash.of(b"no agent roles"),
        DbosCatalogStore(runtime.engine),
    )

    assert snapshot.resolutions == (
        ResolvedReference(
            ReferenceSite("budget", "cook", None),
            RevisionKind.BUDGET_POLICY,
            VersionedReference(ref="build-budget", revision=budget_hash),
            PublishedRevisionHash(budget_hash),
        ),
    )


@pytest.mark.proves("a-published-budget-bounds-an-attempt-or-is-refused-by-name")
@pytest.mark.parametrize(
    ("document", "refusal"),
    (
        (
            b'{"maximum_assistant_turns": 8}',
            BudgetRevisionRefusal.MISSING_ATTEMPT_DEADLINE,
        ),
        (
            b'{"attempt_deadline_seconds": 900, "cost_ceiling": 5}',
            BudgetRevisionRefusal.UNKNOWN_FIELD,
        ),
        (
            b'{"attempt_deadline_seconds": 0}',
            BudgetRevisionRefusal.VALUE_NOT_A_POSITIVE_INT64,
        ),
        (b"what one build may spend", BudgetRevisionRefusal.NOT_A_BUDGET_OBJECT),
        (b"\xff\xfe", BudgetRevisionRefusal.DOCUMENT_NOT_UTF8),
    ),
    ids=lambda value: value.value if isinstance(value, BudgetRevisionRefusal) else "",
)
def test_a_budget_that_bounds_nothing_is_named_by_its_own_reason(
    runtime: DbosRuntime, document: bytes, refusal: BudgetRevisionRefusal
) -> None:
    refused = client(runtime).post(
        BUDGET_PATH, content=document, headers={"content-type": "application/json"}
    )

    assert refused.status_code == 422
    assert refused.json()["type"].endswith(f":budget-{refusal.value}")
    assert refusal.value in refused.json()["detail"]
    missing = DbosCatalogStore(runtime.engine).resolve(
        RevisionKind.BUDGET_POLICY, PublishedRevisionHash.of(document)
    )
    assert isinstance(missing, PublishedRevisionMissing)


def test_a_budget_publication_refuses_the_wrong_media_type(
    runtime: DbosRuntime,
) -> None:
    refused = client(runtime).post(
        BUDGET_PATH, content=BUILD_BUDGET, headers={"content-type": "application/yaml"}
    )

    assert refused.status_code == 415
    assert refused.json()["type"].endswith(":unsupported-media-type")
