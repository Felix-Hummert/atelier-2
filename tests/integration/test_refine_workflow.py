"""The committed `refine` workflow executes its object result contract."""

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
REFINE_DOCUMENT = (WORKFLOWS_DIRECTORY / "refine.yaml").read_bytes()
TEXT_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "nonempty_string.json").read_bytes(),
)
RESULT_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "refine_result.json").read_bytes(),
)
VISION_TEXT = "Ich möchte für Projekte einen verständlichen Einstellungsbereich."
OWNER_DOCUMENTS_TEXT = "Projekte haben Namen und sichtbare Einstellungen."
VISION = json.dumps(VISION_TEXT).encode()
OWNER_DOCUMENTS = json.dumps(OWNER_DOCUMENTS_TEXT).encode()
REFINEMENT = {
    "mirror": (
        "So habe ich dich verstanden: Du willst Projekte einfach einstellen. "
        "Die Einstellungen sollen für Menschen verständlich bleiben."
    ),
    "rounds": 1,
    "expectations": [
        {
            "lens": "create_change_remove",
            "sentence": "Ein Projekt, das du anlegst, kannst du wieder entfernen.",
            "example": "Wenn du ein Projekt anlegst, dann kannst du es entfernen.",
            "counterexample": "Sagst du nein, dann bleibt es bestehen.",
            "technical": "A removal action needs the same project identity as creation.",
            "default": "yes",
            "status": "proposed",
        },
        {
            "lens": "identity",
            "sentence": "Ein Projekt bleibt dasselbe, auch wenn du seinen Namen änderst.",
            "example": "Wenn du den Namen änderst, dann bleibt es dein Projekt.",
            "counterexample": "Sagst du nein, dann entsteht ein neues Projekt.",
            "technical": "Persist identity separately from a mutable display name.",
            "default": "yes",
            "status": "proposed",
        },
        {
            "lens": "states",
            "sentence": "Du siehst, wenn noch keine Einstellung da ist oder etwas nicht klappt.",
            "example": "Wenn du noch keine Einstellung hast, dann siehst du das sofort.",
            "counterexample": "Sagst du nein, dann bleibt der Zustand offen.",
            "technical": "Represent empty and failed states explicitly in the surface.",
            "default": "yes",
            "status": "proposed",
        },
        {
            "lens": "secrets_and_rights",
            "sentence": "Nur Menschen mit Erlaubnis können geheime Einstellungen sehen oder ändern.",
            "example": "Wenn du die Erlaubnis hast, dann kannst du die Einstellung ändern.",
            "counterexample": "Sagst du nein, dann bleibt sie verborgen.",
            "technical": "Authorize reads and writes before disclosing secret values.",
            "default": "yes",
            "status": "proposed",
        },
        {
            "lens": "undo",
            "sentence": "Du kannst eine Änderung zurücknehmen, bevor sie dauerhaft gilt.",
            "example": "Wenn du dich umentscheidest, dann kannst du zurückgehen.",
            "counterexample": "Sagst du nein, dann bleibt die Änderung bestehen.",
            "technical": "Keep an explicit cancellation path before durable persistence.",
            "default": "later",
            "status": "proposed",
        },
        {
            "lens": "scale",
            "sentence": "Die Einstellungen bleiben auch bei vielen Projekten übersichtlich.",
            "example": "Wenn du viele Projekte hast, dann findest du das richtige wieder.",
            "counterexample": "Sagst du nein, dann musst du lange suchen.",
            "technical": "The listing needs a bounded way to find a project among many.",
            "default": "later",
            "status": "proposed",
        },
    ],
    "lenses_without_lines": [],
    "verdict": "complete",
}
ANSWER = json.dumps(REFINEMENT, ensure_ascii=False).encode()
INVALID_REFINEMENT = {
    "mirror": "Ich kann keinen Vorschlag machen.",
    "rounds": 2,
    "expectations": [],
    "lenses_without_lines": [],
    "verdict": "refused",
}


def runtime_over(
    root: Path, provider: RecordingAgentExecutorFactoryV2, scratch_root: Path
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite", "refine-test", agent_scratch_root=scratch_root
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
def provider(request: pytest.FixtureRequest) -> RecordingAgentExecutorFactoryV2:
    return RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-op", getattr(request, "param", ANSWER)
    )


@pytest.fixture
def runtime(
    tmp_path: Path, provider: RecordingAgentExecutorFactoryV2
) -> Iterator[DbosRuntime]:
    with tempfile.TemporaryDirectory(
        prefix="atelier2-refine-scratch-", dir="/var/tmp"
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
    workflow = WorkflowRevision(REFINE_DOCUMENT)
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
    raise AssertionError(f"refine run did not reach {state.value}")


def start_refine(
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
                artifact_order(runtime, "vision", VISION),
                artifact_order(runtime, "owner_documents", OWNER_DOCUMENTS),
            ),
        )
    )
    assert isinstance(created, DurableRunCreated), created


@pytest.mark.proves(
    "a-refine-round-proposes-expectations-before-a-picture-or-breakdown"
)
def test_a_refine_round_returns_schema_valid_expectation_proposals(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    schema = read_schema_document(RESULT_SCHEMA.document)
    assert isinstance(schema, SchemaAccepted), schema
    assert isinstance(read_instance_document(ANSWER, schema), InstanceAccepted)

    workflow, bindings = publish(runtime)
    run_id = RunId("v3/refine-object")
    start_refine(runtime, workflow, bindings, run_id)

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    assert provider.opened is not None
    handed = provider.opened.requests[0].job_bytes
    assert VISION_TEXT.encode() in handed
    assert OWNER_DOCUMENTS_TEXT.encode() in handed
    detail = durable_queries(runtime.engine).get_node_detail(run_id, "refine")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.SUCCEEDED
    assert detail.detail.answer is not None
    assert detail.detail.answer.value == ANSWER


@pytest.mark.parametrize(
    "provider", [json.dumps(INVALID_REFINEMENT).encode()], indirect=True
)
def test_a_schema_invalid_refine_answer_fails_admission(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    workflow, bindings = publish(runtime)
    run_id = RunId("v3/refine-refused")
    start_refine(runtime, workflow, bindings, run_id)

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.FAILED)

    detail = durable_queries(runtime.engine).get_node_detail(run_id, "refine")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.FAILED
    assert detail.detail.answer is None
    assert detail.detail.refusal is not None
    assert "output-schema-refused" in detail.detail.refusal
