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
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import run_events, runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
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
)
from tests.scenarios.api import durable_queries

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
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(created, DurableRunCreated), created


def drive_the_whole_chain(runtime: DbosRuntime) -> None:
    """Launch and wait until both nodes have run, so both have something to read.

    Waiting for the durable terminal state rather than for a clock is what makes
    it deterministic.
    """
    runtime.launch()
    deadline = time.monotonic() + 12
    state = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            state = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
                )
            )
        if state == RunState.COMPLETED.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {state!r}, expected it to complete")


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
                event_kind=written.event_kind.value,
                payload=written.payload,
                payload_hash=written.payload_hash.value,
                event_hash=written.event_hash.value,
            )
        )


@pytest.mark.proves("a-click-into-a-node-answers-what-it-was-asked-and-wrote")
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
