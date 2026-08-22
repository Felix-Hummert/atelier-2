from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.artifact_store import DbosArtifactStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs, workflow_revisions
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import (
    MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS,
    encode_canonical_base64,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from tests.scenarios.api import (
    api_limits,
    discovered_openapi_document,
    durable_queries,
    event_poll_backoff,
    published_workflow_grammar,
)
from tests.scenarios.runtime import exact_output_runtime
from tests.scenarios.workflows import (
    V3_CONTROL_EDGE_LINE,
    V3_DOCUMENT,
    V3_DOCUMENT_NAME,
    V3_NODE_COUNT,
)

GUESSED_PATH = "/a-path-a-first-contact-guesses"

V1_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: agent, type: agent, job: test, output: payload, next: final}
"""


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    configured = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "v3-publication-tests"),
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


def _client(runtime: DbosRuntime) -> TestClient:
    queries = durable_queries(runtime.engine)
    return TestClient(
        create_app(
            source_commit="commit-under-test",
            source_tree="tree-under-test",
            ports=ApiPorts(
                workflow_revision_publisher=DbosWorkflowRevisionPublisher(
                    runtime.engine
                ),
                published_run_starter=DbosDurableRunStarter(
                    runtime.engine,
                    runtime.settings,
                    runtime.agent_executor_registry,
                ),
                wait_answerer=DbosWaitAnswerer(
                    runtime.engine, runtime.settings.application_version
                ),
                reconcile_commander=DbosEffectReconcileCommander(
                    runtime.engine, runtime.settings
                ),
                workflow_revision_queries=queries,
                run_queries=queries,
                run_event_queries=queries,
                workflow_document_parser=parse_workflow_document,
                agent_configuration_catalog=DbosAgentConfigurationCatalog(
                    runtime.engine, runtime.agent_executor_registry
                ),
                agent_attempt_canceller=DbosAgentAttemptStore(
                    runtime.engine, runtime.settings.application_version
                ),
                catalog_resolver=DbosCatalogStore(runtime.engine),
                catalog_admissions=DbosCatalogStore(runtime.engine),
                published_revision_registry=DbosCatalogStore(runtime.engine),
                artifact_publisher=DbosArtifactStore(runtime.engine),
                host_configuration_channel=DbosHostConfigurationChannel(runtime.engine),
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def _publish(client: TestClient, document: bytes) -> Response:
    return client.post(
        API_PREFIX + "/workflow-revisions",
        content=document,
        headers={"content-type": "application/yaml"},
    )


def _row_count(runtime: DbosRuntime, table: sa.Table) -> int:
    with runtime.engine.connect() as connection:
        return int(
            connection.scalar(sa.select(sa.func.count()).select_from(table)) or 0
        )


def test_a_valid_v3_document_publishes_as_one_immutable_hash_identified_revision(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)

    created = _publish(client, V3_DOCUMENT)
    retried = _publish(client, V3_DOCUMENT)

    assert (created.status_code, retried.status_code) == (201, 200)
    assert created.json() == retried.json()
    assert (
        created.json()["workflow_revision_hash"]
        == hashlib.sha256(V3_DOCUMENT).hexdigest()
    )
    assert created.json()["document_base64"] == encode_canonical_base64(V3_DOCUMENT)
    assert _row_count(runtime, workflow_revisions) == 1


EXECUTABLE_V3_DOCUMENT = b"""format_version: 3
name: One agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
    outputs:
      - name: result
        schema: {ref: any-json, revision: "%s"}
