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

import json
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest
import sqlalchemy as sa
import uvicorn
from fastapi import FastAPI

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.artifact_store import DbosArtifactStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import DbosHostConfigurationChannel
from atelier2.adapters.dbos.queue_projection_store import DbosQueueProjectionStore
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import (
    context_packages_v3,
    node_execution_requests_v3,
    node_receipts_v3,
    run_inputs_v3,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.markdown_agent_definitions import (
    parse_agent_definition,
    render_agent_definition,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.openapi import API_PREFIX
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
from atelier2.contracts.artifacts import (
    MAXIMUM_ARTIFACT_BYTES,
    Artifact,
    ArtifactHash,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.node_records_v3 import RunInput
from atelier2.contracts.orders import ArtifactOrderValue, InlineOrderValue
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.runs import (
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.schemas_v3 import MAXIMUM_INSTANCE_DOCUMENT_BYTES
from atelier2.host.run_command import (
    AgentBindingSource,
    RunOrder,
    SuppliedOrder,
    execute_run,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.artifacts import ArtifactCreated, ArtifactExisting
from atelier2.ports.durable_runs import (
    AuthoredOrder,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunFormatNotExecutable,
    DurableRunIdentityConflict,
    DurableV3StartInputRefused,
    StartPublishedRunRequestV3,
    V3InputRefusal,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    agent_scratch_root,
    dying,
)
from tests.scenarios.api import (
    api_limits,
    durable_api_client,
    durable_queries,
    event_poll_backoff,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

PORTIONS_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    b'{"type": "object", "properties": {"portions": {"type": "integer", '
    b'"minimum": 1}}, "required": ["portions"], "additionalProperties": false}',
)
NOT_A_SCHEMA = PublishedRevision(RevisionKind.SCHEMA, b'{"type": "nonsense"}')

ORDER_NAME = "order"
DYING_COOK_SAID = b"cook: this provider takes no order that long"


def ordered_document(schema_hash: PublishedRevisionHash) -> bytes:
    """One agent that reads one order the graph declares."""
    return (
        f"""format_version: 3
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
        + declared_output()
    )


@pytest.fixture
def cook(request: pytest.FixtureRequest) -> RecordingAgentExecutorFactoryV2:
    """The provider this run reaches, kept so the job it was handed can be read.

    The runtime opens its executors when it is built, before a test body runs,
    so a test whose subject is how the provider *ends* hands its command in
    here rather than assigning one afterwards. Without a parameter the provider
    is the child that writes the declared answer.
    """
    return RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-op",
        b'"cooked"',
        command=getattr(request, "param", None),
    )


@pytest.fixture
def runtime(
    tmp_path: Path, cook: RecordingAgentExecutorFactoryV2
) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "atelier.sqlite",
            "v3-order-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
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
    publish(runtime, schema, ANY_JSON_SCHEMA)
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
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
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


@pytest.mark.proves("a-run-input-binds-as-a-materialized-package-member")
def test_the_order_binds_into_the_persisted_context_package(
    runtime: DbosRuntime,
) -> None:
    """#38 sentence 2b, literally: the order is a material member of the package.

    The start persists the node's `context-package/v3`, and the stored manifest
    binds the content hash of the exact order bytes the run carries -- a hash a
    declared reference cannot produce and material can. The member is part of
    the package's identity: the same document started with a different order is
    a different package.
    """
    workflow, bindings = publish_ordered_workflow(runtime)
    first = RunId("v3/mit-order")
    second = RunId("v3/mit-anderer-order")
    assert isinstance(
        start(runtime, workflow, bindings, first, order(b'{"portions": 2}')),
        DurableRunCreated,
    )
    assert isinstance(
        start(runtime, workflow, bindings, second, order(b'{"portions": 3}')),
        DurableRunCreated,
    )

    revision = WorkflowRevisionHash(workflow.revision_hash.value)
    with runtime.engine.connect() as connection:
        packages = {
            run_id: _persisted_package(connection, run_id, revision)
            for run_id in (first, second)
        }
    first_hash, first_manifest = packages[first]
    second_hash, _second_manifest = packages[second]
    bound = order(b'{"portions": 2}').value_hash.value.encode("ascii")
    assert bound in first_manifest
    assert first_hash != second_hash


def _persisted_package(
    connection: sa.engine.Connection,
    run_id: RunId,
    revision: WorkflowRevisionHash,
) -> tuple[str, bytes]:
    """The stored package of this run's cook node: its hash and exact manifest."""
    package_hash = connection.scalar(
        sa.select(node_execution_requests_v3.c.context_package_hash).where(
            node_execution_requests_v3.c.node_execution_id
            == NodeExecutionId.for_node(run_id, revision, "cook").value
        )
    )
    manifest = connection.scalar(
        sa.select(context_packages_v3.c.manifest).where(
            context_packages_v3.c.package_hash == package_hash
        )
    )
    assert package_hash is not None and manifest is not None
    return str(package_hash), bytes(manifest)


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_an_authored_order_reaches_the_agent(
    runtime: DbosRuntime, cook: RecordingAgentExecutorFactoryV2
) -> None:
    """The operator door: a name and bytes, schema pin owned by the document.

    The #240 starts speak `run_inputs` (name, schema hash, bytes). After this
    head that field has no production producer. The public start and the CLI
    both send `orders`. A start that never reaches `_pin_authored_orders`
    stores nothing the agent can read.
    """
    workflow, bindings = publish_ordered_workflow(runtime)
    run_id = RunId("v3/authored")

    created = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
    ).start_published(
        StartPublishedRunRequestV3(
            run_id,
            workflow.revision_hash,
            bindings,
            orders=(AuthoredOrder(ORDER_NAME, InlineOrderValue(b'{"portions": 7}')),),
        )
    )
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    assert b'{"portions": 7}' in jobs_handed_to(cook)[0]


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_the_public_start_route_posts_the_order_the_document_declared(
    runtime: DbosRuntime,
) -> None:
    """The HTTP half: an `orders` body really reaches the start.

    This document declares a graph input, so a route that threw the body
    away would refuse the start as missing rather than store the value.
    """
    workflow, bindings = publish_ordered_workflow(runtime)
    binding = bindings.bindings[0]

    started = durable_api_client(runtime).post(
        API_PREFIX + "/runs",
        json={
            "workflow_format_version": 3,
            "run_id": "v3/route-order",
            "workflow_revision_hash": workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": binding.role.value,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash.value
                    ),
                }
            ],
            "orders": [{"name": ORDER_NAME, "value": '{"portions": 7}'}],
        },
    )

    assert started.status_code == 201
    with runtime.engine.connect() as connection:
        stored = (
            connection.execute(
                sa.select(run_inputs_v3.c.value).where(
                    run_inputs_v3.c.run_id == "v3/route-order"
                )
            )
            .scalars()
            .one()
        )
    assert bytes(stored) == b'{"portions": 7}'


@pytest.mark.proves("an-order-the-start-cannot-honour-is-refused-by-its-own-name")
def test_the_public_start_route_names_an_undeclared_order(
    runtime: DbosRuntime,
) -> None:
    """The HTTP half of the refusal the application seam already pinned.

    The table at the starter names `UNDECLARED`. A route that swallowed the
    token or the order name would still 422, and the operator would not know
    which input to drop.
    """
    workflow, bindings = publish_ordered_workflow(runtime)
    binding = bindings.bindings[0]

    refused = durable_api_client(runtime).post(
        API_PREFIX + "/runs",
        json={
            "workflow_format_version": 3,
            "run_id": "v3/undeclared-order",
            "workflow_revision_hash": workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": binding.role.value,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash.value
                    ),
                }
            ],
            "orders": [
                {"name": ORDER_NAME, "value": '{"portions": 7}'},
                {"name": "supper", "value": '{"portions": 1}'},
            ],
        },
    )

    assert refused.status_code == 422
    problem = refused.json()
    assert problem["type"] == "urn:atelier2:problem:v1:run-input-refused"
    assert "supper" in problem["detail"]
    assert V3InputRefusal.UNDECLARED.value in problem["detail"]
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


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
        pytest.param(
            (
                order(
                    json.dumps(
                        {"portions": "x" * MAXIMUM_INSTANCE_DOCUMENT_BYTES}
                    ).encode()
                ),
            ),
            V3InputRefusal.VALUE_REFUSED,
            ORDER_NAME,
            id="value-larger-than-this-stack-carries",
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


SIDE_NAME = "side"


def two_reader_document(schema_hash: PublishedRevisionHash) -> bytes:
    """Two orders and two readers: one node reads one, the next reads both."""
    return (
        f"""format_version: 3
name: Cook and plate to order
graph_inputs:
  - name: {ORDER_NAME}
    schema:
      ref: portions-schema
      revision: {schema_hash.value}
  - name: {SIDE_NAME}
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
{declared_output().decode()}  - id: plate
    type: agent
    role: cook
    mode: headless
    instruction: Plate what was cooked, with the side.
    depends_on: [cook]
    inputs:
      - name: {ORDER_NAME}
        from:
          graph_input: {ORDER_NAME}
      - name: {SIDE_NAME}
        from:
          graph_input: {SIDE_NAME}
""".encode()
        + declared_output()
    )


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_each_node_is_handed_the_orders_it_reads_in_one_stable_order(
    runtime: DbosRuntime, cook: RecordingAgentExecutorFactoryV2
) -> None:
    """A node is handed what it declared, and always spelled the same way.

    Two promises live here that one order and one reader cannot tell apart, and
    both are load-bearing once a run carries more than one order.

    **What it reads, not what the run carries.** `cook` declares one of the two
    orders. Handing it the other as well would tell an agent something its author
    never asked for -- and would carry it into the request hash, so the node's
    durable identity would depend on an order it does not read.

    **One spelling, whatever the caller's order.** `plate` reads both, and this
    start supplies them in the opposite order. The job is composed by name so a
    retry of the same run asks the same thing: a job that followed arrival order
    would give one node two identities.
    """
    publish(runtime, PORTIONS_SCHEMA, ANY_JSON_SCHEMA)
    workflow = WorkflowRevision(two_reader_document(PORTIONS_SCHEMA.revision_hash))
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    bindings = bind_cook(runtime)
    supper = RunInput(SIDE_NAME, PORTIONS_SCHEMA.revision_hash, b'{"portions": 9}')
    main = order(b'{"portions": 4}')

    created = start(runtime, workflow, bindings, RunId("v3/two-readers"), supper, main)
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, RunId("v3/two-readers"), RunState.COMPLETED)

    cooked, plated = jobs_handed_to(cook)
    assert b'{"portions": 4}' in cooked
    assert b'{"portions": 9}' not in cooked
    assert plated.index(b"--- order: order ---") < plated.index(b"--- order: side ---")


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_the_same_run_started_with_another_order_is_never_the_same_run(
    runtime: DbosRuntime,
) -> None:
    """The exact-retry answer is exact, or it is a lie about what was stored.

    A start that repeats a run id is answered as the run that already exists, and
    that answer is a promise: what you asked for is what is there. Revision,
    format and bindings were compared for exactly that reason -- the order was
    not, so a second start of the same id with a different order was told its run
    exists while the stored order was still the first one, and the agent would
    keep cooking the first order forever.
    """
    workflow, bindings = publish_ordered_workflow(runtime)
    run_id = RunId("v3/same-id")
    assert isinstance(
        start(runtime, workflow, bindings, run_id, order(b'{"portions": 2}')),
        DurableRunCreated,
    )

    answer = start(runtime, workflow, bindings, run_id, order(b'{"portions": 4}'))

    assert isinstance(answer, DurableRunIdentityConflict), answer
    with runtime.engine.connect() as connection:
        stored = (
            connection.execute(
                sa.select(run_inputs_v3.c.value).where(
                    run_inputs_v3.c.run_id == run_id.value
                )
            )
            .scalars()
            .one()
        )
    assert bytes(stored) == b'{"portions": 2}'


