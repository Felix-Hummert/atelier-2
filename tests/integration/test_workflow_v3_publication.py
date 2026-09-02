from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa
import yaml
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import runs, workflow_revisions
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import (
    MAXIMUM_NODE_INSTRUCTION_PREVIEW_CHARACTERS,
    encode_canonical_base64,
)
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.tool_grants_v3 import ToolGrantCapability
from tests.scenarios.agents import (
    agent_scratch_root,
    failing_agent_executor_factory,
)
from tests.scenarios.api import (
    api_limits,
    discovered_openapi_document,
    durable_ports,
    event_poll_backoff,
    published_workflow_grammar,
)
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.runs import publish_v3_agent_bindings
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    V3_CONTROL_EDGE_LINE,
    V3_DOCUMENT,
    V3_DOCUMENT_NAME,
    V3_NODE_COUNT,
    declared_output,
)

GUESSED_PATH = "/a-path-a-first-contact-guesses"


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    """The runtime these publications read through, armed to start a V3 line.

    The V3 executor is what makes a start here a real one rather than a refusal
    about an unbound executor; nothing in this file launches it, so no agent
    ever runs.
    """
    configured = DbosRuntime(
        canonical_runtime_settings(
            tmp_path, "v3-publication-tests", agent_scratch_root(tmp_path)
        ),
        canonical_loopback_effects(tmp_path),
        (failing_agent_executor_factory("exact", []),),
    )
    configured.initialize_storage()
    try:
        yield configured
    finally:
        configured.close()


