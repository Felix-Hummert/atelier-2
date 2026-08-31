"""The real conductor document, driven as a conversation through the public doors.

**Why this file exists.** `tests/integration/test_v3_wait_in_loop.py` proved the
generic machinery: a `loop{wait, agent}` document may start on its Wait, take an
answer as a same-round input, read a previous round through the self-edge, and
end honestly at its declared ceiling. What that test does not prove is that
`atelier2.host.conductor_workflow.conductor_workflow_document` -- the actual
document `atelier serve` publishes -- really builds that shape: its own node ids,
its own round ceiling (`CONDUCTOR_LOOP_MAXIMUM_ROUNDS`), and its own report
contract (`CONDUCTOR_REPORT_SCHEMA`, with `carried_context` and the honesty
marker). This file drives the real builder's output, not a stand-in.

**Why one test.** A run started through the public API rests on the Wait in
round one; the operator's first message becomes that round's input with no
`previous_report` yet (honestly absent); the second round's agent reads its own
first report through the previous-round self-edge; and the conversation's own
named ceiling -- not a verdict -- ends the run `COMPLETED`. Driving the
conductor's real 24-round cap end to end is what tells the difference between
"the machinery can do this" and "the conductor document does this": both facts
about the real, hardcoded ceiling this builder ships.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path

import sqlalchemy as sa
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.app import create_app
from atelier2.api.references import encode_public_run_reference
from atelier2.application.compose_node_job import RESULT_HEADING
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.host.conductor_workflow import (
    CONDUCTOR_AGENT_NODE_ID,
    CONDUCTOR_LOOP_MAXIMUM_ROUNDS,
    CONDUCTOR_MESSAGE_SCHEMA,
    CONDUCTOR_REPORT_SCHEMA,
    CONDUCTOR_ROLE,
    CONDUCTOR_WAIT_NODE_ID,
    conductor_workflow_document,
)
from atelier2.ports.agent_configurations import AgentConfigurationRevisionCreated
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    answering_each_execution,
    publish_checked_model_registry,
)
from tests.scenarios.api import api_limits, durable_ports, event_poll_backoff

RUN = RunId("conductor/conversation")


def message(round_ordinal: int) -> bytes:
    """The operator's round-`round_ordinal` message, as the Wait's JSON answer."""
    return json.dumps(f"round {round_ordinal} request").encode()


def report(round_ordinal: int) -> bytes:
    """The agent's round-`round_ordinal` report, valid under `CONDUCTOR_REPORT_SCHEMA`."""
    return json.dumps(
        {
            "answer": f"round {round_ordinal} done",
            "started_run_ids": [],
            "carried_context": f"context carried after round {round_ordinal}",
            "carried_context_truncated": False,
        }
    ).encode()


def provider() -> RecordingAgentExecutorFactoryV2:
    """The doors-capable executor, answering every round's agent node in turn."""
    return RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        report(1),
        capability_set=frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS}),
        command=answering_each_execution(
            {
                (CONDUCTOR_AGENT_NODE_ID, round_ordinal): report(round_ordinal)
                for round_ordinal in range(1, CONDUCTOR_LOOP_MAXIMUM_ROUNDS + 1)
            }
        ),
    )


def runtime_over(root: Path, recording: RecordingAgentExecutorFactoryV2) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "conductor-loop-test",
            agent_scratch_root=agent_scratch_root(root),
        ),
        LoopbackEffectAdapterFactory(
            root / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (recording,),
    )


def public_client(runtime: DbosRuntime) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=durable_ports(
                runtime.engine,
                runtime.settings,
                runtime.agent_executor_registry,
            ),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )


def publish_and_start(runtime: DbosRuntime) -> WorkflowRevision:
    """Publish the real conductor document and start a run on its Wait."""
    catalog_store = DbosCatalogStore(runtime.engine)
    message_schema = catalog_store.publish_revision(
        PublishedRevision(RevisionKind.SCHEMA, CONDUCTOR_MESSAGE_SCHEMA)
    )
    report_schema = catalog_store.publish_revision(
        PublishedRevision(RevisionKind.SCHEMA, CONDUCTOR_REPORT_SCHEMA)
    )
    assert isinstance(
        message_schema, (PublishedRevisionCreated, PublishedRevisionExisting)
    )
    assert isinstance(
        report_schema, (PublishedRevisionCreated, PublishedRevisionExisting)
    )
    document = conductor_workflow_document(
        message_schema.revision.revision_hash.value,
        report_schema.revision.revision_hash.value,
    )

    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    catalog.publish_auth_profile_revision(auth)
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS_WITH_TOOLS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )

    workflow = WorkflowRevision(document)
    client = public_client(runtime)
    published_response = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=document,
        headers={"content-type": "application/yaml"},
    )
    assert published_response.status_code in (200, 201), published_response.text
    started_response = client.post(
        "/atelier/api/v1/runs",
        json={
            "workflow_format_version": 3,
            "run_id": RUN.value,
            "workflow_revision_hash": workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": CONDUCTOR_ROLE,
                    "agent_configuration_revision_hash": configuration.revision_hash.value,
                }
            ],
            "orders": [],
        },
    )
    assert started_response.status_code == 201, started_response.text
    return workflow