@pytest.mark.proves("a-run-carries-its-order-as-material-not-as-a-new-revision")
def test_the_same_run_started_with_the_same_order_stays_idempotent(
    runtime: DbosRuntime,
) -> None:
    """A repeat that really is a repeat is still answered as the same run."""
    workflow, bindings = publish_ordered_workflow(runtime)
    run_id = RunId("v3/same-order")
    assert isinstance(
        start(runtime, workflow, bindings, run_id, order(b'{"portions": 2}')),
        DurableRunCreated,
    )

    answer = start(runtime, workflow, bindings, run_id, order(b'{"portions": 2}'))

    assert isinstance(answer, DurableRunExisting), answer


def authored(value: bytes) -> AuthoredOrder:
    return AuthoredOrder(ORDER_NAME, InlineOrderValue(value))


@pytest.mark.proves("an-authored-retry-reports-the-existing-run")
def test_an_authored_retry_reports_the_existing_run(runtime: DbosRuntime) -> None:
    """The operator door, not the run_inputs helper the first retry tests used.

    `_supplied_orders` reads `run_inputs`. Production fills `orders`. A compare
    that only sees the first field treats every authored retry as a different
    order. Mutating the early check back to `_supplied_orders` alone turns this
    red.
    """
    workflow, bindings = publish_ordered_workflow(runtime)
    run_id = RunId("v3/authored-retry")
    first = start_authored(
        runtime, workflow, bindings, run_id, authored(b'{"portions": 2}')
    )
    assert isinstance(first, DurableRunCreated), first

    answer = start_authored(
        runtime, workflow, bindings, run_id, authored(b'{"portions": 2}')
    )

    assert isinstance(answer, DurableRunExisting), answer
    assert answer.run.run_id == first.run.run_id


