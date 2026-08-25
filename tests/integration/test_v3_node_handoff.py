"""A node reads the work of the node before it, so the line becomes a chain.

**What this file is about.** Until now every node of a V3 line was handed exactly
its own authored instruction and nothing else: the run drove `code -> review ->
fix` in order, and the reviewer never saw what the coder wrote. A published
document could already *say* the hand-off -- `from: {node: code, output: draft}`
is a first-class input source -- and the executable admission refused it, so the
only honest way to write `iterate-v1` was a chain that was not one.

**Why the proof is the job the provider was handed.** A stored row would say the
run remembers a value. What #235 is about is that the value changes what the next
agent does, and the only channel that reaches an agent is its job. So every test
here reads `job_bytes` off a recording provider after a real launched run.

**A person is an earlier node too.** An earlier node that produced a value is not
always an agent: a Wait node's value is the answer a person typed, and a document
reading it is admitted by the same rule that admits any other hand-off. The last
tests here drive that line, because a reader that only knew the agent's own
completion would admit the document and then die where the answer should travel.
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
from atelier2.adapters.dbos.run_store import (
    DbosWaitAnswerer,
    load_graph,
    load_node_outputs,
    load_run_inputs,
    run_from_record_with_bindings,
)
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import (
    InvalidWorkflowDocument,
    parse_executable_workflow_document,
)
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    UnanswerableWait,
    answer_wait_result,
)
from atelier2.application.compose_node_job import node_job
from atelier2.contracts.agent_attempts import AgentAttemptId
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
    ResolvedAgentBinding,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.workflows_v3 import AgentNodeV3, WorkflowGraphV3
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.agent_executions import AgentExecutionResult
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import RunFound
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
)
from tests.scenarios.api import durable_queries

TEXT_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b'{"type": "string"}')
PROVIDER_OUTPUT = b'"the exact sentence this node produced"'
RUN = RunId("v3/chain")
WAIT_NODE = "approve"
ANSWER = b'"ship it, but shorten the second paragraph"'
ANSWER_THE_SCHEMA_REFUSES = b"41"


def chained_document(schema_hash: PublishedRevisionHash) -> bytes:
    """code -> review -> fix, with the work handed on at every edge."""
    return f"""format_version: 3
name: iterate-v1
description: code, then review, then fix.
nodes:
  - id: code
    type: agent
    role: builder
    mode: headless
    instruction: Write the thing this chain is for.
    outputs:
      - name: draft
        schema:
          ref: text-schema
          revision: {schema_hash.value}
  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Review the draft you were handed.
    depends_on: [code]
    inputs:
      - name: draft
        from:
          node: code
          output: draft
    outputs:
      - name: findings
        schema:
          ref: text-schema
          revision: {schema_hash.value}
  - id: fix
    type: agent
    role: builder
    mode: headless
    instruction: Apply the review to the work.
    depends_on: [review]
    inputs:
      - name: findings
        from:
          node: review
          output: findings
    outputs:
      - name: patch
        schema:
          ref: text-schema
          revision: {schema_hash.value}
""".encode()


def blind_document(schema_hash: PublishedRevisionHash) -> bytes:
    """The same two nodes with nothing handed on -- the contrast case.

    Each still declares the one output its own schema judges, because that is the
    shape any executable agent node has. What it declares is no *input*: nothing
    here asks for the value the node before it wrote.
    """
    return f"""format_version: 3
name: iterate-v1 without a hand-off
nodes:
  - id: code
    type: agent
    role: builder
    mode: headless
    instruction: Write the thing this chain is for.
    outputs:
      - name: draft
        schema:
          ref: text-schema
          revision: {schema_hash.value}
  - id: review
    type: agent
    role: builder
    mode: headless
    instruction: Review what the previous node produced.
    depends_on: [code]
    outputs:
      - name: findings
        schema:
          ref: text-schema
          revision: {schema_hash.value}
""".encode()


def answered_document(schema_hash: PublishedRevisionHash) -> bytes:
    """draft -> a person answers -> apply, with the answer handed on.

    The applying node reads the Wait node's declared output and nothing else, so
    the bytes it is handed can only have come from the person.
    """
    return f"""format_version: 3