def _client(runtime: DbosRuntime) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit-under-test",
            source_tree="tree-under-test",
            ports=durable_ports(
                runtime.engine, runtime.settings, runtime.agent_executor_registry
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


PUBLICATION_PATH_OF_KIND = {
    RevisionKind.SCHEMA: API_PREFIX + "/schema-revisions",
    RevisionKind.TOOL: API_PREFIX + "/tool-grant-revisions",
}


def _publish_revision(client: TestClient, revision: PublishedRevision) -> None:
    published = client.post(
        PUBLICATION_PATH_OF_KIND[revision.kind],
        content=revision.document,
        headers={"content-type": "application/json"},
    )
    assert published.status_code in (200, 201), published.text


def _publish_schema(client: TestClient, schema: PublishedRevision) -> None:
    _publish_revision(client, schema)


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
""" + declared_output()
"""Executable once `ANY_JSON_SCHEMA`, the one reference it pins, is published."""


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
    _publish_schema(client, ANY_JSON_SCHEMA)
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
    _publish_schema(client, ANY_JSON_SCHEMA)
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


VERIFICATION_GRANT = PublishedRevision(
    RevisionKind.TOOL,
    json.dumps(
        {"capability": ToolGrantCapability.RUN_PROJECT_VERIFICATION.value}
    ).encode(),
)
GRANTED_V3_DOCUMENT = (
    b"""format_version: 3
name: One agent that must verify the project
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
    tools:
      - {ref: project-verification, revision: %s}
"""
    % VERIFICATION_GRANT.revision_hash.value.encode()
    + declared_output()
)


@dataclass(frozen=True)
class PinnedReference:
    """One kind a node pins, the document pinning it, and where the author wrote it."""

    document: bytes
    missing: PublishedRevision
    published_first: tuple[PublishedRevision, ...]
    site: str


PINNED_REFERENCES = {
    "schema": PinnedReference(
        EXECUTABLE_V3_DOCUMENT, ANY_JSON_SCHEMA, (), "field 'outputs.schema'"
    ),
    "tool-grant": PinnedReference(
        GRANTED_V3_DOCUMENT, VERIFICATION_GRANT, (ANY_JSON_SCHEMA,), "field 'tools'"
    ),
}


@pytest.mark.proves("a-revision-says-which-form-it-waits-for-not-which-version-it-is")
@pytest.mark.parametrize("pinned", PINNED_REFERENCES.values(), ids=PINNED_REFERENCES)
def test_a_revision_pinning_an_unpublished_reference_is_not_executable_until_it_is(
    runtime: DbosRuntime, pinned: PinnedReference
) -> None:
    """The reader's verdict and the start's are one verdict (#701).

    A well-formed line whose pinned reference nobody published read back as
    executable while the start refused it as not executable, and the conductor
    trusted the reading. Now the publication answer, the detail read and the
    described listing all name the reference the start would refuse, and the
    moment that reference is published, all of them say executable.
    """
    client = _client(runtime)
    for revision in pinned.published_first:
        _publish_revision(client, revision)
    created = _publish(client, pinned.document).json()
    revision_hash = created["workflow_revision_hash"]

    assert created["graph"]["executable"] is False
    reason = created["graph"]["not_executable_reason"]
    assert pinned.site in reason
    assert pinned.missing.revision_hash.value in reason
    assert reason.endswith("[unpublished_revision]")
    listed = client.get(API_PREFIX + "/workflow-revisions?view=described").json()
    assert [item["not_executable_reason"] for item in listed["items"]] == [reason]

    refused = client.post(
        API_PREFIX + "/runs",
        json={
            "workflow_format_version": 2,
            "run_id": "unresolved-reference",
            "workflow_revision_hash": revision_hash,
            "agent_bindings": [
                {"role": "builder", "agent_configuration_revision_hash": "c" * 64}
            ],
        },
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["type"].endswith(":workflow-format-not-executable")
    assert _row_count(runtime, runs) == 0

    _publish_revision(client, pinned.missing)

    read = client.get(API_PREFIX + f"/workflow-revisions/{revision_hash}").json()
    assert read["graph"] == {
        **created["graph"],
        "executable": True,
        "not_executable_reason": None,
    }
    relisted = client.get(API_PREFIX + "/workflow-revisions?view=described").json()
    assert [item["executable"] for item in relisted["items"]] == [True]


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
        "wait_answer_schemas": [],
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
    # `schema-decision` is not a hash this build has ever published, so the
    # excerpt has nothing to classify -- it names the wait node's own schema
    # hull and falls back to `free` rather than guessing.
    assert graph["wait_answer_schemas"] == [
        {
            "node_id": "approve",
            "schema": {"ref": "decision", "revision": "schema-decision"},
            "kind": "free",
            "values": None,
        }
    ]


@pytest.mark.proves("a-waiting-v3-run-is-answerable-on-its-run-page")
def test_a_published_wait_schema_reads_back_classified_over_the_real_route(
    runtime: DbosRuntime,
) -> None:
    """The live hole #553 closes: POST the schema, then GET the wait node that pins it.

    Naming `boolean` or `enum` needs the schema's own bytes, and the API layer
    may not make that read itself by matching a port's record directly
    (`api-port-record-problems`, `scripts/check_architecture.py`) -- so
    `atelier2.application.read_workflow_revisions` reads and classifies, and
    the projection only renders what it hands over. This is the real
    `GET /workflow-revisions/{hash}` route, the real registry, no intercept.
    """
    client = _client(runtime)
    boolean_schema = client.post(
        API_PREFIX + "/schema-revisions",
        content=b'{"type": "boolean"}',
        headers={"content-type": "application/json"},
    ).json()["schema_revision_hash"]
    enum_schema = client.post(
        API_PREFIX + "/schema-revisions",
        content=b'{"enum": ["approve", "revise"]}',
        headers={"content-type": "application/json"},
    ).json()["schema_revision_hash"]

    def wait_document(schema_revision: str) -> bytes:
        return f"""format_version: 3
name: Ship it or hold it
nodes:
  - id: go
    type: wait
    prompt: Ship it?
    outputs:
      - name: decision
        schema: {{ref: decision, revision: {schema_revision}}}
""".encode()

    boolean_revision_hash = _publish(client, wait_document(boolean_schema)).json()[
        "workflow_revision_hash"
    ]
    boolean_graph = client.get(
        API_PREFIX + f"/workflow-revisions/{boolean_revision_hash}"
    ).json()["graph"]

    assert boolean_graph["wait_answer_schemas"] == [
        {
            "node_id": "go",
            "schema": {"ref": "decision", "revision": boolean_schema},
            "kind": "boolean",
            "values": None,
        }
    ]

    enum_revision_hash = _publish(client, wait_document(enum_schema)).json()[
        "workflow_revision_hash"
    ]
    enum_graph = client.get(
        API_PREFIX + f"/workflow-revisions/{enum_revision_hash}"
    ).json()["graph"]

    assert enum_graph["wait_answer_schemas"] == [
        {
            "node_id": "go",
            "schema": {"ref": "decision", "revision": enum_schema},
            "kind": "enum",
            "values": ['"approve"', '"revise"'],
        }
    ]


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


def test_a_runnable_revision_published_beside_a_refused_one_still_starts_its_run(
    runtime: DbosRuntime,
) -> None:
    """A revision this build refuses does not stand in the way of one it runs.

    The refusal above is about the document a start names, not about what else
    the store holds -- so the same store answering `409` for one revision
    answers `201` for the next, and exactly one run row exists afterwards.
    """
    client = _client(runtime)
    _publish_schema(client, ANY_JSON_SCHEMA)
    _publish(client, V3_DOCUMENT)
    executable_hash = _publish(client, EXECUTABLE_V3_DOCUMENT).json()[
        "workflow_revision_hash"
    ]
    bindings = publish_v3_agent_bindings(
        runtime.engine, runtime.agent_executor_registry
    )

    started = client.post(
        API_PREFIX + "/runs",
        json={
            "workflow_format_version": 3,
            "run_id": "runnable-run",
            "workflow_revision_hash": executable_hash,
            "agent_bindings": [
                {
                    "role": binding.role,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash
                    ),
                }
                for binding in bindings
            ],
            "orders": [],
        },
    )

    assert started.status_code == 201, started.text
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
