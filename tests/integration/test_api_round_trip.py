"""Every request this consumer writes is assembled out of the answers before it.

**What is being proved.** Finding 2 of the #85 measurement said the API answers
with values a consumer cannot hand back unchanged: the same thing was called
`revision_hash` on one surface and `workflow_revision_hash` on another, the
format version split the same way, and the address `POST /artifacts` answered
had to be renamed to travel in an order. A consumer therefore had to carry a
translation table that lives nowhere on the wire.

**How this file proves it, rather than asserting it.** Nothing here writes a
field name next to a value read from a different field name. Every value that
crosses from an answer into the next request goes through `carried`, which can
only produce a field under the name the previous answer already used. A drift
does not make an assertion fail somewhere downstream -- it makes the request
unbuildable, at the line that tries to build it. Reverting any one of the
renames this change made kills this test where the drift is.

The four round-trips are the ones an operator's machine really walks: a name to
a start, a run reading to its answer, a catalog reading to a start, and a
published artifact to the order that names it. Discovery from a bare base URL is
the first head's proof (#322 Kopf 1) and is not repeated here; this consumer
starts where that one left off, at the versioned prefix.
"""

from __future__ import annotations

import time
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.openapi import API_PREFIX
from atelier2.api.references import encode_canonical_base64
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.runs import RunState
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.api import durable_api_client

WORKFLOW_PATH = API_PREFIX + "/workflow-revisions"
LINEAGE_PATH = API_PREFIX + "/workflow-lineages"
SCHEMA_PATH = API_PREFIX + "/schema-revisions"
ARTIFACT_PATH = API_PREFIX + "/artifacts"
AUTH_PROFILE_PATH = API_PREFIX + "/auth-profile-revisions"
AGENT_CONFIGURATION_PATH = API_PREFIX + "/agent-configuration-revisions"
RUN_PATH = API_PREFIX + "/runs"

ANY_JSON = b"true"
"""The schema an order and an agent output pin here: it admits every JSON value.

The subject of this file is which words a value travels under, not which values
a schema admits, so the narrowest useful schema is the one that never refuses.
The wait node below pins a schema that does refuse, because the answer this
consumer writes has to be a real answer to a real question."""

ONLY_A_STRING = b'{"type": "string"}'

PROVIDER_OUTPUT = b'"the exact provider bytes"'
APPROVAL = b'"approved"'
ORDER_MATERIAL = b'{"portions": 7}'
RUN_ID = "322/round-trip"
READ_DEADLINE_SECONDS = 8
READ_INTERVAL_SECONDS = 0.025


def workflow_document(order_schema: str, approval_schema: str) -> bytes:
    """One line that needs an order, an agent, a person, and a second agent.

    The two schema revisions are the ones the publication just answered with, so
    even the document a consumer authors is written out of an answer.
    """

    return f"""format_version: 3
name: cook-and-approve
graph_inputs:
  - name: order
    schema:
      ref: order-schema
      revision: {order_schema}
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Cook exactly what the order says.
    inputs:
      - name: order
        from:
          graph_input: order
    outputs:
      - name: result
        schema:
          ref: result-schema
          revision: {order_schema}
  - id: approve
    type: wait
    prompt: Approve this candidate, or name the blocking defect.
    depends_on: [implement]
    outputs:
      - name: approval
        schema:
          ref: approval-schema
          revision: {approval_schema}
  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Check what the person approved.
    depends_on: [approve]
    outputs:
      - name: verdict
        schema:
          ref: verdict-schema
          revision: {order_schema}
""".encode()


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    """A runtime whose agents succeed, so the line reaches the person and ends."""

    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "round-trip-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (
            RecordingAgentExecutorFactoryV2(
                "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
            ),
        ),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def carried(answer: Mapping[str, Any], *names: str) -> dict[str, Any]:
    """The named fields of one answer, under the names that answer gave them.

    This is the whole mechanism of this file. It cannot produce a request field
    whose name the previous answer did not already use, so a consumer that had
    to translate one spelling into another could not write its next request
    through here at all -- it would fail here, naming the field that drifted.
    """

    absent = tuple(name for name in names if name not in answer)
    if absent:
        raise AssertionError(
            f"the answer does not name {absent}; it names {tuple(sorted(answer))}"
        )
    return {name: answer[name] for name in names}


def answered(response: Response, *expected: int) -> dict[str, Any]:
    assert response.status_code in expected, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def read_until(client: TestClient, reference: str, state: RunState) -> dict[str, Any]:
    """The run as the consumer sees it once it reaches the state it waits for."""

    deadline = time.monotonic() + READ_DEADLINE_SECONDS
    run: dict[str, Any] = {}
    while time.monotonic() < deadline:
        run = answered(client.get(f"{RUN_PATH}/{reference}"), 200)
        if run["state"] == state.value:
            return run
        time.sleep(READ_INTERVAL_SECONDS)
    raise AssertionError(f"run stayed {run.get('state')!r}, expected {state.value!r}")


def node_owing_a_move(run: Mapping[str, Any]) -> Mapping[str, Any]:
    """The rail entry of the node this run says a person owes a move to."""

    owing = [
        node for node in run["node_rail"] if node["state"] == NodeState.NEEDS_YOU.value
    ]
    assert len(owing) == 1, run["node_rail"]
    return owing[0]