""" % (b"e" * 64)


@pytest.mark.proves("a-document-written-against-the-published-shape-is-published")
def test_a_document_written_against_the_published_shape_is_taken_by_the_door(
    runtime: DbosRuntime,
) -> None:
    """First contact, end to end: a refusal, a description, a stored revision.

    Every step is an answer this API gave -- where its description is, which
    body the publication takes, and what shape that body must have. Until the
    description existed, this last step was the one thing a consumer could only
    learn from the repository.
    """
    client = _client(runtime)
    described = discovered_openapi_document(client, GUESSED_PATH)

    published_workflow_grammar(described).validate(
        yaml.safe_load(EXECUTABLE_V3_DOCUMENT)
    )
    created = _publish(client, EXECUTABLE_V3_DOCUMENT)

    assert created.status_code == 201
    assert (
        created.json()["workflow_revision_hash"]
        == hashlib.sha256(EXECUTABLE_V3_DOCUMENT).hexdigest()
    )
    assert created.json()["graph"]["executable"] is True


@pytest.mark.proves("a-revision-says-which-form-it-waits-for-not-which-version-it-is")
def test_a_v3_revision_this_build_runs_reads_back_as_executable(
    runtime: DbosRuntime,
) -> None:
    """The other half of the same rule: a document this build runs says so.

    While `executable` was the constant false, no V3 revision could ever answer
    this way, and the reader was told about the version rather than the document.
    Both halves come from the one rule the start path applies, so this and the
    refusal below cannot drift apart.
    """
    client = _client(runtime)
    revision_hash = _publish(client, EXECUTABLE_V3_DOCUMENT).json()[
        "workflow_revision_hash"
    ]

    read = client.get(API_PREFIX + f"/workflow-revisions/{revision_hash}")

    assert read.status_code == 200
    assert read.json()["graph"]["executable"] is True
    assert read.json()["graph"]["not_executable_reason"] is None
    # The roles are what a caller must bind to start this revision, and until now
    # they could be learnt only by reading the document itself.
    assert read.json()["graph"]["agent_roles"] == ["builder"]
    assert read.json()["graph"]["orders"] == []
    assert read.json()["graph"]["node_previews"] == [
        {
            "id": "implement",
            "kind": "agent",
            "role": "builder",
            "instruction_start": "Do the one thing this chain is for.",
            "depends_on": [],
        }
    ]


def test_a_v3_revision_announces_the_orders_it_declares(
    runtime: DbosRuntime,
) -> None:
    """The graph says which orders a start must supply, without republishing them.

    Until this head a caller could learn the names only by parsing the document
    itself. The announcement is the same class as `agent_roles`: name plus the
    schema the author pinned, and nothing of the schema bytes.
    """
    document = b"""format_version: 3
name: Cook to order
graph_inputs:
  - name: portions
    schema:
      ref: portions-schema
      revision: schema-portions
nodes:
  - id: cook
    type: agent
    role: cook
    mode: headless
    instruction: Cook exactly what the order says.
    inputs:
      - name: portions
        from:
          graph_input: portions