@pytest.mark.proves("an-authored-start-with-another-order-stays-a-conflict")
def test_an_authored_start_with_another_order_stays_a_conflict(
    runtime: DbosRuntime,
) -> None:
    workflow, bindings = publish_ordered_workflow(runtime)
    run_id = RunId("v3/authored-conflict")
    assert isinstance(
        start_authored(
            runtime, workflow, bindings, run_id, authored(b'{"portions": 2}')
        ),
        DurableRunCreated,
    )

    answer = start_authored(
        runtime, workflow, bindings, run_id, authored(b'{"portions": 4}')
    )

    assert isinstance(answer, DurableRunIdentityConflict), answer
    with runtime.engine.connect() as connection:
        stored = (
            connection.execute(
                sa.select(run_inputs_v3.c.value).where(
                    run_inputs_v3.c.run_id == run_id.value
                )
            )
            .scalars()
            .one()
        )
    assert bytes(stored) == b'{"portions": 2}'


@pytest.mark.proves("an-authored-retry-reports-the-existing-run")
def test_the_public_start_route_answers_an_authored_retry_as_the_same_run(
    runtime: DbosRuntime,
) -> None:
    workflow, bindings = publish_ordered_workflow(runtime)
    binding = bindings.bindings[0]
    body = {
        "workflow_format_version": 3,
        "run_id": "v3/route-authored-retry",
        "workflow_revision_hash": workflow.revision_hash.value,
        "agent_bindings": [
            {
                "role": binding.role.value,
                "agent_configuration_revision_hash": (
                    binding.agent_configuration_revision_hash.value
                ),
            }
        ],
        "orders": [{"name": ORDER_NAME, "value": '{"portions": 7}'}],
    }
    client = durable_api_client(runtime)

    created = client.post(API_PREFIX + "/runs", json=body)
    retried = client.post(API_PREFIX + "/runs", json=body)

    assert created.status_code == 201, created.text
    assert retried.status_code == 200, retried.text
    assert created.json()["run_id"] == retried.json()["run_id"]
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(runs)
                .where(runs.c.run_id == "v3/route-authored-retry")
            )
            == 1
        )