@pytest.mark.proves("a-consumer-writes-every-request-out-of-the-answers-it-read")
def test_a_consumer_drives_four_round_trips_without_renaming_a_single_value(
    runtime: DbosRuntime,
) -> None:
    """Name to start, run to answer, catalog to start, artifact to order.

    Read as a story: this consumer publishes what its document pins, publishes
    the document, names it, asks the catalog what that name holds, asks the
    listing what format that revision is, publishes the material its order
    carries, starts the run, waits for the person, answers, and reads the ended
    run. Every field of every request above came out of `carried`.
    """

    client = durable_api_client(runtime)
    json_media = {"content-type": "application/json"}

    order_schema = answered(
        client.post(SCHEMA_PATH, content=ANY_JSON, headers=json_media), 200, 201
    )
    approval_schema = answered(
        client.post(SCHEMA_PATH, content=ONLY_A_STRING, headers=json_media), 200, 201
    )
    document = workflow_document(
        carried(order_schema, "schema_revision_hash")["schema_revision_hash"],
        carried(approval_schema, "schema_revision_hash")["schema_revision_hash"],
    )

    published = answered(
        client.post(
            WORKFLOW_PATH,
            content=document,
            headers={"content-type": "application/yaml"},
        ),
        201,
    )
    admitted = answered(
        client.post(
            LINEAGE_PATH,
            json={
                **carried(published, "workflow_revision_hash"),
                "actor": "operator",
                "activated_at": "2026-08-18T00:00:00Z",
            },
        ),
        201,
    )

    # Round-trip 1: the name answers a revision, and the start writes it back.
    resolved = answered(
        client.get(f"{WORKFLOW_PATH}/by-name/{admitted['display_name']}"), 200
    )
    assert resolved["workflow_revision_hash"] == published["workflow_revision_hash"]

    # Round-trip 3: the catalog listing answers the revision and its format,
    # which is exactly the pair a start has to state.
    listing = answered(
        client.get(WORKFLOW_PATH, params={"view": "described", "limit": "50"}), 200
    )
    listed = next(
        item
        for item in listing["items"]
        if item["workflow_revision_hash"]
        == carried(resolved, "workflow_revision_hash")["workflow_revision_hash"]
    )
    detail = answered(
        client.get(f"{WORKFLOW_PATH}/{listed['workflow_revision_hash']}"), 200
    )
    graph = detail["graph"]
    assert graph["workflow_format_version"] == listed["workflow_format_version"]

    auth_profile = answered(
        client.post(
            AUTH_PROFILE_PATH,
            json={
                "profile_id": "max",
                "revision_number": 1,
                "provider_id": "exact",
                "auth_mode": "subscription",
            },
        ),
        201,
    )
    configuration = answered(
        client.post(
            AGENT_CONFIGURATION_PATH,
            json={
                "model": "opus",
                "executor_revision": "exact/v1",
                **carried(auth_profile, "auth_profile_revision_hash"),
            },
        ),
        201,
    )

    # Round-trip 4: material is published by its bytes and ordered by the
    # address that publication answered with.
    material = answered(
        client.post(
            ARTIFACT_PATH,
            content=ORDER_MATERIAL,
            headers={"content-type": "application/octet-stream"},
        ),
        201,
    )

    # The roles and the declared orders are lists of values rather than fields,
    # so they travel as values: the role the document binds, and the name the
    # document demands at start. The schema hull is the author's own
    # `schema: {ref, revision}` — carried, not rebuilt under other names.
    (role,) = graph["agent_roles"]
    (declared_order,) = graph["orders"]
    assert carried(declared_order["schema"], "ref", "revision") == {
        "ref": "order-schema",
        "revision": carried(order_schema, "schema_revision_hash")[
            "schema_revision_hash"
        ],
    }
    started = answered(
        client.post(
            RUN_PATH,
            json={
                "run_id": RUN_ID,
                **carried(listed, "workflow_revision_hash", "workflow_format_version"),
                "agent_bindings": [
                    {
                        "role": role,
                        **carried(configuration, "agent_configuration_revision_hash"),
                    }
                ],
                "orders": [
                    {
                        **carried(declared_order, "name"),
                        **carried(material, "artifact_hash"),
                    }
                ],
            },
        ),
        201,
    )

    runtime.launch()
    reference = carried(started, "public_run_reference")["public_run_reference"]
    waiting = read_until(client, reference, RunState.WAITING_INPUT)

    # Round-trip 2: the waiting run answers the revision it runs and the node
    # that owes a person a move, and the answer writes both back.
    accepted = answered(
        client.post(
            f"{RUN_PATH}/{reference}/answers",
            json={
                **carried(waiting, "workflow_revision_hash"),
                **carried(node_owing_a_move(waiting), "node_id"),
                "answer_base64": encode_canonical_base64(APPROVAL),
            },
        ),
        200,
        202,
    )
    assert accepted["run_id"] == started["run_id"]

    ended = read_until(client, reference, RunState.COMPLETED)
    assert ended["terminal_hash"] is not None
    assert [node["state"] for node in ended["node_rail"]] == [
        NodeState.SUCCEEDED.value
    ] * 3