"""
    client = _client(runtime)
    published = _publish(client, document).json()
    revision_hash = published["workflow_revision_hash"]

    graph = client.get(API_PREFIX + f"/workflow-revisions/{revision_hash}").json()[
        "graph"
    ]
    expected_orders = [
        {
            "name": "portions",
            "schema": {
                "ref": "portions-schema",
                "revision": "schema-portions",
            },
        }
    ]

    assert published["graph"]["orders"] == expected_orders
    assert graph["orders"] == expected_orders
    assert document.decode() not in str(graph)


@pytest.mark.proves("a-revision-says-which-form-it-waits-for-not-which-version-it-is")
def test_the_published_v3_revision_reads_back_naming_what_it_waits_for(
    runtime: DbosRuntime,
) -> None:
    """Not executable is the answer; which form is waiting is the useful half.

    This revision was refused for its format before, and is refused for its own
    authored forms now -- the verdict is unchanged, the reason it gives is the
    document's rather than the version's.
    """
    client = _client(runtime)
    published = _publish(client, V3_DOCUMENT)
    revision_hash = published.json()["workflow_revision_hash"]

    read = client.get(API_PREFIX + f"/workflow-revisions/{revision_hash}")

    assert read.status_code == 200
    assert read.json() == published.json()
    assert read.json()["graph"] == {
        "workflow_format_version": 3,
        "executable": False,
        "not_executable_reason": (
            "graph outputs nothing carries out of a run: verdict"
        ),
        "node_count": V3_NODE_COUNT,
        "agent_roles": ["builder", "reviewer"],
        "orders": [],
        "node_previews": [
            {
                "id": "implement",
                "kind": "agent",
                "role": "builder",
                "instruction_start": (
                    "Implement every acceptance sentence of the bound story."
                ),
                "depends_on": [],
            },
            {
                "id": "review",
                "kind": "agent",
                "role": "reviewer",
                "instruction_start": (
                    "Name every defect with the sentence it violates."
                ),
                "depends_on": ["implement"],
            },
        ],
        "loops": [],
        "name": V3_DOCUMENT_NAME,
        "description": None,
    }
    assert client.get(API_PREFIX + "/workflow-revisions").json() == {
        "items": [{"workflow_revision_hash": revision_hash}],
        "next_after_revision_hash": None,
    }


def test_a_v3_revision_answers_an_instruction_start_not_the_authored_whole(
    runtime: DbosRuntime,
) -> None:
    """The preview is an excerpt: past the wire bound, the rest stays in the document."""
    bound = MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS
    authored = ("ä" * bound) + "TAIL-MUST-NOT-APPEAR"
    document = (
        "format_version: 3\n"
        "name: One long instruction\n"
        "nodes:\n"
        "  - id: implement\n"
        "    type: agent\n"
        "    role: builder\n"
        "    mode: headless\n"
        f"    instruction: {authored}\n"
    ).encode()
    client = _client(runtime)
    revision_hash = _publish(client, document).json()["workflow_revision_hash"]

    graph = client.get(API_PREFIX + f"/workflow-revisions/{revision_hash}").json()[
        "graph"
    ]

    assert graph["node_previews"] == [
        {
            "id": "implement",
            "kind": "agent",
            "role": "builder",
            "instruction_start": authored[:bound],
            "depends_on": [],
        }
    ]
    assert "TAIL-MUST-NOT-APPEAR" not in graph["node_previews"][0]["instruction_start"]
    assert len(graph["node_previews"][0]["instruction_start"]) == bound


def test_a_v3_node_without_an_instruction_answers_that_field_empty(
    runtime: DbosRuntime,
) -> None:
    """A wait declares a prompt, not an instruction. Empty is the node's answer."""
    document = b"""format_version: 3
name: Wait for a decision
nodes:
  - id: approve
    type: wait
    prompt: Approve the candidate or send it back.
    outputs:
      - name: decision
        schema: {ref: decision, revision: schema-decision}
"""
    client = _client(runtime)
    revision_hash = _publish(client, document).json()["workflow_revision_hash"]

    graph = client.get(API_PREFIX + f"/workflow-revisions/{revision_hash}").json()[
        "graph"
    ]

    assert graph["node_previews"] == [
        {
            "id": "approve",
            "kind": "wait",
            "role": None,
            "instruction_start": None,
            "depends_on": [],
        }
    ]
    assert graph["agent_roles"] == []
    assert graph["orders"] == []
    assert "Approve the candidate" not in str(graph["node_previews"])


def test_a_v1_revision_does_not_invent_a_v3_node_preview(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)
    revision_hash = _publish(client, V1_DOCUMENT).json()["workflow_revision_hash"]

    graph = client.get(API_PREFIX + f"/workflow-revisions/{revision_hash}").json()[
        "graph"
    ]

    assert graph["workflow_format_version"] == 1
    assert "node_previews" not in graph
    assert "instruction_start" not in graph["nodes"][0]
    assert graph["nodes"][0]["job"] == "test"


