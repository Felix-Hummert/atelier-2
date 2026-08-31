"""The committed `breakdown` workflow executes its object result contract."""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.artifact_store import DbosArtifactStore
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
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
from atelier2.contracts.artifacts import Artifact
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.orders import ArtifactOrderValue
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.runs import RunId, RunState, WorkflowRevision
from atelier2.contracts.schemas_v3 import (
    InstanceAccepted,
    SchemaAccepted,
    read_instance_document,
    read_schema_document,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.artifacts import ArtifactCreated, ArtifactExisting
from atelier2.ports.durable_runs import (
    AuthoredOrder,
    DurableRunCreated,
    StartPublishedRunRequestV3,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import NodeDetailFound
from tests.scenarios.agents import (
    RecordingAgentExecutorFactoryV2,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_queries

WORKFLOWS_DIRECTORY = Path(__file__).parents[2] / "workflows"
BREAKDOWN_DOCUMENT = (WORKFLOWS_DIRECTORY / "breakdown.yaml").read_bytes()
TEXT_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "nonempty_string.json").read_bytes(),
)
RESULT_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "breakdown_result.json").read_bytes(),
)
ITEM_BODY_TEXT = "Build a catalog workflow for breakdown planning."
OWNER_DOCUMENTS_TEXT = "The workflow schema owns its output contract."
ITEM_BODY = json.dumps(ITEM_BODY_TEXT).encode()
OWNER_DOCUMENTS = json.dumps(OWNER_DOCUMENTS_TEXT).encode()
BREAKDOWN = {
    "slices": [
        {
            "title": "Publish the breakdown workflow",
            "files": ["workflows/breakdown.yaml"],
            "done_when": "The catalog admits the workflow.",
            "depends_on": [],
            "builder_class": "mechanical",
            "risk": [],
        }
    ],
    "contradictions": [],
    "verdict": "buildable",
}
ANSWER = json.dumps(BREAKDOWN, ensure_ascii=False).encode()
INVALID_BREAKDOWN = {
    "slices": [],
    "contradictions": [],
    "verdict": "approved",
}


def runtime_over(
    root: Path, provider: RecordingAgentExecutorFactoryV2, scratch_root: Path
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite", "breakdown-test", agent_scratch_root=scratch_root
        ),
        LoopbackEffectAdapterFactory(
            root / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
        (provider,),
    )


@pytest.fixture
def provider(request: pytest.FixtureRequest) -> RecordingAgentExecutorFactoryV2:
    return RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-op", getattr(request, "param", ANSWER)
    )


@pytest.fixture
def runtime(
    tmp_path: Path, provider: RecordingAgentExecutorFactoryV2
) -> Iterator[DbosRuntime]:
    with tempfile.TemporaryDirectory(
        prefix="atelier2-breakdown-scratch-", dir="/var/tmp"
    ) as directory:
        started = runtime_over(tmp_path, provider, Path(directory))
        started.initialize_storage()
        try:
            yield started
        finally:
            started.close()


def publish(runtime: DbosRuntime) -> tuple[WorkflowRevision, AgentBindingSet]:
    store = DbosCatalogStore(runtime.engine)
    for revision in (TEXT_SCHEMA, RESULT_SCHEMA):
        result = store.publish_revision(revision)
        assert isinstance(
            result, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), result
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
    workflow = WorkflowRevision(BREAKDOWN_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("planner"), configuration.revision_hash),)
    )


def artifact_order(runtime: DbosRuntime, name: str, content: bytes) -> AuthoredOrder:
    published = DbosArtifactStore(runtime.engine).publish_artifact(Artifact(content))
    assert isinstance(published, (ArtifactCreated, ArtifactExisting)), published
    return AuthoredOrder(name, ArtifactOrderValue(published.artifact.artifact_hash))


def wait_for_state(runtime: DbosRuntime, run_id: RunId, state: RunState) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            if (
                connection.scalar(
                    sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
                )
                == state.value
            ):
                return
        time.sleep(0.025)
    raise AssertionError(f"breakdown run did not reach {state.value}")


def start_breakdown(
    runtime: DbosRuntime,
    workflow: WorkflowRevision,
    bindings: AgentBindingSet,
    run_id: RunId,
) -> None:
    created = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV3(
            run_id,
            workflow.revision_hash,
            bindings,
            orders=(
                artifact_order(runtime, "item_body", ITEM_BODY),
                artifact_order(runtime, "owner_documents", OWNER_DOCUMENTS),
            ),
        )
    )
    assert isinstance(created, DurableRunCreated), created


def test_a_breakdown_round_trips_artifact_orders_to_an_object_result(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    schema = read_schema_document(RESULT_SCHEMA.document)
    assert isinstance(schema, SchemaAccepted), schema
    assert isinstance(read_instance_document(ANSWER, schema), InstanceAccepted)

    workflow, bindings = publish(runtime)
    run_id = RunId("v3/breakdown-object")
    start_breakdown(runtime, workflow, bindings, run_id)

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    assert provider.opened is not None
    handed = provider.opened.requests[0].job_bytes
    assert ITEM_BODY_TEXT.encode() in handed
    assert OWNER_DOCUMENTS_TEXT.encode() in handed
    detail = durable_queries(runtime.engine).get_node_detail(run_id, "plan")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.SUCCEEDED
    assert detail.detail.answer is not None
    assert detail.detail.answer.value == ANSWER


@pytest.mark.parametrize(
    "provider", [json.dumps(INVALID_BREAKDOWN).encode()], indirect=True
)
def test_an_invalid_breakdown_object_fails_admission(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    workflow, bindings = publish(runtime)
    run_id = RunId("v3/breakdown-refused")
    start_breakdown(runtime, workflow, bindings, run_id)

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.FAILED)

    detail = durable_queries(runtime.engine).get_node_detail(run_id, "plan")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.FAILED
    assert detail.detail.answer is None
    assert detail.detail.refusal is not None
    assert "output-schema-refused" in detail.detail.refusal
