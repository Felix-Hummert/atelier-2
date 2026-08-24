"""A click into a node answers what it was asked, wrote, and is waiting on.

The operator's wish on #238, measured where it becomes true: `GET
/runs/{ref}/nodes/{node_id}` hands back one node's four answers instead of
leaving a panel to stitch them together from the run, the events and the
receipts.

The refusal is the reason this is one read and not three. When a node's own
output does not satisfy the schema its author pinned, the run stops there -- and
until now that reason existed only as an exception inside the driver. The live
run `live/die-kette-sieht` stood silently on STARTED for exactly that: its
`implement` node wrote three German sentences as prose while its author had
pinned a text schema, so the chain refused to hand the value on and nobody could
see why. That run is the shape this file reproduces.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
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
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.answer_wait import AnswerAcceptedPending, answer_wait_result
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
    RunEvent,
    RunEventKind,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import (
    NodeDetailFound,
    NodeQueryMissing,
    RunQueryMissing,
    RunReceiptsFound,
)
from atelier2.ports.workflow_revisions import QueryDurableStateCorrupt
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    commit_configured_agent,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.runs import prepare_and_launch_graph_action, start_published_v1_run
from tests.scenarios.runtime import exact_output_runtime

TEXT_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b'{"type": "string"}')
ANSWER = b'"Ein gutes Code-Review schuetzt vor fehlerhaftem Code."'
PROSE = b"Ein gutes Code-Review schuetzt vor fehlerhaftem Code."
"""The same sentence as bare prose: what a provider answered before #57 enforced.

A run of this build never writes it -- the success write reads the bytes against
the schema their node pinned first -- so the only way it reaches a store is to
have been written by a build without that guard. That is the state the reader
still has to judge, and where the refusal below comes from.
"""
RUN = RunId("v3/detail")


def chained_document(schema_hash: str) -> bytes:
    """implement declares a text output; review reads it."""
    return f"""format_version: 3
name: the chain the operator watched
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Write three German sentences about code review.
    outputs:
      - name: draft
        schema:
          ref: text-schema
          revision: {schema_hash}
  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Judge the draft you were handed.
    depends_on: [implement]
    inputs:
      - name: draft
        from:
          node: implement
          output: draft
    outputs:
      - name: findings
        schema:
          ref: text-schema
          revision: {schema_hash}
""".encode()


@pytest.fixture
def provider() -> RecordingAgentExecutorFactoryV2:
    """A provider whose answer is the one JSON value its node's schema admits."""
    return RecordingAgentExecutorFactoryV2("exact", "exact/v1", "exact-op", ANSWER)


@pytest.fixture
def runtime(
    tmp_path: Path, provider: RecordingAgentExecutorFactoryV2
) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "node-detail-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (provider,),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def publish_and_start(runtime: DbosRuntime) -> None:
    store = DbosCatalogStore(runtime.engine)
    published = store.publish_revision(TEXT_SCHEMA)
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
    workflow = WorkflowRevision(chained_document(TEXT_SCHEMA.revision_hash.value))
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    created = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(created, DurableRunCreated), created


def wait_for_run_state(runtime: DbosRuntime, run_id: RunId, state: RunState) -> None:
    """Poll for a durable state rather than a clock, so waiting stays deterministic."""

    deadline = time.monotonic() + 12
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
                )
            )
        if observed == state.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {observed!r}, expected {state.value!r}")


def drive_the_whole_chain(runtime: DbosRuntime) -> None:
    """Launch and wait until both nodes have run, so both have something to read."""
    runtime.launch()
    wait_for_run_state(runtime, RUN, RunState.COMPLETED)