def wait_for_receipt(
    runtime: DbosRuntime,
    run_id: RunId,
    revision: WorkflowRevisionHash,
    node_id: str,
) -> str:
    """The reason on one node's terminal receipt, once the launched run wrote it.

    A failing node leaves its run `STARTED`, so waiting for a run state would
    wait for something that never comes: the receipt is the terminal fact here.
    """
    execution_id = NodeExecutionId.for_node(run_id, revision, node_id).value
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            reason = connection.scalar(
                sa.select(node_receipts_v3.c.reason).where(
                    node_receipts_v3.c.node_execution_id == execution_id
                )
            )
        if reason is not None:
            return str(reason)
        time.sleep(0.025)
    raise AssertionError(f"node {node_id!r} of {run_id.value!r} never got a receipt")


@pytest.mark.proves("a-dead-process-ends-its-attempt-durably-named")
@pytest.mark.parametrize(
    "cook", (dying(3, DYING_COOK_SAID),), indirect=True, ids=("a child that dies",)
)
def test_a_provider_process_that_dies_leaves_the_run_a_readable_reason(
    runtime: DbosRuntime, cook: RecordingAgentExecutorFactoryV2
) -> None:
    """The whole vertical of the class that bit live, with a real dying child.

    A decoder answering a failure would prove the store and not the chain: what
    an operator was missing is the standard error of a process nobody kept, so
    the proof has to start at a child that really wrote it and really exited.
    """
    workflow, bindings = publish_ordered_workflow(runtime)
    run_id = RunId("v3/gestorbener-prozess")
    assert isinstance(
        start(runtime, workflow, bindings, run_id, order(b'{"portions": 4}')),
        DurableRunCreated,
    )

    runtime.launch()

    reason = wait_for_receipt(
        runtime, run_id, WorkflowRevisionHash(workflow.revision_hash.value), "cook"
    )
    assert reason.startswith("process-exited-unsuccessfully: ")
    assert "exited with code 3" in reason
    assert DYING_COOK_SAID.decode() in reason


