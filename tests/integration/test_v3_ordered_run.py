"""One published revision serves every order, through the door an operator uses.

**What this file is about.** Until now a distinct input meant a distinct published
revision: the job travelled inside the document, so asking the same workflow for
four portions instead of two meant publishing a second workflow. That is the
everyday cost #38 exists to end, and this is where it ends -- on `start_published`,
the seam the API, the host and the runtime actually bind, with the runtime
launched so the order reaches the agent that was supposed to read it.

**Why the whole vertical is one test file rather than a layer of them.** The
capability is only true if all of it holds at once: the document is admitted, the
schema it pinned resolves, the order is refused or stored before any row exists,
and the agent receives it. A test that proved the storing without the delivery
would describe a run that carries an order nobody reads, which is exactly the
shape this work was frozen for once already.
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
from atelier2.adapters.dbos.schema import run_inputs_v3, runs
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
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    DurableRunFormatNotExecutable,
    DurableV3StartInputRefused,
    StartPublishedRunRequestV3,
    V3InputRefusal,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2

PORTIONS_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    b'{"type": "object", "properties": {"portions": {"type": "integer", '
    b'"minimum": 1}}, "required": ["portions"], "additionalProperties": false}',
)
NOT_A_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b'{"type": "nonsense"}')

ORDER_NAME = "order"


def ordered_document(schema_hash: PublishedRevisionHash) -> bytes:
    """One agent that reads one order the graph declares."""
    return f"""format_version: 3
name: Cook to order
graph_inputs:
  - name: {ORDER_NAME}
    schema:
      ref: portions-schema
      revision: {schema_hash.value}
nodes:
  - id: cook
    type: agent
    role: cook
    mode: headless
    instruction: Cook exactly what the order says.
    inputs:
      - name: {ORDER_NAME}
        from:
          graph_input: {ORDER_NAME}