def test_the_described_listing_does_not_carry_the_node_excerpt(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)
    revision_hash = _publish(client, EXECUTABLE_V3_DOCUMENT).json()[
        "workflow_revision_hash"
    ]

    listed = client.get(API_PREFIX + "/workflow-revisions?view=described")

    assert listed.status_code == 200
    item = listed.json()["items"][0]
    assert item["workflow_revision_hash"] == revision_hash
    assert "node_previews" not in item
    assert "nodes" not in item


def test_starting_a_run_on_a_v3_revision_is_refused_by_name_and_writes_no_run(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)
    revision_hash = _publish(client, V3_DOCUMENT).json()["workflow_revision_hash"]

    refused = client.post(
        API_PREFIX + "/runs",
        json={"run_id": "v3-run", "workflow_revision_hash": revision_hash},
    )

    assert refused.status_code == 409
    assert refused.json()["type"].endswith(":workflow-format-not-executable")
    assert _row_count(runtime, runs) == 0


def test_a_v1_revision_published_beside_a_v3_one_still_starts_its_run(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)
    _publish(client, V3_DOCUMENT)
    executable_hash = _publish(client, V1_DOCUMENT).json()["workflow_revision_hash"]

    started = client.post(
        API_PREFIX + "/runs",
        json={"run_id": "v1-run", "workflow_revision_hash": executable_hash},
    )

    assert started.status_code == 201
    assert started.json()["workflow_revision_hash"] == executable_hash
    assert _row_count(runtime, runs) == 1


@pytest.mark.parametrize(
    ("broken", "expected_fragments"),
    [
        pytest.param(
            V3_DOCUMENT.replace(V3_CONTROL_EDGE_LINE, b""),
            ("'review'", "'inputs'", "data_edge_outside_closure"),
            id="data edge outside the depends_on closure",
        ),
        pytest.param(
            V3_DOCUMENT.replace(
                b"    role: reviewer\n", b"    role: reviewer\n    next: done\n"
            ),
            ("'review'", "'next'", "retired_key", "depends_on"),
            id="retired V1 key",
        ),
        pytest.param(
            V3_DOCUMENT.replace(
                b"    instruction: Name every",
                b"    speed: fast\n    instruction: Name every",
            ),
            ("'review'", "'speed'", "unknown_field"),
            id="unknown field",
        ),
        pytest.param(
            V3_DOCUMENT.replace(
                b"    type: agent\n    role: reviewer",
                b"    type: mystery\n    role: reviewer",
            ),
            ("'review'", "'type'", "invalid_value"),
            id="unknown node kind",
        ),
        pytest.param(
            V3_DOCUMENT.replace(
                b"    type: agent\n    role: reviewer", b"    role: reviewer"
            ),
            ("'review'", "'type'", "missing_field"),
            id="missing node kind",
        ),
        pytest.param(
            V3_DOCUMENT + b"format_version: 3\n",
            ("'format_version'", "duplicate_key"),
            id="unsafe YAML duplicate key",
        ),
        pytest.param(
            V3_DOCUMENT.replace(b"format_version: 3", b"format_version: !!int 3", 1),
            ("'tag'", "forbidden_yaml_feature"),
            id="unsafe YAML explicit tag",
        ),
        pytest.param(
            V3_DOCUMENT.replace(
                b"    outputs:\n      - name: candidate\n",
                b"    outputs: "
                + b"[" * 40
                + b"]" * 40
                + b"\n      - name: candidate\n",
            ),
            ("'nesting'", "document_too_deep"),
            id="unsafe YAML nested past the bound",
        ),
    ],
)
def test_an_invalid_v3_document_is_refused_naming_its_node_and_field(
    runtime: DbosRuntime, broken: bytes, expected_fragments: tuple[str, ...]
) -> None:
    client = _client(runtime)

    refused = _publish(client, broken)

    assert refused.status_code == 422
    assert refused.json()["type"].endswith(":invalid-workflow-document")
    detail = refused.json()["detail"]
    assert all(fragment in detail for fragment in expected_fragments), detail
    assert _row_count(runtime, workflow_revisions) == 0
