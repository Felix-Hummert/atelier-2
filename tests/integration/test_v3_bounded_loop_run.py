"""A declared loop turns its rounds by itself, and each round is its own execution.

Every V3 head before this one moved a run forwards along the edges its author
wrote. This is the first head where a run goes *back*: a document declares that a
stretch of its graph repeats, and the engine runs that stretch again under an
identity the round owns. Nothing here reaches into the engine — the loop is
declared, the run is started through the public start seam, `launch()` hands it
to the real queue, and what is asserted is what an operator would see afterwards.

What is deliberately not proven here, because this head does not build it: a
round that ends the loop early. Reaching the declared bound is the only way out
of a loop this build has, and a result that says "green" steering the next edge
is the verdict-driven continuation of #25's second head.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_receipts_v2,
    node_artifacts_v3,
    node_execution_requests_v3,
    node_receipts_v3,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow_ids import node_workflow_id_for
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
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
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    LOOPED_LINE_DOCUMENT,
    LOOPED_LINE_MAXIMUM_ROUNDS,
    LOOPED_LINE_NODE_IDS,
)

RUN = RunId("v3/build-review-loop")
PROVIDER_OUTPUT = b'"the exact provider bytes"'
EXPECTED_ROUNDS = tuple(range(1, LOOPED_LINE_MAXIMUM_ROUNDS + 1))


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    recording = RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
    )
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-loop-test",
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


def publish_looped_line(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    published = DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
    assert isinstance(
        published, (PublishedRevisionCreated, PublishedRevisionExisting)
    ), published
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
    workflow = WorkflowRevision(LOOPED_LINE_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def start_and_run(runtime: DbosRuntime) -> WorkflowRevision:
    workflow, bindings = publish_looped_line(runtime)
    started = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(started, DurableRunCreated), started
    runtime.launch()
    wait_for_state(runtime, RunState.COMPLETED)
    return workflow


def wait_for_state(runtime: DbosRuntime, state: RunState) -> None:
    deadline = time.monotonic() + 16
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


def executions_of(workflow: WorkflowRevision) -> dict[str, tuple[str, int]]:
    """Every node execution this document declares, by its identity."""
    return {
        NodeExecutionId.for_node(
            RUN, workflow.revision_hash, node_id, round_ordinal
        ).value: (node_id, round_ordinal)
        for round_ordinal in EXPECTED_ROUNDS
        for node_id in LOOPED_LINE_NODE_IDS
    }


@pytest.mark.proves("a-declared-loop-runs-its-rounds-and-ends-at-its-bound")
def test_a_two_node_loop_turns_until_its_declared_bound_and_then_ends(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The run goes back to the loop's first node until the bound is reached.

    The order is the whole claim: every round runs the body from its head, in
    the order the edges declare, and the run ends on the sink of the last round
    rather than on the first time the sink is reached.
    """
    started_runtime, recording = runtime

    start_and_run(started_runtime)

    with started_runtime.engine.connect() as connection:
        events = [
            (
                int(str(record["event_sequence"])),
                str(record["node_id"]),
                int(str(record["round_ordinal"])),
                str(record["event_kind"]),
            )
            for record in connection.execute(
                sa.select(run_events)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).mappings()
        ]
        head = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )

    assert events == [
        (
            round_ordinal * len(LOOPED_LINE_NODE_IDS)
            - len(LOOPED_LINE_NODE_IDS)
            + turn,
            node_id,
            round_ordinal,
            RunEventKind.AGENT_COMPLETED.value,
        )
        for round_ordinal in EXPECTED_ROUNDS
        for turn, node_id in enumerate(LOOPED_LINE_NODE_IDS, start=1)
    ]
    assert str(head["current_node_id"]) == LOOPED_LINE_NODE_IDS[-1]
    assert int(str(head["current_round_ordinal"])) == LOOPED_LINE_MAXIMUM_ROUNDS
    assert recording.opened is not None
    assert [request.node_id for request in recording.opened.requests] == [
        node_id for _ in EXPECTED_ROUNDS for node_id in LOOPED_LINE_NODE_IDS
    ]


@pytest.mark.proves("every-round-of-a-loop-is-its-own-durable-execution")
def test_each_round_leaves_its_own_receipt_output_and_durable_workflow(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A round is an execution, not a repeat: it owes its own evidence.

    One receipt, one artifact and one durable node workflow per round of per
    node — and every one of them addressed by the identity that round has, so no
    round can answer for another.
    """
    started_runtime, _ = runtime

    workflow = start_and_run(started_runtime)
    declared = executions_of(workflow)

    with started_runtime.engine.connect() as connection:
        receipted = set(
            connection.execute(
                sa.select(node_receipts_v3.c.node_execution_id)
            ).scalars()
        )
        requested = set(
            connection.execute(
                sa.select(node_execution_requests_v3.c.node_execution_id)
            ).scalars()
        )
        artifacts = {
            (str(record["node_execution_id"]), str(record["node_id"]))
            for record in connection.execute(
                sa.select(node_artifacts_v3).where(
                    node_artifacts_v3.c.run_id == RUN.value
                )
            ).mappings()
        }
        agent_rounds = sorted(
            (str(record["node_id"]), int(str(record["round_ordinal"])))
            for record in connection.execute(
                sa.select(agent_receipts_v2).where(
                    agent_receipts_v2.c.run_id == RUN.value
                )
            ).mappings()
        )
        node_workflows = set(
            connection.execute(
                sa.text(
                    "SELECT workflow_uuid FROM workflow_status "
                    "WHERE name='atelier2_graph_node'"
                )
            ).scalars()
        )

    assert receipted == set(declared)
    assert requested == set(declared)
    assert artifacts == {
        (execution, node_id) for execution, (node_id, _) in declared.items()
    }
    assert agent_rounds == sorted(declared.values())
    assert node_workflows == {
        node_workflow_id_for(NodeExecutionId(execution)) for execution in declared
    }


@pytest.mark.proves("every-round-of-a-loop-is-its-own-durable-execution")
def test_the_terminal_hash_of_a_looped_run_recomputes_over_every_round(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The chain carries the rounds, so no round can be dropped unnoticed.

    The round is inside each event's execution identity, which the event hash
    already binds — so a terminal hash that recomputes over the stored events is
    a statement about which rounds actually ran.
    """
    started_runtime, _ = runtime

    workflow = start_and_run(started_runtime)

    with started_runtime.engine.connect() as connection:
        stored_terminal = str(
            connection.scalar(
                sa.select(runs.c.terminal_hash).where(runs.c.run_id == RUN.value)
            )
        )
        event_hashes = tuple(
            Sha256Hash(str(value))
            for value in connection.execute(
                sa.select(run_events.c.event_hash)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).scalars()
        )

    assert len(event_hashes) == LOOPED_LINE_MAXIMUM_ROUNDS * len(LOOPED_LINE_NODE_IDS)
    assert (
        terminal_hash_for(workflow.revision_hash, event_hashes).value == stored_terminal
    )
