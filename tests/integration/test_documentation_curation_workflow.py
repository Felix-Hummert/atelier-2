"""The committed documentation-curation workflow executes its candidate contract."""

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
    DurableV3StartInputRefused,
    StartPublishedRunRequestV3,
    V3InputRefusal,
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
CURATION_DOCUMENT = (WORKFLOWS_DIRECTORY / "documentation-curation.yaml").read_bytes()
FRESHNESS_REPORT_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (
        WORKFLOWS_DIRECTORY / "schemas" / "documentation_freshness_report.json"
    ).read_bytes(),
)
OWNER_SOURCES_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "documentation_owner_sources.json").read_bytes(),
)
CURRENT_DOCUMENTS_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (
        WORKFLOWS_DIRECTORY / "schemas" / "documentation_current_documents.json"
    ).read_bytes(),
)
TEXT_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "nonempty_string.json").read_bytes(),
)
CANDIDATE_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (
        WORKFLOWS_DIRECTORY / "schemas" / "documentation_curation_candidate.json"
    ).read_bytes(),
)

DOCUMENT_PATH = "docs/requirements/0004-runner-und-remote.md"
DOCUMENT_DIGEST = "a" * 64
OWNER_DIGEST = "b" * 64
SOURCE_CONTENT = "The source object requires a corrected derived view."
CURRENT_DOCUMENT_BYTES = "# Current derived view\n"
CONTEXT_TEXT = "Keep the derived view factual and name uncertainty."
FRESHNESS_REPORT = {
    "stale_documents": [
        {
            "path": DOCUMENT_PATH,
            "bound_document_digest": DOCUMENT_DIGEST,
            "source_thread": "github-issue:162",
            "watermark": {"kind": "issue_body_revision", "identifier": "body-v1"},
            "later_objects": [
                {
                    "reference": {"kind": "issue_comment", "identifier": "comment-2"},
                    "ordinal": 2,
                }
            ],
        }
    ],
    "current_documents": [],
    "unassessed_documents": [],
}
OWNER_SOURCES = {
    "objects": [
        {
            "source_thread": "github-issue:162",
            "reference": {"kind": "issue_comment", "identifier": "comment-2"},
            "digest": OWNER_DIGEST,
            "content_utf8": SOURCE_CONTENT,
        }
    ]
}
CURRENT_DOCUMENTS = {
    "documents": [
        {
            "path": DOCUMENT_PATH,
            "digest": DOCUMENT_DIGEST,
            "content_utf8": CURRENT_DOCUMENT_BYTES,
        }
    ]
}
CANDIDATE = {
    "stale_documents": [
        {
            "path": DOCUMENT_PATH,
            "expected_current_digest": DOCUMENT_DIGEST,
            "replacement_utf8_content": "# Corrected derived view\n",
            "cited_owner_objects": [
                {
                    "source_thread": "github-issue:162",
                    "reference": {
                        "kind": "issue_comment",
                        "identifier": "comment-2",
                    },
                    "digest": OWNER_DIGEST,
                }
            ],
        }
    ],
    "unresolved_questions": ["Which owner approves the revised wording?"],
}
ANSWER = json.dumps(CANDIDATE, ensure_ascii=False).encode()
REFUSED_CANDIDATES = {
    "extra candidate field": {**CANDIDATE, "writes_repository": False},
    "replacement missing its expected digest": {
        "stale_documents": [
            {
                "path": DOCUMENT_PATH,
                "replacement_utf8_content": "# Corrected derived view\n",
                "cited_owner_objects": CANDIDATE["stale_documents"][0][
                    "cited_owner_objects"
                ],
            }
        ],
        "unresolved_questions": [],
    },
}


def runtime_over(
    root: Path, provider: RecordingAgentExecutorFactoryV2, scratch_root: Path
) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "documentation-curation-test",
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
        "exact", "exact/v1", "exact-op", getattr(request, "param", ANSWER)
    )


@pytest.fixture
def runtime(
    tmp_path: Path, provider: RecordingAgentExecutorFactoryV2
) -> Iterator[DbosRuntime]:
    with tempfile.TemporaryDirectory(
        prefix="atelier2-documentation-curation-scratch-", dir="/var/tmp"
    ) as directory:
        started = runtime_over(tmp_path, provider, Path(directory))
        started.initialize_storage()
        try:
            yield started
        finally:
            started.close()