def plant_the_value_a_build_without_the_guard_wrote(runtime: DbosRuntime) -> None:
    """Put `implement`'s completion in the store carrying bytes its schema refuses.

    This build cannot produce that state and that is the point: the success write
    reads the exact decoded bytes against the schema their node pinned, so prose
    never becomes a completion. A store written before that guard existed still
    holds such values, and the node that reads one must still name the refusal
    instead of handing it on -- so the event is written here exactly as the
    earlier build left it, through the run-event contract that owns its hashes.
    """
    with runtime.engine.connect() as connection:
        revision_hash = WorkflowRevisionHash(
            str(
                connection.scalar(
                    sa.select(runs.c.revision_hash).where(runs.c.run_id == RUN.value)
                )
            )
        )
    written = RunEvent(
        RUN,
        revision_hash,
        1,
        "implement",
        NodeExecutionId.for_node(RUN, revision_hash, "implement"),
        RunEventKind.AGENT_COMPLETED,
        PROSE,
    )
    with runtime.engine.begin() as connection:
        connection.execute(
            run_events.insert().values(
                run_id=written.run_id.value,
                revision_hash=written.revision_hash.value,
                event_sequence=written.event_sequence,
                node_id=written.node_id,
                node_execution_id=written.node_execution_id.value,
                round_ordinal=written.round_ordinal,
                event_kind=written.event_kind.value,
                payload=written.payload,
                payload_hash=written.payload_hash.value,
                event_hash=written.event_hash.value,
            )
        )