def test_an_order_the_size_of_a_diff_review_reaches_the_agent_whole(
    runtime: DbosRuntime, cook: RecordingAgentExecutorFactoryV2
) -> None:
    """The measurement behind the `desk/review-pr313` class, taken here.

    Those runs died `PROCESS_EXITED_UNSUCCESSFULLY` with a diff-review order of
    this size while short orders went through, and the first guess was that the
    order never survived the way it is handed over. It does: neither provider
    puts a prompt in `argv`, this stack carries an order of that size whole into
    the job the agent is handed, and the run reaches its terminal state. What
    this stack does bound is the door -- an order past
    `MAXIMUM_INSTANCE_DOCUMENT_BYTES` is refused by name before a row exists,
    which is the neighbouring case above and not a silent death. The remaining
    suspect is therefore the provider itself, and the receipt this head writes
    is what will finally say so in the provider's own words.
    """
    workflow, bindings = publish_ordered_workflow(runtime, ANY_JSON_SCHEMA)
    diff = json.dumps({"diff": "x" * 14_000}).encode()
    run_id = RunId("v3/diff-review-order")
    assert isinstance(
        start(runtime, workflow, bindings, run_id, order(diff, ANY_JSON_SCHEMA)),
        DurableRunCreated,
    )

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    assert diff in jobs_handed_to(cook)[0]


def artifact_order(runtime: DbosRuntime, content: bytes) -> AuthoredOrder:
    """The order that says `read the artifact holding these bytes`."""
    published = DbosArtifactStore(runtime.engine).publish_artifact(Artifact(content))
    assert isinstance(published, (ArtifactCreated, ArtifactExisting)), published
    return AuthoredOrder(
        ORDER_NAME, ArtifactOrderValue(published.artifact.artifact_hash)
    )


def start_authored(
    runtime: DbosRuntime,
    workflow: WorkflowRevision,
    bindings: AgentBindingSet,
    run_id: RunId,
    *orders: AuthoredOrder,
) -> object:
    return DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
    ).start_published(
        StartPublishedRunRequestV3(
            run_id, workflow.revision_hash, bindings, orders=tuple(orders)
        )
    )


@pytest.mark.proves("a-full-pull-request-diff-reaches-its-agent-as-an-artifact")
def test_a_hundred_kilobyte_diff_reaches_its_agent_as_an_artifact_ordered_by_address(
    runtime: DbosRuntime, cook: RecordingAgentExecutorFactoryV2
) -> None:
    """The sentence this work exists for, end to end and byte exact.

    A diff of this size cannot be written into a start: it is six times the
    inline bound, which stays strict. Published as an artifact and ordered by
    its address, the same bytes travel -- and the agent is handed all of them,
    not a hash and not a truncation.

    The chain carries the artifact's identity without being told to: an order's
    stored `value_hash` is the SHA-256 of its bytes, and so is an artifact's
    address, so the row that binds this run to its material names the exact
    artifact it came from.
    """
    workflow, bindings = publish_ordered_workflow(runtime, ANY_JSON_SCHEMA)
    diff = json.dumps({"diff": "x" * 100_000}).encode()
    assert len(diff) > 6 * MAXIMUM_INSTANCE_DOCUMENT_BYTES
    ordered = artifact_order(runtime, diff)
    run_id = RunId("v3/artifact-diff-order")

    assert isinstance(
        start_authored(runtime, workflow, bindings, run_id, ordered), DurableRunCreated
    )

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    assert diff in jobs_handed_to(cook)[0]
    with runtime.engine.connect() as connection:
        stored = (
            connection.execute(
                sa.select(run_inputs_v3.c.value, run_inputs_v3.c.value_hash).where(
                    run_inputs_v3.c.run_id == run_id.value
                )
            )
            .mappings()
            .one()
        )
    assert bytes(stored["value"]) == diff
    assert isinstance(ordered.value, ArtifactOrderValue)
    assert str(stored["value_hash"]) == ordered.value.artifact_hash.value


