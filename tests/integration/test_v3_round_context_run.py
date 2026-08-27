"""Round n of the builder is handed what the last review actually said.

K1 gave the loop its rounds. ADR 0015 let the review steer the back edge. The
builder of the next round still started from its authored instruction alone —
the review sat in a durable artifact and never travelled as input, because a
`from` onto the tail is not a `depends_on`. This is that input edge.

The bound is three and the review accepts in the second round. What is asserted
is not that a row remembers the verdict but that the second builder was *asked*
about it: its job carries the exact bytes the first review produced.
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
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.verdicts import VERDICT_ANSWER_SCHEMA, Verdict
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
    publish_checked_model_registry,
)
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    ROUND_CONTEXT_LOOP_DOCUMENT,
    VERDICT_LOOP_ACCEPTING_ROUND,
    VERDICT_LOOP_MAXIMUM_ROUNDS,
    verdict_answer,
)

RUN = RunId("v3/round-context-loop")
CANDIDATE = b'"the candidate this round produced"'
FIRST_REVIEW = verdict_answer(Verdict.REVISE)
ACCEPTING_REVIEW = verdict_answer(Verdict.ACCEPTED)
ANSWERS = {
    ("implement", round_ordinal): CANDIDATE
    for round_ordinal in range(1, VERDICT_LOOP_MAXIMUM_ROUNDS + 1)
} | {
    ("review", round_ordinal): (
        ACCEPTING_REVIEW
        if round_ordinal == VERDICT_LOOP_ACCEPTING_ROUND
        else FIRST_REVIEW
    )
    for round_ordinal in range(1, VERDICT_LOOP_MAXIMUM_ROUNDS + 1)
}


@pytest.fixture
def runtime(
    tmp_path: Path,
) -> Iterator[tuple[DbosRuntime, RecordingAgentExecutorFactoryV2]]:
    recording = RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-operation",
        b"",
        command=answering_each_execution(ANSWERS),
    )
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-round-context-test",
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


def publish_and_run(runtime: DbosRuntime) -> None:
    catalog_store = DbosCatalogStore(runtime.engine)
    for schema in (ANY_JSON_SCHEMA, VERDICT_ANSWER_SCHEMA):
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
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    workflow = WorkflowRevision(ROUND_CONTEXT_LOOP_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
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
    deadline = time.monotonic() + 16
    observed = ""
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = str(
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
                )
            )
        if observed == RunState.COMPLETED.value:
            return
        time.sleep(0.025)
    raise AssertionError(
        f"run stayed {observed!r}, expected {RunState.COMPLETED.value!r}"
    )


@pytest.mark.proves("a-later-round-reads-the-previous-rounds-output")
def test_the_second_builder_is_handed_what_the_first_review_wrote(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The sentence this item exists for, measured where it becomes true."""
    started_runtime, recording = runtime

    publish_and_run(started_runtime)

    assert recording.opened is not None
    jobs = {
        (request.node_id, request.round_ordinal): request.job_bytes
        for request in recording.opened.requests
    }
    review_heading = RESULT_HEADING.format(node="review", name="verdict").encode()
    first_builder = jobs[("implement", 1)]
    second_builder = jobs[("implement", 2)]

    assert review_heading not in first_builder
    assert FIRST_REVIEW not in first_builder
    assert first_builder == b"Do the one thing this chain is for."
    assert review_heading in second_builder
    assert FIRST_REVIEW in second_builder
    assert ACCEPTING_REVIEW not in second_builder
