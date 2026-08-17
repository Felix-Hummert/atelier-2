"""A V3 line drives itself from its public start to its terminal hash.

Every earlier V3 head proved one joint and held the run still around it: the
document was admitted (#194 H1a), a node bound onto the attempt path (H1c), the
terminal condition left the subworkflow node (H1b), and a node named the heir its
author declared (H2). Each was measured by calling the piece directly, because
the runtime refused the family at the start seam and no line could move on its
own.

This is the head where nothing is held. The run is started through the one public
start seam, `launch()` hands it to the real DBOS queue, and no test code touches
`start_node`, `durable_node` or the attempt store afterwards. This line said "the
public route" while a named stopgap refused a V3 revision over HTTP, which made it
false; #219 removed that refusal, so the route would answer now too. It still says
seam rather than route, because that is what this test drives -- the HTTP claim is
proven where the route is actually called. What is asserted is
what an operator would see: both nodes ran, in the order the author declared, and
the run ended on a terminal hash that recomputes from the events it wrote.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agent_attempts import AgentAttemptState
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    node_workflow_id_for,
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from atelier2.ports.run_events import StreamReady
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.api import durable_queries

TWO_NODE_DOCUMENT = b"""format_version: 3
name: Two agents in a line
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Check what the node before you did.
    depends_on: [implement]
"""

RUN = RunId("v3/two-agents")
PROVIDER_OUTPUT = b"the exact provider bytes"


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    """A runtime whose agent executor succeeds, so the line can actually move."""
    recording = RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
    )
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-driver-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (recording,),
    )
    started.initialize_storage()
    try:
        yield started, recording
    finally:
        started.close()


def publish_two_node_line(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("exact"), AuthMode.SUBSCRIPTION)
    assert isinstance(
        catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
    )
    configuration = AgentConfigurationRevision(
        "opus",
        auth.revision_hash,
        AgentExecutorRevision("exact/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    assert isinstance(
        catalog.publish_agent_configuration_revision(configuration),
        AgentConfigurationRevisionCreated,
    )
    workflow = WorkflowRevision(TWO_NODE_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def wait_for_state(runtime: DbosRuntime, state: RunState) -> None:
    deadline = time.monotonic() + 8
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


@pytest.mark.proves("a-v3-run-drives-itself-through-the-runtime")
def test_a_v3_line_runs_both_its_nodes_without_a_hand_reaching_in(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The whole point of the family: started once, it finishes by itself.

    The only calls this test makes are the ones an operator makes -- publish,
    start, launch. Everything between the start and COMPLETED is the runtime's
    own chain: the bootstrap picks up the entry node, the attempt runs, the
    completion asks for the heir the author declared, and the sink ends the run.
    """
    started_runtime, recording = runtime
    workflow, bindings = publish_two_node_line(started_runtime)

    started = DbosDurableRunStarter(
        started_runtime.engine,
        started_runtime.settings,
        started_runtime.agent_executor_registry,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated)

    started_runtime.launch()
    wait_for_state(started_runtime, RunState.COMPLETED)

    with started_runtime.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        events = [
            (
                int(str(record["event_sequence"])),
                str(record["node_id"]),
                str(record["event_kind"]),
                bytes(record["payload"]),
            )
            for record in connection.execute(
                sa.select(run_events)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).mappings()
        ]
        attempts = [
            (str(record["node_id"]), str(record["state"]))
            for record in connection.execute(
                sa.select(agent_attempts)
                .where(agent_attempts.c.run_id == RUN.value)
                .order_by(agent_attempts.c.node_id)
            ).mappings()
        ]
        event_hashes = tuple(
            Sha256Hash(str(value))
            for value in connection.execute(
                sa.select(run_events.c.event_hash)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).scalars()
        )
        receipt_count = connection.scalar(
            sa.select(sa.func.count()).select_from(agent_receipts_v2)
        )
        node_workflows = tuple(
            connection.execute(
                sa.text(
                    "SELECT workflow_uuid FROM workflow_status "
                    "WHERE name='atelier2_graph_node' ORDER BY workflow_uuid"
                )
            ).scalars()
        )

    assert events == [
        (1, "implement", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
        (2, "review", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
    ]
    assert attempts == [
        ("implement", AgentAttemptState.SUCCEEDED.value),
        ("review", AgentAttemptState.SUCCEEDED.value),
    ]
    assert recording.opened is not None
    assert [request.node_id for request in recording.opened.requests] == [
        "implement",
        "review",
    ]
    assert receipt_count == 2
    assert node_workflows == tuple(
        sorted(
            node_workflow_id_for(
                NodeExecutionId.for_node(RUN, workflow.revision_hash, node_id)
            )
            for node_id in ("implement", "review")
        )
    )
    assert str(run["current_node_id"]) == "review"
    assert int(str(run["workflow_format_version"])) == 3
    assert (
        str(run["terminal_hash"])
        == terminal_hash_for(workflow.revision_hash, event_hashes).value
    )


def test_the_finished_line_can_have_its_events_read_back(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A run that ended is a run whose history opens, not one reported corrupt.

    The stream's pre-flight decides whether the head event is the one that ended
    the run. It knew a single spelling of an ending -- the subworkflow completion
    a V1 or V2 line ends on -- and a V3 line ends on its **agent** sink, because
    #194 H1b lifted the terminal condition off the subworkflow node onto the run.
    So every finished V3 run answered `durable-state-corrupt`: the loudest thing
    this system can say, about a store that is intact.

    Measured on the operator's own first V3 run before this repair, where the run
    itself read back at 200 while its events answered 500. The cockpit opens this
    stream for the run it is showing, so the page rendered and its live view fell
    straight to disconnected.
    """
    started, _ = runtime
    workflow, bindings = publish_two_node_line(started)
    DbosDurableRunStarter(
        started.engine, started.settings, started.agent_executor_registry
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    started.launch()
    wait_for_state(started, RunState.COMPLETED)

    prepared = durable_queries(started.engine).prepare_run_event_stream(RUN, 0)

    assert isinstance(prepared, StreamReady), prepared
    assert prepared.terminal is True
    assert prepared.head_sequence == 2