@pytest.mark.proves("an-order-the-start-cannot-honour-is-refused-by-its-own-name")
def test_an_order_naming_an_unpublished_artifact_is_refused_before_any_row(
    runtime: DbosRuntime,
) -> None:
    """An address nobody published is a named refusal, never a run to clean up."""
    workflow, bindings = publish_ordered_workflow(runtime)
    run_id = RunId("v3/unknown-artifact")
    absent = ArtifactHash("ab" * 32)

    refused = start_authored(
        runtime,
        workflow,
        bindings,
        run_id,
        AuthoredOrder(ORDER_NAME, ArtifactOrderValue(absent)),
    )

    assert isinstance(refused, DurableV3StartInputRefused), refused
    assert refused.refusal is V3InputRefusal.UNKNOWN_ARTIFACT
    assert refused.name == ORDER_NAME
    assert absent.value in str(refused.detail)
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(runs)
                .where(runs.c.run_id == run_id.value)
            )
            == 0
        )


@pytest.mark.proves("an-order-the-start-cannot-honour-is-refused-by-its-own-name")
def test_an_inline_order_past_the_inline_bound_is_sent_to_the_artifact_door(
    runtime: DbosRuntime,
) -> None:
    """The inline bound stays strict, and its refusal says where the material goes.

    The same bytes are admitted as an artifact, so a refusal that only said
    "too large" would leave an operator believing this stack cannot carry them.
    """
    workflow, bindings = publish_ordered_workflow(runtime, ANY_JSON_SCHEMA)
    oversized = json.dumps({"diff": "x" * MAXIMUM_INSTANCE_DOCUMENT_BYTES}).encode()

    refused = start_authored(
        runtime,
        workflow,
        bindings,
        RunId("v3/inline-too-large"),
        AuthoredOrder(ORDER_NAME, InlineOrderValue(oversized)),
    )

    assert isinstance(refused, DurableV3StartInputRefused), refused
    assert refused.refusal is V3InputRefusal.VALUE_REFUSED
    assert "artifact" in str(refused.detail)


@pytest.mark.proves("a-full-pull-request-diff-reaches-its-agent-as-an-artifact")
def test_the_public_start_route_orders_material_by_its_published_address(
    runtime: DbosRuntime,
) -> None:
    """The operator door: publish the bytes, then start naming the address.

    Two routes and one mechanism -- the artifact this POST publishes is the
    material the start resolves, and nothing in between repeats the bytes.
    """
    workflow, bindings = publish_ordered_workflow(runtime, ANY_JSON_SCHEMA)
    binding = bindings.bindings[0]
    diff = json.dumps({"diff": "x" * 100_000}).encode()
    client = durable_api_client(runtime)

    published = client.post(
        API_PREFIX + "/artifacts",
        content=diff,
        headers={"content-type": "application/octet-stream"},
    )
    assert published.status_code == 201, published.text
    address = published.json()["artifact_hash"]

    started = client.post(
        API_PREFIX + "/runs",
        json={
            "workflow_format_version": 3,
            "run_id": "v3/route-artifact-order",
            "workflow_revision_hash": workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": binding.role.value,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash.value
                    ),
                }
            ],
            "orders": [{"name": ORDER_NAME, "artifact_hash": address}],
        },
    )

    assert started.status_code == 201, started.text
    assert (
        client.post(
            API_PREFIX + "/artifacts",
            content=diff,
            headers={"content-type": "application/octet-stream"},
        ).status_code
        == 200
    )
    with runtime.engine.connect() as connection:
        stored = (
            connection.execute(
                sa.select(run_inputs_v3.c.value).where(
                    run_inputs_v3.c.run_id == "v3/route-artifact-order"
                )
            )
            .scalars()
            .one()
        )
    assert bytes(stored) == diff


