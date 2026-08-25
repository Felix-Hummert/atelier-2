"""A V3 line stops for a person, survives as a stopped run, and is carried on.

The agent half of #194 proved a line that runs to its end without a hand
reaching in. This is the half where a hand is exactly the point: the runtime
reaches a Wait node, writes WAITING_INPUT durably and stops -- no queue work
pending, nothing polling -- and the run stays there until an answer arrives
through the surface an operator actually uses. The answer then carries it to the
run's terminal hash.

What is driven here is the answer use case, one layer under the route that calls
it; #219 owns the HTTP resource and proves the route itself. What is asserted is
what an operator would see: the pause, the two named refusals around it, and the
line finishing on a hash that recomputes from the events the run wrote.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from dbos import DBOS

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.names import ANSWER_WORKFLOW_NAME
from atelier2.adapters.dbos.run_store import (
    DbosWaitAnswerer,
    commit_wait_answered,
    load_wait_answer,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import run_events, runs, wait_answers
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow_ids import node_workflow_id_for
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.projection.runs import run_resource
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    AnswerStateConflict,
    UnanswerableWait,
    answer_wait_result,
)
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
    WaitAnswerState,
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import NodeDetailFound, RunFound
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

APPROVAL_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b'{"type": "string"}')
"""The contract the wait node pins, narrow enough to refuse a wrong answer.

The pause is the subject here, so the schema is the smallest one that can say no
to something: a JSON string is admitted and every other JSON value is not. A
schema admitting everything would make the refusal below unprovable.
"""

WAIT_IN_THE_MIDDLE = (
    b"""format_version: 3
name: A person approves between two agents
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + declared_output()
    + b"""  - id: approve
    type: wait
    prompt: Approve this candidate, or name the blocking defect.
    depends_on: [implement]
"""
    + declared_output(APPROVAL_SCHEMA, "approval")
    + b"""  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Check what the person approved.
    depends_on: [approve]
"""
    + declared_output()
)

