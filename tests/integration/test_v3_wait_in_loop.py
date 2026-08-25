"""A declared loop may now repeat a Wait node, and a run may start on one.

**Why this file exists.** #658 amends `_unrepeatable_loop_forms` to admit a Wait
inside a loop body -- no grammar changed, because the round identity this needs
already landed with ADR 0014 (`WaitNodeBinding` carries the round ordinal, and an
answer is keyed by execution and round). What was never driven end to end is the
shape a conversation actually needs: a generic `loop{wait, agent}` document, run
through the public start and answer doors, where the run's very first node is the
Wait a person answers, an agent reads that answer plus its own previous round's
report, and the loop ends honestly at its declared ceiling.

**Why one test.** The five things this proves -- start-on-Wait, the answer as a
same-round input, the previous-round self-edge (present in round two, absent in
round one), the ceiling ending the run rather than a verdict, and a restart across
the second round's pause -- only add up to the claimed shape together. Proving them
apart would let four pass while the fifth silently regressed.

Running it is what found the store gap the V36 hop closes: the once-per-node
event key refused a Wait node's second pause, so a document shaped like this one
could be admitted but not driven to a second round.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import run_events, runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.answer_wait import AnswerAcceptedPending, answer_wait_result
from atelier2.application.compose_node_job import RESULT_HEADING
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
from atelier2.contracts.executions import NodeExecutionId, RunEventKind
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
    answering_each_execution,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

RUN = RunId("v3/wait-in-loop")
WAIT_NODE = "ask"
AGENT_NODE = "work"
LOOP_ID = "conversation"
LOOP_MAXIMUM_ROUNDS = 2

ANSWER_ROUND_1 = b'"do the first thing"'
ANSWER_ROUND_2 = b'"do the second thing"'
REPORT_ROUND_1 = b'"the first thing is done"'
REPORT_ROUND_2 = b'"the second thing is done"'

WAIT_IN_LOOP_DOCUMENT = (
    b"""format_version: 3
name: A person and an agent take alternating rounds
nodes:
  - id: """
    + WAIT_NODE.encode()
    + b"""
    type: wait
    prompt: What should this round do?
"""
    + declared_output(ANY_JSON_SCHEMA, "answer")
    + b"""  - id: """
    + AGENT_NODE.encode()
    + b"""
    type: agent
    role: builder
    mode: headless
    instruction: Do what the person just asked, informed by your own last report.
    depends_on: ["""
    + WAIT_NODE.encode()
    + b"""]
    inputs:
      - name: answer
        from: {node: """
    + WAIT_NODE.encode()
    + b""", output: answer}
      - name: previous_report
        from: {node: """
    + AGENT_NODE.encode()
    + b""", output: report}
"""
    + declared_output(ANY_JSON_SCHEMA, "report")
    + f"""loops:
  - id: {LOOP_ID}
    body: [{WAIT_NODE}, {AGENT_NODE}]
    maximum_rounds: {LOOP_MAXIMUM_ROUNDS}
""".encode()
)
"""The thinnest shape a conversational round needs: a person, then an agent that
reads what the person just said and what it itself said last round -- looped, with
no verdict, so only the declared ceiling ends it.

