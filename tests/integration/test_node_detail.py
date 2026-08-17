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
from atelier2.adapters.dbos.schema import runs
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
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
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
from atelier2.ports.run_queries import NodeDetailFound, NodeQueryMissing
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.api import durable_queries

TEXT_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b'{"type": "string"}')
PROSE = b"Ein gutes Code-Review schuetzt vor fehlerhaftem Code."
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
""".encode()


@pytest.fixture
def provider() -> RecordingAgentExecutorFactoryV2:
    """A provider that answers in prose, exactly as the live one did."""
    return RecordingAgentExecutorFactoryV2("exact", "exact/v1", "exact-op", PROSE)


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


def drive_until_the_chain_stops(runtime: DbosRuntime) -> None:
    """Launch and wait until the first node has written and the run stands still.

    The run cannot complete: the value `implement` wrote is not what its own
    schema admits, so the chain refuses to hand it on. Waiting for the durable
    event rather than for a clock is what makes that deterministic.
    """
    runtime.launch()
    deadline = time.monotonic() + 12
    state = ""
    standing = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            state = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
                )
            )
            standing = str(
                connection.scalar(
                    sa.select(runs.c.current_node_id).where(runs.c.run_id == RUN.value)
                )
            )
        if standing == "review" and state == RunState.STARTED.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"run stayed {state!r} on {standing!r}")


@pytest.mark.proves("a-click-into-a-node-answers-what-it-was-asked-and-wrote")
def test_a_finished_node_answers_its_job_its_value_and_who_produced_it(
    runtime: DbosRuntime,
) -> None:
    """The three questions an operator asks about a node that has run."""
    publish_and_start(runtime)
    drive_until_the_chain_stops(runtime)

    found = durable_queries(runtime.engine).get_node_detail(RUN, "implement")

    assert isinstance(found, NodeDetailFound), found
    detail = found.detail
    assert detail.state is NodeState.SUCCEEDED
    assert detail.job == b"Write three German sentences about code review."
    assert detail.job_hash == Sha256Hash.of(detail.job).value
    assert detail.answer is not None
    assert detail.answer.value == PROSE
    assert detail.answer.value_hash == Sha256Hash.of(PROSE)
    assert detail.provenance is not None
    assert detail.provenance.provider_id == "exact"
    assert detail.provenance.role == "builder"


@pytest.mark.proves("a-node-that-stops-the-run-says-what-it-is-waiting-on")
def test_the_node_that_stops_the_run_names_the_refusal_that_stops_it(
    runtime: DbosRuntime,
) -> None:
    """The live silence, given a voice.

    `review` cannot be handed the draft, because the draft is prose and its
    author pinned a text schema. Before this read, that reason lived only as an
    exception inside the driver and the operator saw a run standing still. The
    node detail names it, in the words the schema owner used.
    """
    publish_and_start(runtime)
    drive_until_the_chain_stops(runtime)

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