""".encode()


@pytest.fixture
def cook() -> RecordingAgentExecutorFactoryV2:
    """The provider this run reaches, kept so the job it was handed can be read."""
    return RecordingAgentExecutorFactoryV2("exact", "exact/v1", "exact-op", b"cooked")


@pytest.fixture
def runtime(
    tmp_path: Path, cook: RecordingAgentExecutorFactoryV2
) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "v3-order-test"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (cook,),
    )
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def jobs_handed_to(cook: RecordingAgentExecutorFactoryV2) -> list[bytes]:
    """Every job this provider was actually asked to run, in the order asked."""
    assert cook.opened is not None, "the provider was never opened"
    return [request.job_bytes for request in cook.opened.requests]


def publish(runtime: DbosRuntime, *revisions: PublishedRevision) -> None:
    store = DbosCatalogStore(runtime.engine)
    for revision in revisions:
        answer = store.publish_revision(revision)
        assert isinstance(
            answer, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), answer


def bind_cook(runtime: DbosRuntime) -> AgentBindingSet:
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
    return AgentBindingSet(
        (AgentBinding(AgentRole("cook"), configuration.revision_hash),)
    )


def publish_ordered_workflow(
    runtime: DbosRuntime, schema: PublishedRevision = PORTIONS_SCHEMA
) -> tuple[WorkflowRevision, AgentBindingSet]:
    publish(runtime, schema)
    workflow = WorkflowRevision(ordered_document(schema.revision_hash))
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, bind_cook(runtime)


def order(value: bytes, schema: PublishedRevision = PORTIONS_SCHEMA) -> RunInput:
    return RunInput(ORDER_NAME, schema.revision_hash, value)


def start(
    runtime: DbosRuntime,
    workflow: WorkflowRevision,
    bindings: AgentBindingSet,
    run_id: RunId,
    *orders: RunInput,
) -> object:
    return DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV3(
            run_id, workflow.revision_hash, bindings, tuple(orders)
        )
    )


def wait_for_state(runtime: DbosRuntime, run_id: RunId, state: RunState) -> None:
    deadline = time.monotonic() + 8
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


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_the_order_reaches_the_agent_and_is_stored_under_the_run(
    runtime: DbosRuntime, cook: RecordingAgentExecutorFactoryV2
) -> None:
    """The whole vertical: admitted, resolved, stored, delivered.

    The agent's own request is what proves the delivery. A stored row alone would
    say the run remembers an order; what an operator asked for is that the order
    changes what the agent does, and the only channel that reaches an agent is
    the job it is handed.
    """
    workflow, bindings = publish_ordered_workflow(runtime)
    run_id = RunId("v3/ordered")

    created = start(runtime, workflow, bindings, run_id, order(b'{"portions": 4}'))
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    with runtime.engine.connect() as connection:
        stored = (
            connection.execute(
                sa.select(run_inputs_v3).where(run_inputs_v3.c.run_id == run_id.value)
            )
            .mappings()
            .all()
        )
    assert [(str(row["name"]), bytes(row["value"])) for row in stored] == [
        (ORDER_NAME, b'{"portions": 4}')
    ]

    assert b'{"portions": 4}' in jobs_handed_to(cook)[0]


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_one_published_revision_serves_two_different_orders(
    runtime: DbosRuntime, cook: RecordingAgentExecutorFactoryV2
) -> None:
    """The reflex this story exists to end, stated as one assertion.

    Two runs, two different orders, and exactly one published workflow revision
    between them -- the agent is asked for something different each time without
    anybody publishing a second document.
    """
    workflow, bindings = publish_ordered_workflow(runtime)

    for run_name, value in (
        ("v3/two", b'{"portions": 2}'),
        ("v3/four", b'{"portions": 4}'),
    ):
        created = start(runtime, workflow, bindings, RunId(run_name), order(value))
        assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    for run_name in ("v3/two", "v3/four"):
        wait_for_state(runtime, RunId(run_name), RunState.COMPLETED)

    delivered = sorted(jobs_handed_to(cook))
    assert len(delivered) == 2
    assert b'{"portions": 2}' in delivered[0]
    assert b'{"portions": 4}' in delivered[1]


@pytest.mark.proves("an-order-the-start-cannot-honour-is-refused-by-its-own-name")
@pytest.mark.parametrize(
    ("supplied", "expected", "expected_name"),
    [
        pytest.param((), V3InputRefusal.MISSING, ORDER_NAME, id="missing"),
        pytest.param(
            (
                order(b'{"portions": 1}'),
                RunInput("supper", PORTIONS_SCHEMA.revision_hash, b'{"portions": 1}'),
            ),
            V3InputRefusal.UNDECLARED,
            "supper",
            id="undeclared",
        ),
        pytest.param(
            (RunInput(ORDER_NAME, NOT_A_SCHEMA.revision_hash, b'{"portions": 1}'),),
            V3InputRefusal.SCHEMA_MISMATCH,
            ORDER_NAME,
            id="schema-mismatch",
        ),
        pytest.param(
            (order(b'{"portions": 0}'),),
            V3InputRefusal.VALUE_REFUSED,
            ORDER_NAME,
            id="value-refused",
        ),
    ],
)
def test_an_order_the_start_cannot_honour_is_refused_by_name_leaving_nothing(
    runtime: DbosRuntime,
    supplied: tuple[RunInput, ...],
    expected: V3InputRefusal,
    expected_name: str,
) -> None:
    """Refused before a row exists, and the refusal names the input, not the rule.

    The name comes first because it is what the operator fixes. An answer that
    said only "schema violated" would send them to read the document to find out
    which order they got it wrong for.
    """
    workflow, bindings = publish_ordered_workflow(runtime)
    publish(runtime, NOT_A_SCHEMA)
    run_id = RunId("v3/refused")

    refused = start(runtime, workflow, bindings, run_id, *supplied)

    assert isinstance(refused, DurableV3StartInputRefused), refused
    assert refused.refusal is expected
    assert refused.name == expected_name
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(run_inputs_v3))
            == 0
        )


@pytest.mark.proves("an-order-the-start-cannot-honour-is-refused-by-its-own-name")
def test_an_order_whose_schema_is_not_a_schema_refuses_the_document(
    runtime: DbosRuntime,
) -> None:
    """The unreadable schema is refused where the reference is resolved.

    This is the case #215 could only name: there it needed a store contradicting
    its own document, so a test would have written the corruption it then proved.
    Here it is an ordinary published revision that is not a schema this product
    enforces, and the document that pins it is refused before any run exists.
    """
    publish(runtime, NOT_A_SCHEMA)
    workflow = WorkflowRevision(ordered_document(NOT_A_SCHEMA.revision_hash))
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    bindings = bind_cook(runtime)

    refused = start(
        runtime,
        workflow,
        bindings,
        RunId("v3/unreadable"),
        order(b'{"portions": 1}', NOT_A_SCHEMA),
    )

    assert isinstance(refused, DurableRunFormatNotExecutable), refused
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


@pytest.mark.proves("an-order-the-start-cannot-honour-is-refused-by-its-own-name")
def test_one_name_answers_one_order(runtime: DbosRuntime) -> None:
    """Two orders under one name is a request that says two things at once.

    Left to the store it would arrive as a primary-key collision and be reported
    as durable corruption, which names the store for what is wrong with the
    request. It is refused here instead, by the name that was supplied twice.
    """
    workflow, bindings = publish_ordered_workflow(runtime)

    refused = start(
        runtime,
        workflow,
        bindings,
        RunId("v3/twice"),
        order(b'{"portions": 2}'),
        order(b'{"portions": 4}'),
    )

    assert isinstance(refused, DurableV3StartInputRefused), refused
    assert refused.name == ORDER_NAME
    assert refused.refusal is V3InputRefusal.DUPLICATED
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
