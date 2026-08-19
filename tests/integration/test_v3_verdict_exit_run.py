"""A loop ends in the round its own review accepts, not at the bound it declared.

K1 gave a declared loop its rounds, and reaching the bound was the only way out
it had. This is the second exit: the answer a round produced decides whether the
next round runs. Nothing here reaches into the engine -- the loop and its verdict
are declared, the run is started through the public start seam, `launch()` hands
it to the real queue, and what is asserted is what an operator would see
afterwards.

The bound is three and the review accepts in the second round, so an engine that
ignored the verdict would be seen running a third.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    node_artifacts_v3,
    node_receipts_v3,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agent_attempts import AgentAttemptId
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
from atelier2.contracts.executions import AgentAttemptExecution, RunEventKind
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.verdicts import VERDICT_ANSWER_SCHEMA, Verdict
from atelier2.contracts.workflows import RunCompletes
from atelier2.ports.agent_attempts import AgentAttemptSucceeded
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
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    VERDICT_LOOP_ACCEPTING_ROUND,
    VERDICT_LOOP_DOCUMENT,
    VERDICT_LOOP_MAXIMUM_ROUNDS,
    verdict_answer,
)

RUN = RunId("v3/verdict-steered-loop")
CANDIDATE = b'"the candidate this round produced"'
ROUNDS_THAT_RUN = tuple(range(1, VERDICT_LOOP_ACCEPTING_ROUND + 1))
ANSWERS = {
    ("implement", round_ordinal): CANDIDATE
    for round_ordinal in range(1, VERDICT_LOOP_MAXIMUM_ROUNDS + 1)
} | {
    ("review", round_ordinal): verdict_answer(
        Verdict.ACCEPTED
        if round_ordinal == VERDICT_LOOP_ACCEPTING_ROUND
        else Verdict.REVISE
    )
    for round_ordinal in range(1, VERDICT_LOOP_MAXIMUM_ROUNDS + 1)
}
"""What each node answers in each round it could run, including rounds it must not.

The rounds past the accepting one are answered too, and they answer `revise`: a
scenario that could not have run them would prove the loop stopped for want of an
answer rather than because a verdict ended it.
"""


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
            "v3-verdict-test",
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


def publish_verdict_loop(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet]:
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
    workflow = WorkflowRevision(VERDICT_LOOP_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def start_and_run(runtime: DbosRuntime) -> WorkflowRevision:
    workflow, bindings = publish_verdict_loop(runtime)
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


@pytest.mark.proves("a-declared-verdict-ends-a-loop-before-its-bound")
def test_a_loop_ends_in_the_round_its_own_answer_accepts(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """Every round before the accepting one runs in full, and none after it does."""
    started_runtime, recording = runtime

    start_and_run(started_runtime)

    with started_runtime.engine.connect() as connection:
        events = [
            (str(record["node_id"]), int(str(record["round_ordinal"])))
            for record in connection.execute(
                sa.select(run_events)
                .where(
                    run_events.c.run_id == RUN.value,
                    run_events.c.event_kind == RunEventKind.AGENT_COMPLETED.value,
                )
                .order_by(run_events.c.event_sequence)
            ).mappings()
        ]
        head = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )

    assert events == [
        (node_id, round_ordinal)
        for round_ordinal in ROUNDS_THAT_RUN
        for node_id in ("implement", "review")
    ]
    assert str(head["current_node_id"]) == "review"
    assert int(str(head["current_round_ordinal"])) == VERDICT_LOOP_ACCEPTING_ROUND
    assert recording.opened is not None
    assert [
        (request.node_id, request.round_ordinal)
        for request in recording.opened.requests
    ] == events


@pytest.mark.proves("a-declared-verdict-ends-a-loop-before-its-bound")
def test_the_rounds_the_verdict_ended_leave_no_durable_trace(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """A round that never ran owes nothing: no receipt, no value, no attempt."""
    started_runtime, _ = runtime

    start_and_run(started_runtime)

    with started_runtime.engine.connect() as connection:
        receipts = connection.scalar(
            sa.select(sa.func.count()).select_from(node_receipts_v3)
        )
        artifacts = connection.scalar(
            sa.select(sa.func.count())
            .select_from(node_artifacts_v3)
            .where(node_artifacts_v3.c.run_id == RUN.value)
        )

    executions_that_ran = len(ROUNDS_THAT_RUN) * 2
    assert (receipts, artifacts) == (executions_that_ran, executions_that_ran)


@pytest.mark.proves(
    "the-continuation-a-verdict-steers-is-read-from-what-the-round-kept"
)
def test_a_driver_recovering_after_the_deciding_round_reaches_the_same_edge(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The claim a recovered driver makes is answered from the kept value.

    It is the same request the run itself was driven by, so nothing here
    rebuilds what the engine decided; what is asked is whether the answer that
    round produced still names the edge it took. A recovery that read no verdict
    could not answer at all, and one that read the wrong verdict would contradict
    the run's own head.
    """
    started_runtime, recording = runtime

    start_and_run(started_runtime)
    assert recording.opened is not None
    deciding = recording.opened.requests[-1]
    outcome = DbosAgentAttemptStore(
        started_runtime.engine, started_runtime.settings.application_version
    ).claim(
        AgentAttemptExecution(
            deciding,
            AgentAttemptId.for_execution(
                deciding.node_execution_id, deciding.request_hash, 1
            ),
            1,
        )
    )

    assert isinstance(outcome, AgentAttemptSucceeded), outcome
    assert outcome.completion == RunCompletes()
