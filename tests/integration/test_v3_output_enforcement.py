"""What an agent answered is held to the schema its node declared, or nothing lands.

**The belegte case this file exists for.** The first real self-run proved the
engine and not the work: a V3 agent was asked for compact JSON, the provider
answered prose, and the atelier wrote `AGENT_COMPLETED` anyway, ran the successor
and ended the run successfully. The contract was declared, pinned and resolved --
and never read against the bytes that came back.

**Where the proof sits, and why there.** The seam under test is the success
write: it is the one moment a provider's answer becomes a fact the product cannot
take back, it is inside the transaction that writes the receipt, the event and
the run's advance, and it is where the graph and the pinned schema are both in
hand. Driving it directly is what makes the refusal case deterministic -- a
launched run whose write refuses stands still, and waiting for a run to keep not
completing proves nothing a clock could not fake.

What is asserted after a refusal is therefore the durable state an operator can
read: no receipt, no completion event, no advanced run, and an attempt that never
became terminal.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.run_store import (
    NodeOutputSchemaRefused,
    run_from_record_with_bindings,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
    run_events,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agent_attempts import AgentAttemptId, AgentAttemptState
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.agent_attempts import AgentAttemptSucceeded
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.agent_executions import AgentExecutionResult
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import agent_scratch_root, failing_agent_executor_factory

PLAN_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    b'{"type": "object", "properties": {"steps": {"type": "integer", '
    b'"minimum": 1}}, "required": ["steps"], "additionalProperties": false}',
)
"""What the node's author asked for: one object naming how many steps it plans."""

NODE = "plan"
INSTRUCTION = b"Answer with the plan this node declared."
RUN = RunId("v3/output-contract")
THE_ANSWER_THE_SCHEMA_ADMITS = b'{"steps": 3}'


def planning_document(schema: PublishedRevision) -> bytes:
    """One agent node whose one output pins a schema that means something."""
    return f"""format_version: 3
name: Plan the work
nodes:
  - id: {NODE}
    type: agent
    role: builder
    mode: headless
    instruction: {INSTRUCTION.decode("ascii")}
    outputs:
      - name: plan
        schema:
          ref: plan-schema
          revision: {schema.revision_hash.value}
""".encode()


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    """A runtime with no provider that can answer: this file drives the store."""
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-output-contract-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (failing_agent_executor_factory("exact", []),),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def armed_attempt(runtime: DbosRuntime) -> AgentAttemptExecution:
    """One started run of the planning document, armed at its one agent node."""
    published = DbosCatalogStore(runtime.engine).publish_revision(PLAN_SCHEMA)
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
    workflow = WorkflowRevision(planning_document(PLAN_SCHEMA))
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    created = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(created, DurableRunCreated), created

    revision_hash = WorkflowRevisionHash(workflow.revision_hash.value)
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)
    assert isinstance(run, RunV3)
    binding = run.agent_bindings[0]
    request = AgentExecutionRequestV2(
        NodeExecutionId.for_node(RUN, revision_hash, NODE),
        RUN,
        revision_hash,
        NODE,
        binding,
        AgentExecutorOperationalIdentity("exact-operation"),
        INSTRUCTION,
    )
    execution = AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    store.prepare(execution)
    store.claim(execution)
    return execution


def durable_answer(runtime: DbosRuntime) -> tuple[int, int, str, str]:
    """What an operator can read afterwards: receipts, events, run and attempt."""
    with runtime.engine.connect() as connection:
        receipts = connection.scalar(
            sa.select(sa.func.count()).select_from(agent_receipts_v2)
        )
        completions = connection.scalar(
            sa.select(sa.func.count())
            .select_from(run_events)
            .where(run_events.c.run_id == RUN.value)
        )
        state = connection.scalar(
            sa.select(runs.c.state).where(runs.c.run_id == RUN.value)
        )
        attempt = connection.scalar(
            sa.select(agent_attempts.c.state).where(
                agent_attempts.c.run_id == RUN.value
            )
        )
    return int(receipts or 0), int(completions or 0), str(state), str(attempt)


@pytest.mark.proves("bytes-their-own-schema-refuses-never-become-a-success")
@pytest.mark.parametrize(
    ("answered", "named"),
    [
        pytest.param(
            b"Sure! I would plan this in three steps.",
            "instance-not-json",
            id="prose where JSON was declared",
        ),
        pytest.param(b'{"steps": 0}', "schema-violated", id="a value out of bounds"),
        pytest.param(b'{"stps": 3}', "schema-violated", id="a misspelled field"),
        pytest.param(b'["one", "two"]', "schema-violated", id="the wrong JSON shape"),
        pytest.param(b'{"steps": 3}\xff', "instance-not-utf8", id="broken UTF-8"),
        pytest.param(
            b'{"steps": 1, "steps": 2}',
            "duplicate-object-key",
            id="one field answered twice",
        ),
    ],
)
def test_an_answer_its_own_schema_refuses_never_becomes_a_success(
    runtime: DbosRuntime, answered: bytes, named: str
) -> None:
    """The belegte case and its neighbours: refused by name, nothing written.

    Each of these reached `AGENT_COMPLETED` before this head, because the pinned
    schema was resolved and then never applied to what came back. The refusal
    names the profile owner's own verdict, and the four things a success would
    have written are all absent: the receipt, the completion event, the run's
    advance, and the attempt's terminal state.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    with pytest.raises(NodeOutputSchemaRefused) as refused:
        store.complete_success(execution, AgentExecutionResult(answered))

    assert named in str(refused.value)
    assert NODE in str(refused.value)
    assert durable_answer(runtime) == (
        0,
        0,
        RunState.STARTED.value,
        AgentAttemptState.LAUNCH_ARMED.value,
    )


@pytest.mark.proves("bytes-their-own-schema-refuses-never-become-a-success")
def test_the_answer_its_own_schema_admits_is_written_as_the_one_success(
    runtime: DbosRuntime,
) -> None:
    """The contrast, so the refusals above cannot pass by refusing everything.

    The same seam, the same schema, one answer that satisfies it: the receipt
    keeps the exact bytes, the completion event carries them under their own
    hash, and the run reaches its terminal state because this node is its sink.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    succeeded = store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_ADMITS)
    )

    assert isinstance(succeeded, AgentAttemptSucceeded), succeeded
    assert durable_answer(runtime) == (
        1,
        1,
        RunState.COMPLETED.value,
        AgentAttemptState.SUCCEEDED.value,
    )
    with runtime.engine.connect() as connection:
        payload = connection.scalar(
            sa.select(run_events.c.payload).where(run_events.c.run_id == RUN.value)
        )
        kept = connection.scalar(sa.select(agent_receipts_v2.c.output_bytes))
    assert bytes(payload or b"") == THE_ANSWER_THE_SCHEMA_ADMITS
    assert bytes(kept or b"") == THE_ANSWER_THE_SCHEMA_ADMITS