WAIT_AS_THE_SINK = (
    b"""format_version: 3
name: A person approves last
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""
    + declared_output()
    + b"""  - id: approve
    type: wait
    prompt: Approve this candidate, or name the blocking defect.
    depends_on: [implement]
"""
    + declared_output(APPROVAL_SCHEMA, "approval")
)

RUN = RunId("v3/a-person-approves")
PROVIDER_OUTPUT = b'"the exact provider bytes"'
ANSWER = b'"approved"'
WAIT_NODE = "approve"


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    """A runtime whose agent executor succeeds, so the line reaches the pause."""
    recording = RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-operation", PROVIDER_OUTPUT
    )
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-wait-test",
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


def start_and_launch(runtime: DbosRuntime, document: bytes) -> WorkflowRevision:
    """Publish everything the document pins, start the run, and let it drive."""
    catalog_store = DbosCatalogStore(runtime.engine)
    for schema in (ANY_JSON_SCHEMA, APPROVAL_SCHEMA):
        assert isinstance(
            catalog_store.publish_revision(schema),
            (PublishedRevisionCreated, PublishedRevisionExisting),
        )
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
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
    ).start_published(
        StartPublishedRunRequestV2(
            RUN,
            workflow.revision_hash,
            AgentBindingSet(
                (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
            ),
        )
    )
    assert isinstance(started, DurableRunCreated), started
    runtime.launch()
    return workflow


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


def durable_events(runtime: DbosRuntime) -> list[tuple[int, str, str, bytes]]:
    with runtime.engine.connect() as connection:
        return [
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


def answer(runtime: DbosRuntime, workflow: WorkflowRevision, value: bytes) -> object:
    """Answer the waiting node exactly as the API route does, one layer under it."""
    return answer_wait_result(
        RUN,
        workflow.revision_hash,
        WAIT_NODE,
        value,
        DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
    )


def what_the_answer_workflow_recorded(
    runtime: DbosRuntime,
) -> tuple[str, tuple[str, ...]]:
    """The answer workflow's own durable record: its result, and what it started.

    Read through DBOS rather than off the run, because recovery replays exactly
    this record. The started ids are the child workflows the answer drove.
    """
    with runtime.engine.connect() as connection:
        workflow_id = str(
            connection.scalar(
                sa.select(wait_answers.c.answer_workflow_id).where(
                    wait_answers.c.run_id == RUN.value,
                    wait_answers.c.node_id == WAIT_NODE,
                )
            )
        )
    status = DBOS.get_workflow_status(workflow_id)
    assert status is not None and status.name == ANSWER_WORKFLOW_NAME
    return (
        str(DBOS.retrieve_workflow(workflow_id).get_result()),
        tuple(
            str(step["child_workflow_id"])
            for step in DBOS.list_workflow_steps(workflow_id)
            if step["child_workflow_id"] is not None
        ),
    )


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_a_v3_line_holds_at_its_wait_node_until_a_person_answers_it(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The pause is durable, visible, and nothing moves the run while it holds.

    Held rather than merely reached: after the run reports WAITING_INPUT it is
    read again, and the agent executor is asked how many nodes it ran. A pause
    that some queue quietly drove past would show the second agent's attempt
    here, and the run would have moved on without the person it is waiting for.
    """
    started, recording = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)

    wait_for_state(started, RunState.WAITING_INPUT)
    time.sleep(0.2)

    with started.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
    assert (str(run["state"]), str(run["current_node_id"])) == (
        RunState.WAITING_INPUT.value,
        WAIT_NODE,
    )
    assert run["terminal_hash"] is None
    assert durable_events(started) == [
        (1, "implement", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
        (2, WAIT_NODE, RunEventKind.WAITING_INPUT.value, b""),
    ]
    assert recording.opened is not None
    assert [request.node_id for request in recording.opened.requests] == ["implement"]
    assert workflow.revision_hash.value == str(run["revision_hash"])


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_a_waiting_v3_run_reads_back_as_waiting_with_the_node_that_owes_a_move(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A reader is told the run is waiting and which node the person is at.

    This is the half an operator sees. The run resource says WAITING_INPUT rather
    than a state a V3 run supposedly cannot reach, and the rail marks the wait
    node as the one owing a move while the node before it reads as done.
    """
    started, _ = runtime
    start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    read = durable_queries(started.engine).get_run(RUN)

    assert isinstance(read, RunFound), read
    resource = run_resource(read.projection).model_dump(mode="json")
    assert resource["workflow_format_version"] == 3
    assert resource["state"] == RunState.WAITING_INPUT.value
    assert resource["current_node_id"] == WAIT_NODE
    assert resource["terminal_hash"] is None
    assert [(entry["node_id"], entry["state"]) for entry in resource["node_rail"]] == [
        ("implement", NodeState.SUCCEEDED.value),
        (WAIT_NODE, NodeState.NEEDS_YOU.value),
        ("review", NodeState.QUEUED.value),
    ]


@pytest.mark.proves("a-waiting-v3-run-is-answerable-on-its-run-page")
def test_the_waiting_node_reading_carries_the_authored_prompt(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The published document already names the question; this read passes it on."""
    started, _ = runtime
    start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    found = durable_queries(started.engine).get_node_detail(RUN, WAIT_NODE)

    assert isinstance(found, NodeDetailFound), found
    assert found.detail.job == b"Approve this candidate, or name the blocking defect."
    assert found.detail.job_hash == Sha256Hash.of(found.detail.job).value


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_an_answer_carries_a_waiting_v3_line_on_to_its_terminal_hash(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The person's answer is what restarts the line, and the line then ends.

    The answer's bytes are kept as the event's payload, so the terminal hash the
    run settles on folds in what the person actually said: a different answer is
    a different chain, which is the whole reason the pause is part of the record.
    """
    started, recording = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    accepted = answer(started, workflow, ANSWER)

    assert isinstance(accepted, AnswerAcceptedPending), accepted
    wait_for_state(started, RunState.COMPLETED)
    assert durable_events(started) == [
        (1, "implement", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
        (2, WAIT_NODE, RunEventKind.WAITING_INPUT.value, b""),
        (3, WAIT_NODE, RunEventKind.WAIT_ANSWERED.value, ANSWER),
        (4, "review", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
    ]
    assert recording.opened is not None
    assert [request.node_id for request in recording.opened.requests] == [
        "implement",
        "review",
    ]
    with started.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        event_hashes = tuple(
            Sha256Hash(str(value))
            for value in connection.execute(
                sa.select(run_events.c.event_hash)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).scalars()
        )
        answer_state = connection.scalar(
            sa.select(wait_answers.c.state).where(
                wait_answers.c.run_id == RUN.value,
                wait_answers.c.node_id == WAIT_NODE,
            )
        )
    assert str(run["current_node_id"]) == "review"
    assert (
        str(run["terminal_hash"])
        == terminal_hash_for(workflow.revision_hash, event_hashes).value
    )
    assert str(answer_state) == WaitAnswerState.APPLIED.value

    # #511: the answer this test just drove through is readable on its own node,
    # not only folded into the run's terminal hash.
    found = durable_queries(started.engine).get_node_detail(RUN, WAIT_NODE)
    assert isinstance(found, NodeDetailFound), found
    assert found.detail.answer is not None
    assert found.detail.answer.value == ANSWER
    assert found.detail.answer.value_hash == Sha256Hash.of(ANSWER)


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_an_answered_wait_standing_last_completes_its_own_run(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A pause at the end of the line is the node that ends the run.

    The completion rule is the one every other node is asked, so a Wait node that
    nothing depends on carries the run to COMPLETED itself rather than handing on
    to a successor its author never declared.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_AS_THE_SINK)
    wait_for_state(started, RunState.WAITING_INPUT)

    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)

    wait_for_state(started, RunState.COMPLETED)
    assert durable_events(started) == [
        (1, "implement", RunEventKind.AGENT_COMPLETED.value, PROVIDER_OUTPUT),
        (2, WAIT_NODE, RunEventKind.WAITING_INPUT.value, b""),
        (3, WAIT_NODE, RunEventKind.WAIT_ANSWERED.value, ANSWER),
    ]
    with started.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        event_hashes = tuple(
            Sha256Hash(str(value))
            for value in connection.execute(
                sa.select(run_events.c.event_hash)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            ).scalars()
        )
    assert str(run["current_node_id"]) == WAIT_NODE
    assert (
        str(run["terminal_hash"])
        == terminal_hash_for(workflow.revision_hash, event_hashes).value
    )


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_the_answer_that_ends_a_run_reports_completion_and_starts_nothing_after_it(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The answer workflow of a sink records COMPLETED and drives no node after it.

    This is the record recovery replays, so it is what has to be honest rather
    than merely harmless. Reporting STARTED for a finished run would be the
    workflow misdescribing itself, and starting a node after a terminal
    transition is only free while DBOS happens to deduplicate the sink's own
    workflow id -- correctness after the run has ended must not rest on that.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_AS_THE_SINK)
    wait_for_state(started, RunState.WAITING_INPUT)

    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)

    wait_for_state(started, RunState.COMPLETED)
    assert what_the_answer_workflow_recorded(started) == (RunState.COMPLETED.value, ())


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_the_answer_that_carries_a_run_on_reports_it_started_the_next_node(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """Where a successor is owed, the same record says STARTED and names its child.

    The counterpart of the sink case: the two branches of the answer workflow are
    told apart by what each one records, so neither reading can be reached by the
    other run's shape.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)

    wait_for_state(started, RunState.COMPLETED)
    result, started_children = what_the_answer_workflow_recorded(started)
    assert result == RunState.STARTED.value
    assert started_children == (
        node_workflow_id_for(
            NodeExecutionId.for_node(RUN, workflow.revision_hash, "review")
        ),
    )


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_a_stored_answer_names_the_exact_execution_of_the_node_it_answers(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The row is keyed by execution and round, not by the node the person saw.

    A node a declared loop turns pauses once per round, so an answer that named
    only run and node would let a message typed for one round be applied to a
    later one. What is asserted is the key the store really holds.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)

    wait_for_state(started, RunState.COMPLETED)
    with started.engine.connect() as connection:
        stored = (
            connection.execute(
                sa.select(wait_answers).where(wait_answers.c.run_id == RUN.value)
            )
            .mappings()
            .one()
        )
        paused_event = (
            connection.execute(
                sa.select(run_events).where(
                    run_events.c.run_id == RUN.value,
                    run_events.c.event_kind == RunEventKind.WAIT_ANSWERED.value,
                )
            )
            .mappings()
            .one()
        )
    execution = NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE)
    assert str(stored["node_execution_id"]) == execution.value
    assert int(stored["round_ordinal"]) == FIRST_ROUND_ORDINAL
    assert str(paused_event["node_execution_id"]) == execution.value
    assert int(paused_event["round_ordinal"]) == FIRST_ROUND_ORDINAL


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_an_answer_workflow_recovered_without_a_round_still_reaches_its_answer(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """An answer enqueued before rounds existed carries three arguments, not four.

    Such a row recovers into `durable_answer` with no round, so the lookup it
    makes is the roundless one. It has to reach the same stored answer and
    commit the same transition, or the hop would strand every answer a
    predecessor had already accepted.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_AS_THE_SINK)
    wait_for_state(started, RunState.WAITING_INPUT)
    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)
    wait_for_state(started, RunState.COMPLETED)

    with started.engine.connect() as connection:
        recovered = load_wait_answer(connection, RUN, workflow.revision_hash, WAIT_NODE)
        replayed = commit_wait_answered(connection, recovered.answer)

    assert recovered.answer.answer_bytes == ANSWER
    assert recovered.answer.round_ordinal == FIRST_ROUND_ORDINAL
    assert recovered.state is WaitAnswerState.APPLIED
    assert replayed.event.event_kind is RunEventKind.WAIT_ANSWERED
    assert replayed.state is RunState.COMPLETED


@pytest.mark.parametrize(
    "rejected",
    [b"41", b'{"verdict": "approved"}', b"approved", b""],
    ids=["a number", "an object", "text that is no JSON value", "nothing at all"],
)
@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_an_answer_the_waits_own_schema_refuses_leaves_the_run_waiting(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
    rejected: bytes,
) -> None:
    """The schema the author pinned is what judges the answer, and it may say no.

    Refused rather than kept: nothing durable is written, the run is still owed
    an answer, and the person can send another. The judge is the same schema
    owner that reads every value this run produces, so an answer and an agent
    output are held to one standard.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_IN_THE_MIDDLE)
    wait_for_state(started, RunState.WAITING_INPUT)

    refused = answer(started, workflow, rejected)

    assert isinstance(refused, UnanswerableWait), refused
    with started.engine.connect() as connection:
        stored = connection.scalar(sa.select(sa.func.count()).select_from(wait_answers))
        state = connection.scalar(
            sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
        )
    assert stored == 0
    assert str(state) == RunState.WAITING_INPUT.value
    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)


@pytest.mark.proves("a-v3-line-stops-for-a-person-and-their-answer-carries-it-on")
def test_an_answer_to_a_v3_run_that_is_not_waiting_is_refused_by_its_state(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A run that owes nobody a move is refused as the state conflict it is.

    Named apart from the schema refusal on purpose: this answer may be perfectly
    well formed, and what is wrong is that the run is not standing where the
    answer says it is.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_AS_THE_SINK)
    wait_for_state(started, RunState.WAITING_INPUT)
    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)
    wait_for_state(started, RunState.COMPLETED)

    late = answer_wait_result(
        RUN,
        workflow.revision_hash,
        "implement",
        ANSWER,
        DbosWaitAnswerer(started.engine, started.settings.application_version),
    )

    assert isinstance(late, AnswerStateConflict), late