@pytest.mark.proves("a-click-into-a-node-answers-what-it-was-asked-and-wrote")
@pytest.mark.proves("a-node-carries-how-long-it-ran")
def test_a_finished_node_answers_its_job_its_value_and_who_produced_it(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    """The three questions an operator asks about a node that has run."""
    publish_and_start(runtime)
    drive_the_whole_chain(runtime)

    found = durable_queries(runtime.engine).get_node_detail(RUN, "implement")

    assert isinstance(found, NodeDetailFound), found
    detail = found.detail
    assert detail.state is NodeState.SUCCEEDED

    # Held against what the driver really passed, not against itself. Hashing the
    # answer would prove only that this test can hash.
    assert provider.opened is not None
    handed = next(
        request
        for request in provider.opened.requests
        if request.node_id == "implement"
    )
    assert detail.job == handed.job_bytes
    assert detail.job_hash == Sha256Hash.of(handed.job_bytes).value
    assert detail.answer is not None
    assert detail.answer.value == ANSWER
    assert detail.answer.value_hash == Sha256Hash.of(ANSWER)
    assert detail.provenance is not None
    assert detail.provenance.provider_id == "exact"
    assert detail.provenance.role == "builder"
    assert detail.started_at is not None
    assert detail.ended_at is not None
    assert detail.started_at.value <= detail.ended_at.value

    # The receipt's own hash, not the job hash standing in for it: the two frame
    # different preimages, and a reader told to compare them would reject a job
    # that is right.
    assert detail.provenance.request_hash == handed.request_hash.value
    assert detail.provenance.request_hash != detail.job_hash

    receipts = durable_queries(runtime.engine).list_run_receipts(RUN)
    assert isinstance(receipts, RunReceiptsFound), receipts
    assert {item.node_id for item in receipts.items} == {"implement", "review"}
    implement = next(item for item in receipts.items if item.node_id == "implement")
    assert implement.receipt_hash.value == detail.provenance.receipt_hash
    assert implement.request_hash.value == detail.provenance.request_hash
    assert implement.output_bytes == ANSWER
    assert implement.auth_profile_revision_hash.value
    assert implement.binding_set_hash.value


WAIT_ANSWER = b'"looks correct, ship it"'
WAIT_PROMPT = b"Approve what the builder wrote, or name the blocking defect."


def wait_sink_document(schema_hash: str) -> bytes:
    """implement writes a draft; a person approves it and nothing follows.

    The Wait node stands as the run's own sink, exactly as `#510`'s
    `WAIT_ANSWERED` ending already reads a V3 run's terminal node -- the shape
    this file's #238 read has to answer for a Wait the same way it already does
    for an Agent.
    """
    return f"""format_version: 3
name: A person approves what the builder wrote
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Write three German sentences about code review.
    outputs:
      - name: draft
        schema:
          ref: text-schema
          revision: {schema_hash}
  - id: approve
    type: wait
    prompt: {WAIT_PROMPT.decode()}
    depends_on: [implement]
    outputs:
      - name: approval
        schema:
          ref: text-schema
          revision: {schema_hash}
""".encode()


def publish_and_start_wait_chain(runtime: DbosRuntime) -> WorkflowRevision:
    """The same publish-and-start shape as `publish_and_start`, for the Wait sink."""
    store = DbosCatalogStore(runtime.engine)
    published = store.publish_revision(TEXT_SCHEMA)
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
    workflow = WorkflowRevision(wait_sink_document(TEXT_SCHEMA.revision_hash.value))
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    created = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(created, DurableRunCreated), created
    return workflow


def plant_a_pre_v22_wait_answer(
    runtime: DbosRuntime, workflow: WorkflowRevision
) -> None:
    """Write `approve`'s answer exactly as a build before `event_instants` did.

    No `event_instants` row accompanies it, because that table did not exist for
    a build this old to write one -- the same technique
    `plant_the_value_a_build_without_the_guard_wrote` above uses for a state this
    build's own write path cannot reach.
    """
    written = RunEvent(
        RUN,
        workflow.revision_hash,
        1,
        "approve",
        NodeExecutionId.for_node(RUN, workflow.revision_hash, "approve"),
        RunEventKind.WAIT_ANSWERED,
        WAIT_ANSWER,
    )
    with runtime.engine.begin() as connection:
        connection.execute(
            run_events.insert().values(
                run_id=written.run_id.value,
                revision_hash=written.revision_hash.value,
                event_sequence=written.event_sequence,
                node_id=written.node_id,
                node_execution_id=written.node_execution_id.value,
                round_ordinal=written.round_ordinal,
                event_kind=written.event_kind.value,
                payload=written.payload,
                payload_hash=written.payload_hash.value,
                event_hash=written.event_hash.value,
            )
        )


@pytest.mark.proves("a-click-into-a-node-answers-what-it-was-asked-and-wrote")
@pytest.mark.proves("a-node-carries-how-long-it-ran")
def test_an_answered_wait_answers_its_job_its_value_and_when(
    runtime: DbosRuntime,
) -> None:
    """The Wait mirror of the Agent case above.

    #511: `_node_answer` matched `AGENT_COMPLETED` alone, so a Result tab opened
    on an answered Wait read "nothing written" even though the person's answer
    sat durably in `run_events.payload` all along. The same three questions,
    asked of the node a person answered rather than an agent.
    """
    workflow = publish_and_start_wait_chain(runtime)
    runtime.launch()
    wait_for_run_state(runtime, RUN, RunState.WAITING_INPUT)

    answered = answer_wait_result(
        RUN,
        workflow.revision_hash,
        "approve",
        WAIT_ANSWER,
        DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
    )
    assert isinstance(answered, AnswerAcceptedPending), answered
    wait_for_run_state(runtime, RUN, RunState.COMPLETED)

    found = durable_queries(runtime.engine).get_node_detail(RUN, "approve")

    assert isinstance(found, NodeDetailFound), found
    detail = found.detail
    assert detail.state is NodeState.SUCCEEDED
    assert detail.job == WAIT_PROMPT
    assert detail.job_hash == Sha256Hash.of(WAIT_PROMPT).value
    assert detail.answer is not None
    assert detail.answer.value == WAIT_ANSWER
    assert detail.answer.value_hash == Sha256Hash.of(WAIT_ANSWER)

    # A Wait's window is the single instant its answer was recorded -- there is
    # no separate started/ended pair the way an agent attempt has one.
    assert detail.started_at is not None
    assert detail.ended_at is not None
    assert detail.started_at == detail.ended_at


@pytest.mark.proves("a-node-carries-how-long-it-ran")
def test_a_wait_answered_before_event_instants_existed_carries_no_guessed_time(
    runtime: DbosRuntime,
) -> None:
    """A pre-V22 answer still reads back its value, honestly without a timestamp.

    `event_instants` exists from V22 on. A run answered before that build wrote
    no row for it, and the honest read is nothing -- not a guess built from
    whatever else the store happens to hold.
    """
    workflow = publish_and_start_wait_chain(runtime)
    plant_a_pre_v22_wait_answer(runtime, workflow)

    found = durable_queries(runtime.engine).get_node_detail(RUN, "approve")

    assert isinstance(found, NodeDetailFound), found
    detail = found.detail
    assert detail.answer is not None
    assert detail.answer.value == WAIT_ANSWER
    assert detail.started_at is None
    assert detail.ended_at is None


V1_ANSWER_CHAIN_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: waiting, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: waiting}
  - {id: agent, type: agent, job: job-17, output: agent-answer, next: action}