def answer(client: TestClient, workflow: WorkflowRevision, round_ordinal: int) -> None:
    execution_id = NodeExecutionId.for_node(
        RUN, workflow.revision_hash, CONDUCTOR_WAIT_NODE_ID, round_ordinal
    )
    response = client.post(
        f"/atelier/api/v1/runs/{encode_public_run_reference(RUN)}/answers",
        json={
            "workflow_revision_hash": workflow.revision_hash.value,
            "node_id": CONDUCTOR_WAIT_NODE_ID,
            "expected_node_execution_id": execution_id.value,
            "actor": "operator",
            "answer_base64": base64.b64encode(message(round_ordinal)).decode("ascii"),
        },
    )
    assert response.status_code == 202, response.text


def wait_for_waiting_round(runtime: DbosRuntime, round_ordinal: int) -> None:
    deadline = time.monotonic() + 12
    observed: tuple[str, int | None] = ("", None)
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            row = connection.execute(
                sa.select(runs.c.state, runs.c.current_round_ordinal).where(
                    runs.c.run_id == RUN.value
                )
            ).one()
        observed = (str(row.state), int(row.current_round_ordinal))
        if observed == (RunState.WAITING_INPUT.value, round_ordinal):
            return
        time.sleep(0.025)
    raise AssertionError(
        f"run stayed {observed!r}, expected waiting in round {round_ordinal}"
    )


def wait_for_state(runtime: DbosRuntime, state: RunState) -> None:
    deadline = time.monotonic() + 12
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
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed!r}, expected {state.value!r}")


def test_a_conductor_run_answers_rounds_and_ends_at_its_named_cap(
    tmp_path: Path,
    dbos_logging_isolation: None,
) -> None:
    recording = provider()
    runtime = runtime_over(tmp_path, recording)
    runtime.initialize_storage()
    try:
        workflow = publish_and_start(runtime)
        client = public_client(runtime)
        runtime.launch()

        # The run's very first node is the Wait itself: nothing runs before the
        # operator's first message.
        wait_for_waiting_round(runtime, 1)

        answer(client, workflow, 1)
        wait_for_waiting_round(runtime, 2)
        assert recording.opened is not None
        round_one_job = recording.opened.requests[0].job_bytes.decode("utf-8")
        assert (
            RESULT_HEADING.format(node=CONDUCTOR_WAIT_NODE_ID, name="message")
            in round_one_job
        )
        assert "round 1 request" in round_one_job
        assert (
            RESULT_HEADING.format(node=CONDUCTOR_AGENT_NODE_ID, name="report")
            not in round_one_job
        )

        # Round two's agent reads its own round-one report through the
        # previous-round self-edge -- present now, absent one round ago. The
        # executor is opened once for the whole run, so its requests accumulate
        # one per round in order.
        answer(client, workflow, 2)
        wait_for_waiting_round(runtime, 3)
        assert recording.opened is not None
        round_two_job = recording.opened.requests[1].job_bytes.decode("utf-8")
        assert (
            RESULT_HEADING.format(node=CONDUCTOR_WAIT_NODE_ID, name="message")
            in round_two_job
        )
        assert "round 2 request" in round_two_job
        assert (
            RESULT_HEADING.format(node=CONDUCTOR_AGENT_NODE_ID, name="report")
            in round_two_job
        )
        assert "context carried after round 1" in round_two_job

        # Every further round answers and turns the loop again, until the
        # conductor's own named ceiling -- not a verdict -- ends the run.
        for round_ordinal in range(3, CONDUCTOR_LOOP_MAXIMUM_ROUNDS + 1):
            answer(client, workflow, round_ordinal)
            if round_ordinal < CONDUCTOR_LOOP_MAXIMUM_ROUNDS:
                wait_for_waiting_round(runtime, round_ordinal + 1)
        wait_for_state(runtime, RunState.COMPLETED)

        with runtime.engine.connect() as connection:
            head = (
                connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
                .mappings()
                .one()
            )
        assert int(head["current_round_ordinal"]) == CONDUCTOR_LOOP_MAXIMUM_ROUNDS
        assert str(head["current_node_id"]) == CONDUCTOR_AGENT_NODE_ID
    finally:
        runtime.close()
