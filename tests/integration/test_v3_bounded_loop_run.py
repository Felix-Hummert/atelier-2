"""A declared loop turns its rounds by itself, and each round is its own execution.

Every V3 head before this one moved a run forwards along the edges its author
wrote. This is the first head where a run goes *back*: a document declares that a
stretch of its graph repeats, and the engine runs that stretch again under an
identity the round owns. Nothing here reaches into the engine — the loop is
declared, the run is started through the public start seam, `launch()` hands it
to the real queue, and what is asserted is what an operator would see afterwards.

This loop declares no verdict, so the bound is the only exit it has, and that is
exactly what is under test: the rounds run to the declared end and the run ends
there. A loop whose own answer ends it earlier is the verdict-steered kind, and
`test_v3_verdict_exit_run.py` is where that is proven.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from pathlib import Path
from threading import Event

import pytest
import sqlalchemy as sa
from dbos import DBOS

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
    AgentExecutionRequestV2,
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
from atelier2.contracts.run_projections import NodeState, RunPage
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.agent_executions import AgentProcessCommand
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    StartPublishedRunRequest,
    StartPublishedRunRequestV2,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_events import RunEventPage, StreamReady
from atelier2.ports.run_queries import NodeDetailFound, RunFound, RunReceiptsFound
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    emitting,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    LOOPED_LINE_DOCUMENT,
    LOOPED_LINE_MAXIMUM_ROUNDS,
    LOOPED_LINE_NODE_IDS,
)

RUN = RunId("v3/build-review-loop")
HEALTHY_RUN = RunId("v1/healthy-query-peer")
PROVIDER_OUTPUT = b'"the exact provider bytes"'
EXPECTED_ROUNDS = tuple(range(1, LOOPED_LINE_MAXIMUM_ROUNDS + 1))
HEALTHY_DOCUMENT = b"""format_version: 1
start: final
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
"""


def gate_execution(
    run_id: RunId,
    node_id: str,
    round_ordinal: int,
    output: bytes = PROVIDER_OUTPUT,
) -> tuple[
    Event,
    Event,
    Callable[[AgentExecutionRequestV2], AgentProcessCommand],
]:
    """Hold one real execution at the provider boundary without polling."""
    entered = Event()
    release = Event()

    def command(request: AgentExecutionRequestV2) -> AgentProcessCommand:
        selected = (
            request.run_id == run_id
            and request.node_id == node_id
            and request.round_ordinal == round_ordinal
        )
        if selected:
            entered.set()
            if not release.wait(timeout=10):
                raise AssertionError("the selected execution was never released")
        return emitting(output if selected else PROVIDER_OUTPUT)(request)

    return entered, release, command


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


def start_loop(runtime: DbosRuntime, run_id: RunId = RUN) -> WorkflowRevision:
    workflow, bindings = publish_looped_line(runtime)
    started = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, bindings)
    )
    assert isinstance(started, DurableRunCreated), started
    return workflow


def start_healthy_peer(runtime: DbosRuntime) -> None:
    workflow = WorkflowRevision(HEALTHY_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    started = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(StartPublishedRunRequest(HEALTHY_RUN, workflow.revision_hash))
    assert isinstance(started, DurableRunCreated), started


def finish_gated_node(
    run_id: RunId,
    workflow: WorkflowRevision,
    node_id: str,
    round_ordinal: int,
    release: Event,
) -> None:
    release.set()
    execution = NodeExecutionId.for_node(
        run_id, workflow.revision_hash, node_id, round_ordinal
    )
    DBOS.retrieve_workflow(node_workflow_id_for(execution)).get_result()


def start_and_run(runtime: DbosRuntime) -> WorkflowRevision:
    workflow = start_loop(runtime)
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


@pytest.mark.proves("round-sensitive-run-views-preserve-current-execution-truth")
def test_a_live_second_round_and_its_healthy_peer_remain_readable(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    started_runtime, recording = runtime
    entered, release, command = gate_execution(RUN, "implement", 2)
    assert recording.opened is not None
    recording.opened.command = command
    workflow = start_loop(started_runtime)
    start_healthy_peer(started_runtime)
    started_runtime.launch()
    assert entered.wait(timeout=10), "the loop never entered round two"
    queries = durable_queries(started_runtime.engine)
    execution = NodeExecutionId.for_node(RUN, workflow.revision_hash, "implement", 2)

    try:
        found = queries.get_run(RUN)
        listed = queries.list_runs(None, 10)
        detailed = queries.get_node_detail(RUN, "implement")
        receipts = queries.list_run_receipts(RUN)
        prepared = queries.prepare_run_event_stream(RUN, 0)
        events = queries.read_run_event_page(RUN, 0, 10)

        assert isinstance(found, RunFound), found
        assert found.projection.run.current_round_ordinal == 2
        assert found.projection.current_agent_attempt is not None
        assert found.projection.current_agent_attempt.node_execution_id == execution
        assert isinstance(listed, RunPage), listed
        assert {item.run.run_id for item in listed.runs} == {RUN, HEALTHY_RUN}
        loop = next(item for item in listed.runs if item.run.run_id == RUN)
        assert loop.current_agent_attempt is not None
        assert loop.current_agent_attempt.node_execution_id == execution
        assert isinstance(detailed, NodeDetailFound), detailed
        assert detailed.detail.state is NodeState.WORKING
        assert detailed.detail.answer is None
        assert detailed.detail.provenance is None
        assert detailed.detail.started_at is not None
        assert detailed.detail.ended_at is None
        assert isinstance(receipts, RunReceiptsFound), receipts
        assert {(item.node_id, item.round_ordinal) for item in receipts.items} == {
            (node_id, 1) for node_id in LOOPED_LINE_NODE_IDS
        }
        assert prepared == StreamReady(2, False, 0)
        assert isinstance(events, RunEventPage), events
        assert [
            (item.event.node_id, item.event.round_ordinal) for item in events.events
        ] == [(node_id, 1) for node_id in LOOPED_LINE_NODE_IDS]
        assert not events.terminal_seen
    finally:
        finish_gated_node(RUN, workflow, "implement", 2, release)


@pytest.mark.proves("round-sensitive-run-views-preserve-current-execution-truth")
def test_a_completed_three_round_loop_has_one_public_query_truth(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    started_runtime, recording = runtime
    entered, release, command = gate_execution(
        RUN, "review", LOOPED_LINE_MAXIMUM_ROUNDS
    )
    assert recording.opened is not None
    recording.opened.command = command
    workflow = start_loop(started_runtime)
    started_runtime.launch()
    assert entered.wait(timeout=10), "the loop never entered its final node"
    finish_gated_node(RUN, workflow, "review", LOOPED_LINE_MAXIMUM_ROUNDS, release)
    queries = durable_queries(started_runtime.engine)

    found = queries.get_run(RUN)
    listed = queries.list_runs(None, 10)
    detailed = queries.get_node_detail(RUN, "review")
    receipts = queries.list_run_receipts(RUN)
    prepared = queries.prepare_run_event_stream(RUN, 0)
    events = queries.read_run_event_page(RUN, 0, 10)

    assert isinstance(found, RunFound), found
    assert found.projection.run.state is RunState.COMPLETED
    assert found.projection.run.current_round_ordinal == LOOPED_LINE_MAXIMUM_ROUNDS
    assert isinstance(listed, RunPage), listed
    assert [item.run.run_id for item in listed.runs] == [RUN]
    assert isinstance(detailed, NodeDetailFound), detailed
    handed = next(
        request
        for request in recording.opened.requests
        if request.node_id == "review"
        and request.round_ordinal == LOOPED_LINE_MAXIMUM_ROUNDS
    )
    assert detailed.detail.state is NodeState.SUCCEEDED
    assert detailed.detail.job == handed.job_bytes
    assert detailed.detail.answer is not None
    assert detailed.detail.answer.value == PROVIDER_OUTPUT
    assert detailed.detail.provenance is not None
    assert detailed.detail.provenance.request_hash == handed.request_hash.value
    assert detailed.detail.refusal is None
    assert detailed.detail.started_at is not None
    assert detailed.detail.ended_at is not None
    assert detailed.detail.started_at.value <= detailed.detail.ended_at.value
    assert isinstance(receipts, RunReceiptsFound), receipts
    assert {item.node_execution_id.value for item in receipts.items} == set(
        executions_of(workflow)
    )
    assert prepared == StreamReady(6, True, 0)
    assert isinstance(events, RunEventPage), events
    assert [item.event.event_sequence for item in events.events] == list(range(1, 7))
    assert [item.event.round_ordinal for item in events.events] == [1, 1, 2, 2, 3, 3]
    assert events.terminal_seen


@pytest.mark.proves("round-sensitive-run-views-preserve-current-execution-truth")
def test_a_later_round_failure_keeps_its_exact_public_refusal(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    started_runtime, recording = runtime
    entered, release, command = gate_execution(RUN, "implement", 2, b"not a JSON value")
    assert recording.opened is not None
    recording.opened.command = command
    workflow = start_loop(started_runtime)
    started_runtime.launch()
    assert entered.wait(timeout=10), "the loop never entered its failing round"
    finish_gated_node(RUN, workflow, "implement", 2, release)
    queries = durable_queries(started_runtime.engine)
    execution = NodeExecutionId.for_node(RUN, workflow.revision_hash, "implement", 2)

    found = queries.get_run(RUN)
    listed = queries.list_runs(None, 10)
    detailed = queries.get_node_detail(RUN, "implement")
    receipts = queries.list_run_receipts(RUN)
    prepared = queries.prepare_run_event_stream(RUN, 0)
    events = queries.read_run_event_page(RUN, 0, 10)

    assert isinstance(found, RunFound), found
    assert found.projection.run.state is RunState.FAILED
    assert found.projection.current_agent_attempt is not None
    assert found.projection.current_agent_attempt.node_execution_id == execution
    assert isinstance(listed, RunPage), listed
    assert listed.runs[0].current_agent_attempt is not None
    assert listed.runs[0].current_agent_attempt.node_execution_id == execution
    assert isinstance(detailed, NodeDetailFound), detailed
    assert detailed.detail.state is NodeState.FAILED
    assert detailed.detail.answer is None
    assert detailed.detail.provenance is None
    assert detailed.detail.refusal is not None
    assert "output-schema-refused" in detailed.detail.refusal
    assert detailed.detail.started_at is not None
    assert detailed.detail.ended_at is not None
    assert isinstance(receipts, RunReceiptsFound), receipts
    assert {(item.node_id, item.round_ordinal) for item in receipts.items} == {
        (node_id, 1) for node_id in LOOPED_LINE_NODE_IDS
    }
    assert prepared == StreamReady(3, True, 0)
    assert isinstance(events, RunEventPage), events
    assert events.terminal_seen
    assert events.events[-1].event.node_execution_id == execution
    assert events.events[-1].node_receipt_reason == detailed.detail.refusal


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