name: a person answers between two agents
nodes:
  - id: draft
    type: agent
    role: builder
    mode: headless
    instruction: Write the thing this chain is for.
    outputs:
      - name: draft
        schema:
          ref: text-schema
          revision: {schema_hash.value}
  - id: {WAIT_NODE}
    type: wait
    prompt: Approve this candidate, or name the blocking defect.
    depends_on: [draft]
    outputs:
      - name: approval
        schema:
          ref: text-schema
          revision: {schema_hash.value}
  - id: apply
    type: agent
    role: builder
    mode: headless
    instruction: Apply what the person answered.
    depends_on: [{WAIT_NODE}]
    inputs:
      - name: approval
        from:
          node: {WAIT_NODE}
          output: approval
    outputs:
      - name: patch
        schema:
          ref: text-schema
          revision: {schema_hash.value}
""".encode()


@pytest.fixture
def provider() -> RecordingAgentExecutorFactoryV2:
    return RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-op", PROVIDER_OUTPUT
    )


def runtime_over(root: Path, provider: RecordingAgentExecutorFactoryV2) -> DbosRuntime:
    """A runtime over the durable state in this directory, as a restart builds one."""
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "v3-chain-test",
            agent_scratch_root=agent_scratch_root(root),
        ),
        LoopbackEffectAdapterFactory(
            root / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (provider,),
    )


@pytest.fixture
def runtime(
    tmp_path: Path, provider: RecordingAgentExecutorFactoryV2
) -> Iterator[DbosRuntime]:
    started = runtime_over(tmp_path, provider)
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def jobs_handed_to(provider: RecordingAgentExecutorFactoryV2) -> list[bytes]:
    assert provider.opened is not None, "the provider was never opened"
    return [request.job_bytes for request in provider.opened.requests]


def publish_chain(
    runtime: DbosRuntime, document: bytes
) -> tuple[WorkflowRevision, AgentBindingSet]:
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
    workflow = WorkflowRevision(document)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )


def start_run(
    runtime: DbosRuntime, workflow: WorkflowRevision, bindings: AgentBindingSet
) -> None:
    """Start the published run these bindings resolve, or fail saying why not."""
    created = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
    ).start_published(StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings))
    assert isinstance(created, DurableRunCreated), created


def answer(runtime: DbosRuntime, workflow: WorkflowRevision, value: bytes) -> object:
    """Answer the waiting node exactly as the API route does, one layer under it."""
    return answer_wait_result(
        RUN,
        workflow.revision_hash,
        WAIT_NODE,
        value,
        DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
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


@pytest.mark.proves("a-node-reads-the-work-of-the-node-before-it")
def test_the_reviewer_is_handed_what_the_coder_actually_wrote(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    """The sentence this item exists for, measured where it becomes true.

    Three nodes, one launch, nothing touched in between. What is asserted is not
    that a row remembers the draft but that the second and third agents were
    *asked* about it: their job carries the exact bytes the node before them
    produced, announced by the node that did the work.
    """
    workflow, bindings = publish_chain(
        runtime, chained_document(TEXT_SCHEMA.revision_hash)
    )

    start_run(runtime, workflow, bindings)

    runtime.launch()
    wait_for_state(runtime, RunState.COMPLETED)

    coded, reviewed, fixed = jobs_handed_to(provider)
    assert PROVIDER_OUTPUT not in coded
    assert b"--- result of code: draft ---" in reviewed
    assert PROVIDER_OUTPUT in reviewed
    assert b"--- result of review: findings ---" in fixed
    assert PROVIDER_OUTPUT in fixed


@pytest.mark.proves("a-node-reads-the-work-of-the-node-before-it")
def test_a_line_that_hands_nothing_on_still_tells_each_node_only_its_own_sentence(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    """The contrast case, so the first test cannot pass by accident.

    A document that declares no hand-off is still admitted and still runs -- and
    its reviewer is still blind, because nothing in it asked for the draft. That
    is the shape `iterate-v1` was stuck in, and it stays available for a line
    whose nodes really are independent.
    """
    workflow, bindings = publish_chain(
        runtime, blind_document(TEXT_SCHEMA.revision_hash)
    )

    start_run(runtime, workflow, bindings)

    runtime.launch()
    wait_for_state(runtime, RunState.COMPLETED)

    coded, reviewed = jobs_handed_to(provider)
    assert coded == b"Write the thing this chain is for."
    assert reviewed == b"Review what the previous node produced."


@pytest.mark.proves("a-form-the-runtime-does-not-keep-is-still-refused-by-name")
@pytest.mark.parametrize(
    ("document", "named"),
    [
        pytest.param(
            b"""format_version: 3
