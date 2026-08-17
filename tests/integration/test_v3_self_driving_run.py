"""A V3 line drives itself from its public start to its terminal hash.

Every earlier V3 head proved one joint and held the run still around it: the
document was admitted (#194 H1a), a node bound onto the attempt path (H1c), the
terminal condition left the subworkflow node (H1b), and a node named the heir its
author declared (H2). Each was measured by calling the piece directly, because
the runtime refused the family at the start seam and no line could move on its
own.

This is the head where nothing is held. The run is started through the public
route, `launch()` hands it to the real DBOS queue, and no test code touches
`start_node`, `durable_node` or the attempt store afterwards. What is asserted is
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
from atelier2.adapters.dbos.schema import agent_attempts, run_events, runs
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
from atelier2.contracts.executions import RunEventKind, terminal_hash_for
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
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2

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
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    """A runtime whose agent executor succeeds, so the line can actually move."""
    started = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "v3-driver-test"),
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
    runtime: DbosRuntime,
) -> None:
    """The whole point of the family: started once, it finishes by itself.

    The only calls this test makes are the ones an operator makes -- publish,
    start, launch. Everything between the start and COMPLETED is the runtime's
    own chain: the bootstrap picks up the entry node, the attempt runs, the
    completion asks for the heir the author declared, and the sink ends the run.
    """
    workflow, bindings = publish_two_node_line(runtime)

    started = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated)

    runtime.launch()
    wait_for_state(runtime, RunState.COMPLETED)

    with runtime.engine.connect() as connection:
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

    assert events == [
        (1, "implement", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
        (2, "review", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
    ]
    assert attempts == [
        ("implement", AgentAttemptState.SUCCEEDED.value),
        ("review", AgentAttemptState.SUCCEEDED.value),
    ]
    assert str(run["current_node_id"]) == "review"
    assert int(str(run["workflow_format_version"])) == 3
    assert (
        str(run["terminal_hash"])
        == terminal_hash_for(workflow.revision_hash, event_hashes).value
    )