def publish(runtime: DbosRuntime) -> tuple[WorkflowRevision, AgentBindingSet]:
    store = DbosCatalogStore(runtime.engine)
    for revision in (
        FRESHNESS_REPORT_SCHEMA,
        OWNER_SOURCES_SCHEMA,
        CURRENT_DOCUMENTS_SCHEMA,
        TEXT_SCHEMA,
        CANDIDATE_SCHEMA,
    ):
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
    workflow = WorkflowRevision(CURATION_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (AgentBinding(AgentRole("curator"), configuration.revision_hash),)
    )


def artifact_order(runtime: DbosRuntime, name: str, content: bytes) -> AuthoredOrder:
    published = DbosArtifactStore(runtime.engine).publish_artifact(Artifact(content))
    assert isinstance(published, (ArtifactCreated, ArtifactExisting)), published
    return AuthoredOrder(name, ArtifactOrderValue(published.artifact.artifact_hash))


def orders(runtime: DbosRuntime) -> tuple[AuthoredOrder, ...]:
    return (
        artifact_order(
            runtime,
            "freshness_report",
            json.dumps(FRESHNESS_REPORT, ensure_ascii=False).encode(),
        ),
        artifact_order(
            runtime,
            "owner_sources",
            json.dumps(OWNER_SOURCES, ensure_ascii=False).encode(),
        ),
        artifact_order(
            runtime,
            "current_documents",
            json.dumps(CURRENT_DOCUMENTS, ensure_ascii=False).encode(),
        ),
        artifact_order(runtime, "context", json.dumps(CONTEXT_TEXT).encode()),
    )


def wait_for_state(runtime: DbosRuntime, run_id: RunId, state: RunState) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = connection.scalar(
                sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
            )
        if observed == state.value:
            return
        time.sleep(0.025)
    raise AssertionError(f"documentation curation run did not reach {state.value}")


def test_a_curator_without_context_is_refused_before_a_run_exists(
    runtime: DbosRuntime,
) -> None:
    workflow, bindings = publish(runtime)
    refused = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV3(
            RunId("v3/documentation-curation-without-context"),
            workflow.revision_hash,
            bindings,
            orders=orders(runtime)[:-1],
        )
    )
    assert isinstance(refused, DurableV3StartInputRefused), refused
    assert refused.name == "context"
    assert refused.refusal is V3InputRefusal.MISSING
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


@pytest.mark.proves(
    "a-curator-candidate-is-bound-to-exact-stale-view-and-owner-artifacts"
)
def test_a_curator_hands_exact_artifact_evidence_to_one_schema_bound_candidate(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    schema = read_schema_document(CANDIDATE_SCHEMA.document)
    assert isinstance(schema, SchemaAccepted), schema
    assert isinstance(read_instance_document(ANSWER, schema), InstanceAccepted)

    workflow, bindings = publish(runtime)
    run_id = RunId("v3/documentation-curation-candidate")
    created = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV3(
            run_id, workflow.revision_hash, bindings, orders=orders(runtime)
        )
    )
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    assert provider.opened is not None
    handed = provider.opened.requests[0].job_bytes
    artifact_payloads = (
        json.dumps(FRESHNESS_REPORT, ensure_ascii=False).encode(),
        json.dumps(OWNER_SOURCES, ensure_ascii=False).encode(),
        json.dumps(CURRENT_DOCUMENTS, ensure_ascii=False).encode(),
        CONTEXT_TEXT.encode(),
    )
    for payload in artifact_payloads:
        assert payload in handed
    detail = durable_queries(runtime.engine).get_node_detail(run_id, "curator")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.SUCCEEDED
    assert detail.detail.answer is not None
    assert detail.detail.answer.value == ANSWER


@pytest.mark.parametrize(
    "provider",
    [
        pytest.param(json.dumps(candidate, ensure_ascii=False).encode(), id=case)
        for case, candidate in REFUSED_CANDIDATES.items()
    ],
    indirect=True,
)
def test_a_candidate_with_an_extra_field_or_missing_digest_never_becomes_a_success(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    workflow, bindings = publish(runtime)
    run_id = RunId("v3/documentation-curation-refused")
    created = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV3(
            run_id, workflow.revision_hash, bindings, orders=orders(runtime)
        )
    )
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.FAILED)

    detail = durable_queries(runtime.engine).get_node_detail(run_id, "curator")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.FAILED
    assert detail.detail.answer is None
    assert detail.detail.refusal is not None
    assert "output-schema-refused" in detail.detail.refusal