name: an output an operator would have to confirm
nodes:
  - id: only
    type: agent
    role: builder
    mode: headless
    instruction: Do it.
    outputs:
      - name: draft
        schema: {ref: s, revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
        confirmed_by: operator
""",
            "claims operator confirmation",
            id="an operator confirmation",
        ),
        pytest.param(
            b"""format_version: 3
name: two values nothing tells apart
nodes:
  - id: only
    type: agent
    role: builder
    mode: headless
    instruction: Do it.
    outputs:
      - name: draft
        schema: {ref: s, revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
      - name: notes
        schema: {ref: s, revision: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
""",
            "2 outputs on node 'only'",
            id="a second output",
        ),
    ],
)
def test_an_output_form_nothing_keeps_is_refused_by_its_own_name(
    document: bytes, named: str
) -> None:
    """Admitting `outputs` is not admitting everything an author may write in it.

    The value itself is kept -- written durably, hash-bound, read against its
    schema before it travels. A second value is not: an agent node completes with
    one payload, and two would leave one of them answered by the other.

    An operator confirmation is refused too, and measuring where showed it is
    refused *earlier* than by this head's rule: a headless node may not claim one
    at all, and headless is the only mode the executable admission takes. This
    head's own refusal therefore stands behind a door nothing opens today, which
    is said here rather than dressed as a proof of it.
    """
    with pytest.raises(InvalidWorkflowDocument, match=named):
        parse_executable_workflow_document(document)


def attempt_for(
    runtime: DbosRuntime,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    job: bytes,
) -> AgentAttemptExecution:
    """The attempt a node would run under, for the job it is really handed."""
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
        NodeExecutionId.for_node(RUN, revision_hash, node_id),
        RUN,
        revision_hash,
        node_id,
        ResolvedAgentBinding(binding.role, binding.configuration, binding.auth_profile),
        AgentExecutorOperationalIdentity("exact-op"),
        job,
    )
    return AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        1,
    )


@pytest.mark.proves("a-node-reads-the-work-of-the-node-before-it")
def test_a_chained_run_reads_back_with_the_attempt_of_the_node_that_was_handed_work(
    runtime: DbosRuntime,
) -> None:
    """The read path must recompute the same job the run was actually given.

    `get_run` proves the attempt it projects by recomputing the job from durable
    truth and comparing the request hash. That recomputation learned the orders a
    node reads and not the work it was handed, so the first run that really was a
    chain would have read back as `RunTransitionConflict` -- a run nobody could
    open, at the moment the chain finally worked.

    Nothing here is hand-built: the first node is driven to success through the
    real attempt store, which advances the run to its heir, and the heir's attempt
    is armed for the job the composition owner says it gets.
    """
    workflow, bindings = publish_chain(
        runtime, chained_document(TEXT_SCHEMA.revision_hash)
    )
    revision_hash = WorkflowRevisionHash(workflow.revision_hash.value)
    start_run(runtime, workflow, bindings)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    coding = attempt_for(
        runtime, revision_hash, "code", b"Write the thing this chain is for."
    )
    store.prepare(coding)
    store.claim(coding)
    store.complete_success(coding, AgentExecutionResult(PROVIDER_OUTPUT))

    with runtime.engine.connect() as connection:
        graph = load_graph(connection, revision_hash)
        assert isinstance(graph, WorkflowGraphV3)
        node = graph.node("review")
        assert isinstance(node, AgentNodeV3)
        handed = node_job(
            node.instruction,
            load_run_inputs(connection, RUN, node),
            load_node_outputs(connection, RUN, revision_hash, graph, node),
        ).encode("utf-8")
    reviewing = attempt_for(runtime, revision_hash, "review", handed)
    store.prepare(reviewing)
    store.claim(reviewing)

    found = durable_queries(runtime.engine).get_run(RUN)

    assert isinstance(found, RunFound), found
    assert found.projection.current_agent_attempt is not None
    assert PROVIDER_OUTPUT in handed


@pytest.mark.proves("a-node-reads-the-work-of-the-node-before-it")
def test_the_agent_after_a_wait_is_handed_the_exact_answer_the_person_typed(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    """The earlier node whose work is read is a person, and the chain still holds.

    The document declaring this hand-off was already admitted, so the only place
    it could fail was the run: the applying node's job either carries the answer
    or the run dies where the value should have travelled. What is asserted is the
    person's exact bytes inside that job, announced by the node they answered --
    and no agent's output, because this node asked for none.
    """
    workflow, bindings = publish_chain(
        runtime, answered_document(TEXT_SCHEMA.revision_hash)
    )
    start_run(runtime, workflow, bindings)
    runtime.launch()
    wait_for_state(runtime, RunState.WAITING_INPUT)

    accepted = answer(runtime, workflow, ANSWER)

    assert isinstance(accepted, AnswerAcceptedPending), accepted
    wait_for_state(runtime, RunState.COMPLETED)
    drafted, applied = jobs_handed_to(provider)
    assert drafted == b"Write the thing this chain is for."
    assert b"--- result of approve: approval ---" in applied
    assert ANSWER in applied
    assert PROVIDER_OUTPUT not in applied


@pytest.mark.proves("a-node-reads-the-work-of-the-node-before-it")
def test_an_answer_the_waits_own_schema_refuses_hands_nothing_on(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    """A value nothing admitted never becomes the work a later node reads.

    The hand-off is only as trustworthy as the judgement in front of it, so the
    same schema owner that reads an agent's output reads the person's answer. The
    refusal leaves the wait open, and the answer that is finally admitted is the
    only one the reading node ever sees.
    """
    workflow, bindings = publish_chain(
        runtime, answered_document(TEXT_SCHEMA.revision_hash)
    )
    start_run(runtime, workflow, bindings)
    runtime.launch()
    wait_for_state(runtime, RunState.WAITING_INPUT)

    refused = answer(runtime, workflow, ANSWER_THE_SCHEMA_REFUSES)

    assert isinstance(refused, UnanswerableWait), refused
    assert isinstance(answer(runtime, workflow, ANSWER), AnswerAcceptedPending)
    wait_for_state(runtime, RunState.COMPLETED)
    _, applied = jobs_handed_to(provider)
    assert ANSWER_THE_SCHEMA_REFUSES not in applied
    assert ANSWER in applied


@pytest.mark.proves("a-node-reads-the-work-of-the-node-before-it")
def test_the_answer_survives_a_restart_between_the_person_and_the_node_reading_it(
    tmp_path: Path, provider: RecordingAgentExecutorFactoryV2
) -> None:
    """The runtime that hands the answer on is not the one that took the pause.

    A pause is exactly where a run is most likely to outlive the process holding
    it, so the answer must be readable from durable truth alone. The window is
    made rather than raced: the first runtime is closed while the run waits, the
    answer is submitted with nothing running to consume it, and only then does a
    second runtime come up. It recovers the enqueued answer and drives the node
    that reads it, having never seen a byte of this run in memory.
    """
    answering = runtime_over(tmp_path, provider)
    answering.initialize_storage()
    try:
        workflow, bindings = publish_chain(
            answering, answered_document(TEXT_SCHEMA.revision_hash)
        )
        start_run(answering, workflow, bindings)
        answering.launch()
        wait_for_state(answering, RunState.WAITING_INPUT)
    finally:
        answering.close()

    recovered = runtime_over(tmp_path, provider)
    try:
        assert isinstance(answer(recovered, workflow, ANSWER), AnswerAcceptedPending)
        recovered.launch()
        wait_for_state(recovered, RunState.COMPLETED)
        handed = jobs_handed_to(provider)
    finally:
        recovered.close()

    assert len(handed) == 1, "the restarted runtime ran more than the reading node"
    assert b"--- result of approve: approval ---" in handed[0]
    assert ANSWER in handed[0]