"""
V1_ANSWER_CHAIN_RUN = RunId("v1/answer-chain")
V1_WAIT_ANSWER = b"7"


@pytest.fixture
def v1_answer_chain_runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    """A V1 line whose four node kinds -- Agent, Action, Wait, Subworkflow -- each
    write their own completion, so the fix is proven against every kind
    `_node_answer` now reads rather than the Agent kind alone.
    """
    runtime = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "v1-answer-chain-test"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    runtime.initialize_storage()
    try:
        yield runtime
    finally:
        runtime.close()


@pytest.mark.proves("a-click-into-a-node-answers-what-it-was-asked-and-wrote")
def test_every_answer_bearing_node_kind_reads_back_its_own_value(
    v1_answer_chain_runtime: DbosRuntime,
) -> None:
    """Action and Subworkflow completions answer their node exactly as Agent does.

    `_node_answer` used to match `AGENT_COMPLETED` alone; this drives one V1 line
    all the way to its own Subworkflow sink, so `ACTION_COMPLETED` and
    `SUBWORKFLOW_COMPLETED` are read back through the real production write path
    rather than a planted event, alongside the Agent and Wait kinds the tests
    above already prove on their own.
    """
    runtime = v1_answer_chain_runtime
    revision = WorkflowRevision(V1_ANSWER_CHAIN_DOCUMENT)
    start_published_v1_run(
        runtime.engine, runtime.settings, V1_ANSWER_CHAIN_RUN, revision
    )
    with canonical_write_transaction(runtime.engine) as connection:
        commit_configured_agent(
            connection, V1_ANSWER_CHAIN_RUN, revision.revision_hash, "agent"
        )
    intent = prepare_and_launch_graph_action(
        runtime.engine,
        runtime.settings,
        V1_ANSWER_CHAIN_RUN,
        revision.revision_hash,
        runtime.effect_adapter_binding,
    )
    runtime.launch()
    wait_for_run_state(runtime, V1_ANSWER_CHAIN_RUN, RunState.WAITING_INPUT)

    answered = answer_wait_result(
        V1_ANSWER_CHAIN_RUN,
        revision.revision_hash,
        "waiting",
        V1_WAIT_ANSWER,
        DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
    )
    assert isinstance(answered, AnswerAcceptedPending), answered
    wait_for_run_state(runtime, V1_ANSWER_CHAIN_RUN, RunState.COMPLETED)

    queries = durable_queries(runtime.engine)
    expected_answers = {
        "agent": b"agent-answer",
        "action": intent.request.payload,
        "waiting": V1_WAIT_ANSWER,
        "final": b"5",
    }
    for node_id, expected in expected_answers.items():
        found = queries.get_node_detail(V1_ANSWER_CHAIN_RUN, node_id)
        assert isinstance(found, NodeDetailFound), (node_id, found)
        answer = found.detail.answer
        assert answer is not None, node_id
        assert answer.value == expected, node_id
        assert answer.value_hash == Sha256Hash.of(expected), node_id


@pytest.mark.proves("a-node-that-stops-the-run-says-what-it-is-waiting-on")
def test_the_node_that_stops_the_run_names_the_refusal_that_stops_it(
    runtime: DbosRuntime,
) -> None:
    """The live silence, given a voice.

    `review` cannot be handed the draft, because the draft is prose and its
    author pinned a text schema. Before this read, that reason lived only as an
    exception inside the driver and the operator saw a run standing still. The
    node detail names it, in the words the schema owner used.

    Since #57 no run of this build writes such a draft -- the success write reads
    it first -- so the value is planted as an older build left it. What is under
    test is the reader: a stored value its own schema refuses is named, never
    handed on.
    """
    publish_and_start(runtime)
    plant_the_value_a_build_without_the_guard_wrote(runtime)

    found = durable_queries(runtime.engine).get_node_detail(RUN, "review")

    assert isinstance(found, NodeDetailFound), found
    detail = found.detail
    assert detail.refusal is not None
    assert "instance-not-json" in detail.refusal
    assert "implement" in detail.refusal
    assert detail.job is None
    assert detail.answer is None


@pytest.mark.proves("a-click-into-a-node-answers-what-it-was-asked-and-wrote")
def test_a_node_the_run_does_not_declare_is_refused_by_name(
    runtime: DbosRuntime,
) -> None:
    """A reader who asks for a node this run has no name for is told so."""
    publish_and_start(runtime)

    assert isinstance(
        durable_queries(runtime.engine).get_node_detail(RUN, "not-a-node"),
        NodeQueryMissing,
    )
    assert isinstance(
        durable_queries(runtime.engine).list_run_receipts(RunId("not-a-run")),
        RunQueryMissing,
    )


@pytest.mark.proves("a-node-that-stops-the-run-says-what-it-is-waiting-on")
def test_a_node_whose_predecessor_has_not_written_carries_no_refusal(
    runtime: DbosRuntime,
) -> None:
    """Waiting is not refusing, and the difference is the whole point.

    Before the first node writes, the second one has nothing to be given -- and
    nothing has judged anything. Reporting that as a refusal would tell an
    operator a run had stopped when it had not started, on the same surface that
    later reports a real refusal honestly.
    """
    publish_and_start(runtime)

    found = durable_queries(runtime.engine).get_node_detail(RUN, "review")

    assert isinstance(found, NodeDetailFound), found
    detail = found.detail
    assert detail.state is NodeState.QUEUED
    assert detail.refusal is None
    assert detail.job is None
    assert detail.answer is None
    assert detail.provenance is None
    receipts = durable_queries(runtime.engine).list_run_receipts(RUN)
    assert isinstance(receipts, RunReceiptsFound), receipts
    assert receipts.items == ()


@pytest.mark.proves("a-node-that-stops-the-run-says-what-it-is-waiting-on")
def test_a_stored_value_that_no_longer_matches_its_hash_is_reported_as_corruption(
    runtime: DbosRuntime,
) -> None:
    """A panel must never dress durable corruption as a tidy refusal.

    The refusal path exists for a value a schema judged and rejected. A payload
    that no longer matches the hash its own event kept is a store disagreeing
    with itself, and it leaves loudly through the same door every other corrupt
    read leaves by.

    The trigger has to be dropped to reach that state at all, and that is worth
    saying: this product cannot write it. An event is immutable by construction,
    so a mismatched payload exists only if something outside the product reached
    the file -- which is exactly the situation the loud answer is for.
    """
    publish_and_start(runtime)
    drive_the_whole_chain(runtime)
    with runtime.engine.begin() as connection:
        connection.execute(sa.text("DROP TRIGGER run_events_no_update"))
        connection.execute(
            sa.text("UPDATE run_events SET payload = :tampered WHERE run_id = :run_id"),
            {"tampered": b'"a value nobody produced"', "run_id": RUN.value},
        )

    found = durable_queries(runtime.engine).get_node_detail(RUN, "review")

    assert isinstance(found, QueryDurableStateCorrupt), found
