"""A V3 agent document of the one admitted shape becomes a started run.

**What this head does not claim.** No provider is invoked, no attempt succeeds,
no `AGENT_COMPLETED` is written, no V3 receipt exists and nothing reaches a
terminal state. Those need the V3 arm in the attempt path (#194 H1c) and the
run-level terminal condition (#194 H1b), and each is named where it is missing
rather than implied by a green test here. What is proven is narrower and real:
the document is admitted, the run carries its own V3 truth, and its agent node
binds onto the durable attempt path with the exact role and configuration it was
started with.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
import sqlalchemy as sa
from dbos import DBOSClient

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.run_store import run_from_record_with_bindings
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import run_agent_bindings, runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.workflow import EncodedAgentBindingV2, _node_binding
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
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
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.run_bindings import RunV3
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import (
    DurableRunCreated,
    DurableRunExisting,
    DurableRunFormatNotExecutable,
    StartPublishedRunRequestV2,
)
from tests.scenarios.agents import failing_agent_executor_factory
from tests.scenarios.api import durable_api_client

ONE_AGENT_DOCUMENT = b"""format_version: 3
name: One agent
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
"""

EXECUTABLE_V2_DOCUMENT = b"""format_version: 2
start: implement
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: implement, type: agent, role: builder, job: build, next: done}
"""

RUN = RunId("v3/one-agent")


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    started = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "v3-start-test"),
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


def publish(
    runtime: DbosRuntime,
    document: bytes = ONE_AGENT_DOCUMENT,
    roles: tuple[str, ...] = ("builder",),
) -> tuple[WorkflowRevision, AgentBindingSet]:
    """Publish one V3 document and bind every role its nodes declare."""
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
        tuple(
            AgentBinding(AgentRole(role), configuration.revision_hash) for role in roles
        )
    )


@pytest.mark.proves("a-v3-agent-document-starts-and-binds-its-node")
def test_a_v3_agent_document_of_the_admitted_shape_starts(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The foundation is durable and inert: a run exists, and nothing runs it."""
    workflow, bindings = publish(runtime)

    def unexpected_enqueue(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the V3 foundation enqueued work no runtime executes")

    monkeypatch.setattr(DBOSClient, "enqueue_in_transaction", unexpected_enqueue)

    result = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_v3_foundation(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )

    assert isinstance(result, DurableRunCreated)
    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
    assert int(record["workflow_format_version"]) == 3
    assert str(record["current_node_id"]) == "implement"
    assert RunState(str(record["state"])) is RunState.STARTED
    assert record["terminal_hash"] is None


@pytest.mark.proves("a-v3-agent-document-starts-and-binds-its-node")
def test_a_started_v3_run_reads_back_as_its_own_shape(runtime: DbosRuntime) -> None:
    """Not a V2 run wearing a new number: a V3 row is a V3 run."""
    workflow, bindings = publish(runtime)
    DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_v3_foundation(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )

    with runtime.engine.connect() as connection:
        record = (
            connection.execute(sa.select(runs).where(runs.c.run_id == RUN.value))
            .mappings()
            .one()
        )
        run = run_from_record_with_bindings(connection, record)

    assert isinstance(run, RunV3)
    assert [binding.role.value for binding in run.agent_bindings] == ["builder"]
    assert run.binding_set_hash == bindings.binding_set_hash


@pytest.mark.proves("a-v3-agent-document-starts-and-binds-its-node")
def test_the_v3_agent_node_binds_with_the_exact_role_and_configuration(
    runtime: DbosRuntime,
) -> None:
    """The binding replays what the run was started with, field for field."""
    workflow, bindings = publish(runtime)
    DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_v3_foundation(
        StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)
    )

    encoded = _node_binding(
        runtime.datasource, RUN, workflow.revision_hash, "implement"
    )
    binding = cast(EncodedAgentBindingV2, encoded)

    assert binding["type"] == "agent-v2"
    assert binding["role"] == "builder"
    assert binding["job"] == "Do the one thing this chain is for."
    assert binding["model"] == "opus"
    assert binding["executor_revision"] == "exact/v1"
    assert (
        binding.get("requested_capability") == AgentExecutionCapability.HEADLESS.value
    )
    assert (
        binding["configuration_hash"]
        == bindings.bindings[0].agent_configuration_revision_hash.value
    )


@pytest.mark.proves("a-public-start-refuses-a-v3-revision-before-any-write")
def test_the_public_start_route_refuses_a_v3_revision_with_no_row_and_no_enqueue(
    runtime: DbosRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route that starts runs must not half-start one it cannot finish.

    Admitting a V3 revision wrote the run and enqueued its bootstrap, and only
    then failed on the missing wire resource -- a public 500 *after* durable
    state existed, with the run left poisoning every later projection. The claim
    is about the composed HTTP boundary, so it is asked of the route and not of a
    starter called by hand.
    """
    workflow, bindings = publish(runtime)

    def unexpected_enqueue(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("a refused start reached the durable queue")

    monkeypatch.setattr(DBOSClient, "enqueue_in_transaction", unexpected_enqueue)

    refused = durable_api_client(runtime).post(
        API_PREFIX + "/runs",
        json={
            "workflow_format_version": 2,
            "run_id": RUN.value,
            "workflow_revision_hash": workflow.revision_hash.value,
            "agent_bindings": [
                {
                    "role": binding.role.value,
                    "agent_configuration_revision_hash": (
                        binding.agent_configuration_revision_hash.value
                    ),
                }
                for binding in bindings.bindings
            ],
        },
    )

    assert refused.status_code == 409
    assert refused.json()["type"].endswith(":workflow-format-not-executable")
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(run_agent_bindings)
            )
            == 0
        )
        assert connection.scalar(sa.text("SELECT COUNT(*) FROM workflow_status")) == 0


@pytest.mark.proves("a-public-start-refuses-a-v3-revision-before-any-write")
def test_the_v3_foundation_seam_refuses_a_document_of_any_other_format(
    runtime: DbosRuntime,
) -> None:
    """The inner door is the outer one's mirror, not its opposite by convention.

    The public seam refuses V3; this one exists only for V3, and a document of
    another format reaching it wrote a durable STARTED run that enqueues nothing
    -- a run no runtime would ever pick up. The V1 request form cannot reach here
    at all: the signature takes the bound form, so that half is a type error
    rather than a runtime answer.
    """
    _workflow, bindings = publish(runtime)
    executable = WorkflowRevision(EXECUTABLE_V2_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(executable)

    result = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_v3_foundation(
        StartPublishedRunRequestV2(RUN, executable.revision_hash, bindings)
    )

    assert isinstance(result, DurableRunFormatNotExecutable)
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(run_agent_bindings)
            )
            == 0
        )


@pytest.mark.proves("a-v3-agent-document-starts-and-binds-its-node")
def test_the_first_start_and_its_retry_answer_the_same_run_shape(
    runtime: DbosRuntime,
) -> None:
    """A first start used to answer RunV2 while its retry answered RunV3."""
    workflow, bindings = publish(runtime)
    starter = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    )
    request = StartPublishedRunRequestV2(RUN, workflow.revision_hash, bindings)

    created = starter.start_v3_foundation(request)
    existing = starter.start_v3_foundation(request)

    assert isinstance(created, DurableRunCreated)
    assert isinstance(existing, DurableRunExisting)
    assert isinstance(created.run, RunV3)
    assert isinstance(existing.run, RunV3)
    assert created.run == existing.run