The loop enters at the Wait, which is also the whole document's only root: this is
the run that has never been started before, because every earlier Wait test put an
Agent in front of it.
"""


def provider() -> RecordingAgentExecutorFactoryV2:
    """The agent executor, answering each round of `work` with its own report."""
    return RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        REPORT_ROUND_1,
        command=answering_each_execution(
            {
                (AGENT_NODE, 1): REPORT_ROUND_1,
                (AGENT_NODE, 2): REPORT_ROUND_2,
            }
        ),
    )


def runtime_over(root: Path, recording: RecordingAgentExecutorFactoryV2) -> DbosRuntime:
    """A runtime over the durable state in this directory, as a restart builds one."""
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "v3-wait-in-loop-test",
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


@pytest.fixture
def recording() -> RecordingAgentExecutorFactoryV2:
    return provider()


def publish_and_start(
    runtime: DbosRuntime, recording: RecordingAgentExecutorFactoryV2
) -> WorkflowRevision:
    """Publish the document and its bindings, then start the run through the
    public start seam -- nothing here reaches into the engine."""
    del recording
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
    workflow = WorkflowRevision(WAIT_IN_LOOP_DOCUMENT)
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
    return workflow


def answer(runtime: DbosRuntime, workflow: WorkflowRevision, value: bytes) -> object:
    """Answer the currently open round of the Wait, exactly as the API route does."""
    return answer_wait_result(
        RUN,
        workflow.revision_hash,
        WAIT_NODE,
        value,
        DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
    )


def wait_for_waiting_round(runtime: DbosRuntime, round_ordinal: int) -> None:
    """Poll durable truth for the run paused on the Wait in exactly this round.

    A state string alone cannot tell round one's pause from round two's -- both
    read WAITING_INPUT -- so this is the predicate a caller actually needs.
    """
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


def durable_events(runtime: DbosRuntime) -> list[tuple[str, int, str]]:
    with runtime.engine.connect() as connection:
        return [
            (str(record.node_id), int(record.round_ordinal), str(record.event_kind))
            for record in connection.execute(
                sa.select(run_events)
                .where(run_events.c.run_id == RUN.value)
                .order_by(run_events.c.event_sequence)
            )
        ]


def test_a_loop_of_wait_and_agent_carries_a_conversation_to_its_ceiling(
    tmp_path: Path, recording: RecordingAgentExecutorFactoryV2
) -> None:
    started = runtime_over(tmp_path, recording)
    started.initialize_storage()
    try:
        workflow = publish_and_start(started, recording)
        started.launch()

        # Round one: the run's very first node is the Wait itself -- nothing
        # ran before it, because there is nothing else in this document.
        wait_for_waiting_round(started, 1)
        assert durable_events(started) == [
            (WAIT_NODE, 1, RunEventKind.WAITING_INPUT.value)
        ]

        assert isinstance(
            answer(started, workflow, ANSWER_ROUND_1), AnswerAcceptedPending
        )

        # Round two's own pause is durable proof round one's agent ran and the
        # loop turned back to its head rather than ending on the bound alone.
        wait_for_waiting_round(started, 2)
        assert recording.opened is not None
        round_one_job = recording.opened.requests[0].job_bytes.decode("utf-8")
    finally:
        started.close()

    # The round-one job read the person's answer this round wrote, in the same
    # round `depends_on` already orders -- and nothing of a previous round,
    # because round one has none.
    assert RESULT_HEADING.format(node=WAIT_NODE, name="answer") in round_one_job
    assert ANSWER_ROUND_1.decode("utf-8") in round_one_job
    assert RESULT_HEADING.format(node=AGENT_NODE, name="report") not in round_one_job

    # The process that takes round two's answer, and drives the node reading
    # it, is not the one that held round one's pause.
    recovered = runtime_over(tmp_path, recording)
    try:
        assert isinstance(
            answer(recovered, workflow, ANSWER_ROUND_2), AnswerAcceptedPending
        )
        recovered.launch()
        wait_for_state(recovered, RunState.COMPLETED)

        assert recording.opened is not None
        round_two_job = recording.opened.requests[0].job_bytes.decode("utf-8")

        # The previous-round self-edge: round two's agent reads its own round-one
        # report, exactly the value that runtime produced, under the identity the
        # answer path now carries.
        assert RESULT_HEADING.format(node=WAIT_NODE, name="answer") in round_two_job
        assert ANSWER_ROUND_2.decode("utf-8") in round_two_job
        assert RESULT_HEADING.format(node=AGENT_NODE, name="report") in round_two_job
        assert REPORT_ROUND_1.decode("utf-8") in round_two_job

        # The loop's declared ceiling, not a verdict, ended the run: two rounds
        # ran, each leaving its own Wait and Agent evidence, and nothing after
        # the loop was ever declared to run a third.
        with recovered.engine.connect() as connection:
            head = (
                connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
                .mappings()
                .one()
            )
        assert str(head["state"]) == RunState.COMPLETED.value
        assert int(head["current_round_ordinal"]) == LOOP_MAXIMUM_ROUNDS
        assert str(head["current_node_id"]) == AGENT_NODE
        assert durable_events(recovered) == [
            (WAIT_NODE, 1, RunEventKind.WAITING_INPUT.value),
            (WAIT_NODE, 1, RunEventKind.WAIT_ANSWERED.value),
            (AGENT_NODE, 1, RunEventKind.AGENT_COMPLETED.value),
            (WAIT_NODE, 2, RunEventKind.WAITING_INPUT.value),
            (WAIT_NODE, 2, RunEventKind.WAIT_ANSWERED.value),
            (AGENT_NODE, 2, RunEventKind.AGENT_COMPLETED.value),
        ]
        assert NodeExecutionId.for_node(
            RUN, workflow.revision_hash, WAIT_NODE, 1
        ) != NodeExecutionId.for_node(RUN, workflow.revision_hash, WAIT_NODE, 2)
    finally:
        recovered.close()