def test_a_body_past_the_artifact_bound_is_refused_before_the_route_reads_it(
    runtime: DbosRuntime,
) -> None:
    """The transport bound is the artifact's own, and it bites at the edge.

    Buffering material the store would refuse anyway is work an unauthenticated
    caller gets to spend for free, so the refusal is taken from the declared
    length before a byte of the body is read.
    """
    refused = durable_api_client(runtime).post(
        API_PREFIX + "/artifacts",
        content=b"x" * (MAXIMUM_ARTIFACT_BYTES + 1),
        headers={"content-type": "application/octet-stream"},
    )

    assert refused.status_code == 422
    assert refused.json()["type"].endswith("artifact-too-large")


def test_an_empty_artifact_is_refused_by_its_own_name(
    runtime: DbosRuntime,
) -> None:
    refused = durable_api_client(runtime).post(
        API_PREFIX + "/artifacts",
        content=b"",
        headers={"content-type": "application/octet-stream"},
    )

    assert refused.status_code == 422
    assert refused.json()["type"].endswith("artifact-empty")


COOK_BINDING_DOCUMENT = json.dumps(
    {
        "auth_profile": {
            "profile_id": "max",
            "revision_number": 1,
            "provider_id": "exact",
            "auth_mode": "subscription",
        },
        "model": "opus",
        "executor_revision": "exact/v1",
    }
).encode()


def application(runtime: DbosRuntime) -> FastAPI:
    queries = durable_queries(runtime.engine)
    catalog = DbosCatalogStore(runtime.engine)
    return create_app(
        source_commit="commit",
        source_tree="tree",
        ports=ApiPorts(
            workflow_revision_publisher=DbosWorkflowRevisionPublisher(runtime.engine),
            published_run_starter=DbosDurableRunStarter(
                runtime.engine,
                runtime.settings,
                runtime.agent_executor_registry,
                effect_adapter_proves_absence=True,
            ),
            wait_answerer=DbosWaitAnswerer(
                runtime.engine, runtime.settings.application_version
            ),
            reconcile_commander=DbosEffectReconcileCommander(
                runtime.engine, runtime.settings
            ),
            workflow_revision_queries=queries,
            run_queries=queries,
            run_event_queries=queries,
            workflow_document_parser=parse_workflow_document,
            agent_definition_parser=parse_agent_definition,
            agent_definition_renderer=render_agent_definition,
            agent_configuration_catalog=DbosAgentConfigurationCatalog(
                runtime.engine, runtime.agent_executor_registry
            ),
            agent_attempt_canceller=DbosAgentAttemptStore(
                runtime.engine, runtime.settings.application_version
            ),
            catalog_resolver=catalog,
            catalog_admissions=catalog,
            published_revision_registry=catalog,
            artifact_publisher=DbosArtifactStore(runtime.engine),
            host_configuration_channel=DbosHostConfigurationChannel(runtime.engine),
            queue_projection=DbosQueueProjectionStore(runtime.engine),
        ),
        limits=api_limits(),
        event_poll_backoff=event_poll_backoff(),
    )


@contextmanager
def live_server(app: FastAPI) -> Iterator[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host="127.0.0.1",
                port=0,
                log_level="critical",
                access_log=False,
                lifespan="off",
            )
        )
        thread = Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=5)
            raise AssertionError("Uvicorn did not start for the CLI retry proof")
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.proves("the-same-cli-command-twice-reports-one-run")
def test_the_same_cli_command_twice_reports_one_run(runtime: DbosRuntime) -> None:
    """The promise on `run --workflow`: twice is one run, not two bills."""
    publish_ordered_workflow(runtime)
    document = ordered_document(PORTIONS_SCHEMA.revision_hash)
    order = RunOrder(
        service_url="unused",
        workflow_document=document,
        bindings=(AgentBindingSource("cook", COOK_BINDING_DOCUMENT),),
        run_id=None,
        orders=(SuppliedOrder(ORDER_NAME, b'{"portions": 2}'),),
        catalog_activated_at="2026-08-18T00:00:00Z",
    )

    with live_server(application(runtime)) as service_url:
        runtime.launch()
        asked = RunOrder(
            service_url=service_url,
            workflow_document=order.workflow_document,
            bindings=order.bindings,
            run_id=order.run_id,
            orders=order.orders,
            catalog_activated_at=order.catalog_activated_at,
        )
        first = execute_run(asked)
        second = execute_run(asked)

    assert first.run_id == second.run_id
    assert first.public_run_reference == second.public_run_reference
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(runs)
                .where(runs.c.run_id == first.run_id)
            )
            == 1
        )
