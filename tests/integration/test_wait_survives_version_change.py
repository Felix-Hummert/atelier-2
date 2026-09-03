"""A run parked on a Wait outlives the `application_version` it was started under.

**Why this file exists.** The auto-redeploy watcher (#1027) counted
`WAITING_INPUT` as active work and deferred every deploy behind an open
conversation, because a redeploy hands the new serve a fresh
`--application-version` and DBOS recovers a workflow only under the version that
enqueued it. That reasoning holds for a *running* workflow. A parked Wait is not
one: the node workflow that wrote `WAITING_INPUT` has already returned, and the
answer door enqueues the continuation itself, under the answering runtime's own
version (`DbosWaitAnswerer`). This test is the proof that the two facts add up --
without it the watcher's rule rests on an assumption nobody drove.

The run keeps no bound agent: a Wait-only document parks on a person with no
executor alive, so nothing but the version change can explain a stranded answer.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient
from httpx import Response

from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import runs
from atelier2.api.references import encode_public_run_reference
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from tests.scenarios.agents import agent_scratch_root
from tests.scenarios.api import durable_api_client
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)
from tests.scenarios.runs import (
    NO_AGENT_EXECUTORS,
    publish_pinned_revisions,
    start_published_v3_run,
)
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    V3_WAIT_LINE_DOCUMENT,
    V3_WAIT_LINE_NODE_ID,
)

RUN = RunId("wait/survives-a-version-change")
WORKFLOW = WorkflowRevision(V3_WAIT_LINE_DOCUMENT)
ANSWER = b'"approved after the redeploy"'
VERSION_THAT_PARKED_THE_WAIT = "executor-A"
VERSION_THAT_TAKES_THE_ANSWER = "executor-B"
STATE_TIMEOUT_SECONDS = 12.0
STATE_POLL_SECONDS = 0.025


def runtime_over(root: Path, application_version: str) -> DbosRuntime:
    """A runtime over the durable state in this directory, as a redeploy builds one."""
    return DbosRuntime(
        canonical_runtime_settings(root, application_version, agent_scratch_root(root)),
        canonical_loopback_effects(root),
        (),
    )


def wait_for_state(runtime: DbosRuntime, state: RunState) -> None:
    deadline = time.monotonic() + STATE_TIMEOUT_SECONDS
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
                )
            )
        if observed == state.value:
            return
        time.sleep(STATE_POLL_SECONDS)
    raise AssertionError(f"run stayed {observed!r}, expected {state.value!r}")


def answer_through_the_public_door(client: TestClient) -> Response:
    execution_id = NodeExecutionId.for_node(
        RUN, WORKFLOW.revision_hash, V3_WAIT_LINE_NODE_ID, 1
    )
    return client.post(
        f"/atelier/api/v1/runs/{encode_public_run_reference(RUN)}/answers",
        json={
            "workflow_revision_hash": WORKFLOW.revision_hash.value,
            "node_id": V3_WAIT_LINE_NODE_ID,
            "expected_node_execution_id": execution_id.value,
            "actor": "operator",
            "answer_base64": base64.b64encode(ANSWER).decode("ascii"),
        },
    )


def test_a_parked_wait_takes_its_answer_under_a_later_application_version(
    tmp_path: Path, dbos_logging_isolation: None
) -> None:
    parked = runtime_over(tmp_path, VERSION_THAT_PARKED_THE_WAIT)
    parked.initialize_storage()
    try:
        publish_pinned_revisions(parked.engine, ANY_JSON_SCHEMA)
        start_published_v3_run(
            parked.engine, parked.settings, RUN, WORKFLOW, NO_AGENT_EXECUTORS, roles=()
        )
        parked.launch()
        wait_for_state(parked, RunState.WAITING_INPUT)
    finally:
        parked.close()

    # The redeploy: the same durable store, a new process, a new version. The
    # answer door is the only thing that moves this run from here on.
    redeployed = runtime_over(tmp_path, VERSION_THAT_TAKES_THE_ANSWER)
    try:
        redeployed.launch()
        client = durable_api_client(redeployed)

        read_back = client.get(
            f"/atelier/api/v1/runs/{encode_public_run_reference(RUN)}"
        )
        assert read_back.status_code == 200, read_back.text
        assert read_back.json()["state"] == RunState.WAITING_INPUT.value

        accepted = answer_through_the_public_door(client)
        assert accepted.status_code == 202, accepted.text

        wait_for_state(redeployed, RunState.COMPLETED)
        proceeded = client.get(
            f"/atelier/api/v1/runs/{encode_public_run_reference(RUN)}"
        )
        assert proceeded.status_code == 200, proceeded.text
        assert proceeded.json()["state"] == RunState.COMPLETED.value
    finally:
        redeployed.close()
