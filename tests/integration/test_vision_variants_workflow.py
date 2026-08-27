"""The committed vision-variants workflow admits only its declared result object.

This is the catalog-side proof for #370: the shipped workflow takes the
operator's fragments and existing owner documents, gives both to one headless
agent, and stores its structured variants result only when that result satisfies
the schema published with the workflow.
"""

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
VISION_VARIANTS_DOCUMENT = (WORKFLOWS_DIRECTORY / "vision-variants.yaml").read_bytes()
TEXT_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "nonempty_string.json").read_bytes(),
)
VISION_VARIANTS_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "vision_variants_result.json").read_bytes(),
)

FRAGMENTS_TEXT = (
    "Make our product vision easy to react to, even from a sentence or screenshot."
)
OWNER_DOCUMENTS_TEXT = "Requirements are traceable to an acknowledged operator answer."
FRAGMENTS = json.dumps(FRAGMENTS_TEXT, ensure_ascii=False).encode()
OWNER_DOCUMENTS = json.dumps(OWNER_DOCUMENTS_TEXT, ensure_ascii=False).encode()

COMPLIANT_RESULT = {
    "variants": [
        {
            "title": "Reaction cards",
            "sketch": "Show three concrete product directions side by side.",
            "tradeoffs": ["Fast comparison", "Needs visual production"],
        },
        {
            "title": "Walkthrough",
            "sketch": "Play one end-to-end operator flow for each direction.",
            "tradeoffs": ["Makes consequences tangible", "Takes longer to read"],
        },
    ],
    "decisions": [
        {
            "question": "Should the first reaction be a choice between cards?",
            "default_answer": "Yes, present cards first.",
            "why": "A concrete choice is easier to answer than a blank prompt.",
        }
    ],
    "contradictions": [],
}
COMPLIANT_ANSWER = json.dumps(COMPLIANT_RESULT, ensure_ascii=False).encode()

REFUSED_RESULTS = {
    "an object with one variant": {
        **COMPLIANT_RESULT,
        "variants": [COMPLIANT_RESULT["variants"][0]],
    },
    "an object with a decision without a default": {
        **COMPLIANT_RESULT,
        "decisions": [
            {
                "question": "Should the first reaction be a choice between cards?",
                "why": "A concrete choice is easier to answer than a blank prompt.",
            }
        ],
    },
}


def runtime_over(
    root: Path,
    provider: RecordingAgentExecutorFactoryV2,
    scratch_root: Path,
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "vision-variants-test",
            agent_scratch_root=scratch_root,
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
        "exact", "exact/v1", "exact-op", request.param
    )


@pytest.fixture
def scratch_root_outside_a_worktree() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(
        prefix="atelier2-vision-variants-scratch-", dir="/var/tmp"
    ) as directory:
        yield Path(directory)


@pytest.fixture
def runtime(
    tmp_path: Path,
    provider: RecordingAgentExecutorFactoryV2,
    scratch_root_outside_a_worktree: Path,
) -> Iterator[DbosRuntime]:
    started = runtime_over(tmp_path, provider, scratch_root_outside_a_worktree)
    started.initialize_storage()
    try:
        yield started
    finally:
        started.close()


def publish_vision_variants(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    store = DbosCatalogStore(runtime.engine)
    for revision in (TEXT_SCHEMA, VISION_VARIANTS_SCHEMA):
        published = store.publish_revision(revision)
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
    publish_checked_model_registry(
        runtime.engine, ProviderId("exact"), (configuration,)
    )
    workflow = WorkflowRevision(VISION_VARIANTS_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("visioner"), configuration.revision_hash),)
    )
    return workflow, bindings


def artifact_order(runtime: DbosRuntime, name: str, content: bytes) -> AuthoredOrder:
    published = DbosArtifactStore(runtime.engine).publish_artifact(Artifact(content))
    assert isinstance(published, (ArtifactCreated, ArtifactExisting)), published
    return AuthoredOrder(name, ArtifactOrderValue(published.artifact.artifact_hash))


def start(
    runtime: DbosRuntime,
    workflow: WorkflowRevision,
    bindings: AgentBindingSet,
    run_id: RunId,
) -> object:
    return DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
    ).start_published(
        StartPublishedRunRequestV3(
            run_id,
            workflow.revision_hash,
            bindings,
            orders=(
                artifact_order(runtime, "fragments", FRAGMENTS),
                artifact_order(runtime, "owner_documents", OWNER_DOCUMENTS),
            ),
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


@pytest.mark.parametrize("provider", [COMPLIANT_ANSWER], indirect=True)
def test_a_compliant_vision_result_round_trips_through_the_headless_agent(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    workflow, bindings = publish_vision_variants(runtime)
    run_id = RunId("v3/vision-variants-accepted")

    created = start(runtime, workflow, bindings, run_id)
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    assert provider.opened is not None
    handed = provider.opened.requests[0].job_bytes
    assert b"--- order: fragments ---" in handed
    assert FRAGMENTS_TEXT.encode() in handed
    assert b"--- order: owner_documents ---" in handed
    assert OWNER_DOCUMENTS_TEXT.encode() in handed
    assert b"first hunt contradictions against the existing owners" in handed
    assert b"variants are not questions" in handed
    assert b"proposed default answer" in handed

    detail = durable_queries(runtime.engine).get_node_detail(run_id, "develop_variants")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.SUCCEEDED
    assert detail.detail.answer is not None
    assert detail.detail.answer.value == COMPLIANT_ANSWER


@pytest.mark.parametrize(
    "provider",
    [
        pytest.param(json.dumps(result, ensure_ascii=False).encode(), id=case)
        for case, result in REFUSED_RESULTS.items()
    ],
    indirect=True,
)
def test_a_vision_result_the_schema_refuses_never_becomes_a_success(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    workflow, bindings = publish_vision_variants(runtime)
    run_id = RunId("v3/vision-variants-refused")

    created = start(runtime, workflow, bindings, run_id)
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.FAILED)

    detail = durable_queries(runtime.engine).get_node_detail(run_id, "develop_variants")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.FAILED
    assert detail.detail.answer is None
    assert detail.detail.refusal is not None
    assert "output-schema-refused" in detail.detail.refusal
