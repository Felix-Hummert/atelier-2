"""The committed documentation-curation workflow executes its candidate contract."""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Iterator
from hashlib import sha256
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
    InstanceRefused,
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
    DurableUncastAgentRoles,
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
TRACE_REVIEW_RESULT_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (
        WORKFLOWS_DIRECTORY / "schemas" / "documentation_trace_review_result.json"
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
CANDIDATE_DIGEST = sha256(ANSWER).hexdigest()
TRACE_REVIEW = {
    "candidate_digest": CANDIDATE_DIGEST,
    "trace_judgements": [
        {
            "document": DOCUMENT_PATH,
            "judgement": "traced",
            "cited_owner_object": CANDIDATE["stale_documents"][0][
                "cited_owner_objects"
            ][0],
        }
    ],
    "findings": [],
    "verdict": "approve",
}
TRACE_REVIEW_ANSWER = json.dumps(TRACE_REVIEW, ensure_ascii=False).encode()
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
REFUSED_TRACE_REVIEWS = {
    "extra trace review field": {**TRACE_REVIEW, "writes_repository": False},
    "missing candidate digest": {
        key: value for key, value in TRACE_REVIEW.items() if key != "candidate_digest"
    },
    "revise without findings": {**TRACE_REVIEW, "verdict": "revise"},
    "cannot judge without reason": {**TRACE_REVIEW, "verdict": "cannot-judge"},
}
TRACE_REVIEW_SCHEMA_CASES = {
    "approve without findings": (TRACE_REVIEW, True),
    "revise with a finding": (
        {
            **TRACE_REVIEW,
            "findings": [
                {
                    "document": DOCUMENT_PATH,
                    "cited_owner_object": TRACE_REVIEW["trace_judgements"][0][
                        "cited_owner_object"
                    ],
                    "text": "The cited source does not support this replacement.",
                }
            ],
            "verdict": "revise",
        },
        True,
    ),
    "revise without findings": ({**TRACE_REVIEW, "verdict": "revise"}, False),
    "cannot judge without reason": (
        {**TRACE_REVIEW, "verdict": "cannot-judge"},
        False,
    ),
    "extra trace review field": ({**TRACE_REVIEW, "writes_repository": False}, False),
}


def runtime_over(
    root: Path,
    providers: tuple[RecordingAgentExecutorFactoryV2, ...],
    scratch_root: Path,
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
        providers,
    )


@pytest.fixture
def providers(
    request: pytest.FixtureRequest,
) -> tuple[RecordingAgentExecutorFactoryV2, RecordingAgentExecutorFactoryV2]:
    curator_answer, trace_review_answer = getattr(
        request, "param", (ANSWER, TRACE_REVIEW_ANSWER)
    )
    return (
        RecordingAgentExecutorFactoryV2(
            "curator", "curator/v1", "curator-op", curator_answer
        ),
        RecordingAgentExecutorFactoryV2(
            "trace-reviewer",
            "trace-reviewer/v1",
            "trace-reviewer-op",
            trace_review_answer,
        ),
    )


@pytest.fixture
def runtime(
    tmp_path: Path,
    providers: tuple[RecordingAgentExecutorFactoryV2, RecordingAgentExecutorFactoryV2],
) -> Iterator[DbosRuntime]:
    with tempfile.TemporaryDirectory(
        prefix="atelier2-documentation-curation-scratch-", dir="/var/tmp"
    ) as directory:
        started = runtime_over(tmp_path, providers, Path(directory))
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
        TRACE_REVIEW_RESULT_SCHEMA,
    ):
        result = store.publish_revision(revision)
        assert isinstance(
            result, (PublishedRevisionCreated, PublishedRevisionExisting)
        ), result
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    curator_auth = AuthProfileRevision(
        "curator-auth", 1, ProviderId("curator"), AuthMode.SUBSCRIPTION
    )
    reviewer_auth = AuthProfileRevision(
        "trace-reviewer-auth", 1, ProviderId("trace-reviewer"), AuthMode.SUBSCRIPTION
    )
    for auth in (curator_auth, reviewer_auth):
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
    curator_configuration = AgentConfigurationRevision(
        "opus",
        curator_auth.revision_hash,
        AgentExecutorRevision("curator/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    reviewer_configuration = AgentConfigurationRevision(
        "opus",
        reviewer_auth.revision_hash,
        AgentExecutorRevision("trace-reviewer/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    for configuration in (curator_configuration, reviewer_configuration):
        assert isinstance(
            catalog.publish_agent_configuration_revision(configuration),
            AgentConfigurationRevisionCreated,
        )
    publish_checked_model_registry(
        runtime.engine, ProviderId("curator"), (curator_configuration,)
    )
    publish_checked_model_registry(
        runtime.engine, ProviderId("trace-reviewer"), (reviewer_configuration,)
    )
    workflow = WorkflowRevision(CURATION_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return workflow, AgentBindingSet(
        (
            AgentBinding(AgentRole("curator"), curator_configuration.revision_hash),
            AgentBinding(
                AgentRole("trace-reviewer"), reviewer_configuration.revision_hash
            ),
        )
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


def test_a_trace_reviewer_without_a_different_provider_family_is_refused_before_a_run_exists(
    runtime: DbosRuntime,
) -> None:
    workflow, bindings = publish(runtime)
    curator_binding = bindings.bindings[0]
    same_family = AgentBindingSet(
        (
            curator_binding,
            AgentBinding(
                AgentRole("trace-reviewer"),
                curator_binding.agent_configuration_revision_hash,
            ),
        )
    )
    refused = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV3(
            RunId("v3/documentation-curation-same-provider-family"),
            workflow.revision_hash,
            same_family,
            orders=orders(runtime),
        )
    )
    assert isinstance(refused, DurableUncastAgentRoles), refused
    assert [(role.role, role.reason.value) for role in refused.roles] == [
        ("trace-reviewer", "family-difference-unavailable")
    ]
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


@pytest.mark.proves(
    "a-curator-candidate-is-bound-to-exact-stale-view-and-owner-artifacts"
)
@pytest.mark.proves(
    "an-independent-trace-review-is-bound-to-the-curator-candidate-and-owner-evidence"
)
def test_an_independent_trace_reviewer_receives_exact_curator_and_owner_evidence(
    runtime: DbosRuntime,
    providers: tuple[RecordingAgentExecutorFactoryV2, RecordingAgentExecutorFactoryV2],
) -> None:
    candidate_schema = read_schema_document(CANDIDATE_SCHEMA.document)
    assert isinstance(candidate_schema, SchemaAccepted), candidate_schema
    assert isinstance(
        read_instance_document(ANSWER, candidate_schema), InstanceAccepted
    )
    schema = read_schema_document(TRACE_REVIEW_RESULT_SCHEMA.document)
    assert isinstance(schema, SchemaAccepted), schema
    assert isinstance(
        read_instance_document(TRACE_REVIEW_ANSWER, schema), InstanceAccepted
    )

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

    curator_provider, trace_reviewer_provider = providers
    assert curator_provider.opened is not None
    assert trace_reviewer_provider.opened is not None
    assert len(curator_provider.opened.requests) == 1
    assert len(trace_reviewer_provider.opened.requests) == 1
    handed = curator_provider.opened.requests[0].job_bytes
    artifact_payloads = (
        json.dumps(FRESHNESS_REPORT, ensure_ascii=False).encode(),
        json.dumps(OWNER_SOURCES, ensure_ascii=False).encode(),
        json.dumps(CURRENT_DOCUMENTS, ensure_ascii=False).encode(),
        CONTEXT_TEXT.encode(),
    )
    for payload in artifact_payloads:
        assert payload in handed
    trace_handoff = trace_reviewer_provider.opened.requests[0].job_bytes
    assert b"--- order: context ---" in trace_handoff
    assert b"--- order: owner_sources ---" in trace_handoff
    assert b"--- result of curator: candidate ---" in trace_handoff
    assert CONTEXT_TEXT.encode() in trace_handoff
    assert json.dumps(OWNER_SOURCES, ensure_ascii=False).encode() in trace_handoff
    assert ANSWER in trace_handoff
    detail = durable_queries(runtime.engine).get_node_detail(run_id, "curator")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.SUCCEEDED
    assert detail.detail.answer is not None
    assert detail.detail.answer.value == ANSWER
    review_detail = durable_queries(runtime.engine).get_node_detail(
        run_id, "trace-review"
    )
    assert isinstance(review_detail, NodeDetailFound), review_detail
    assert review_detail.detail.state is NodeState.SUCCEEDED
    assert review_detail.detail.answer is not None
    assert review_detail.detail.answer.value == TRACE_REVIEW_ANSWER
    assert (
        json.loads(review_detail.detail.answer.value)["candidate_digest"]
        == CANDIDATE_DIGEST
    )


@pytest.mark.parametrize(
    ("payload", "admitted"),
    TRACE_REVIEW_SCHEMA_CASES.values(),
    ids=TRACE_REVIEW_SCHEMA_CASES.keys(),
)
def test_the_trace_review_schema_admits_only_honest_verdicts(
    payload: dict[str, object], admitted: bool
) -> None:
    schema = read_schema_document(TRACE_REVIEW_RESULT_SCHEMA.document)
    assert isinstance(schema, SchemaAccepted), schema
    verdict = read_instance_document(json.dumps(payload).encode(), schema)
    if admitted:
        assert isinstance(verdict, InstanceAccepted), verdict
    else:
        assert isinstance(verdict, InstanceRefused), verdict


@pytest.mark.parametrize(
    "providers",
    [
        pytest.param(
            (json.dumps(candidate, ensure_ascii=False).encode(), TRACE_REVIEW_ANSWER),
            id=case,
        )
        for case, candidate in REFUSED_CANDIDATES.items()
    ],
    indirect=True,
)
def test_a_candidate_with_an_extra_field_or_missing_digest_never_becomes_a_success(
    runtime: DbosRuntime,
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


@pytest.mark.parametrize(
    "providers",
    [
        pytest.param((ANSWER, json.dumps(verdict).encode()), id=case)
        for case, verdict in REFUSED_TRACE_REVIEWS.items()
    ],
    indirect=True,
)
def test_a_malformed_trace_verdict_never_becomes_a_success(
    runtime: DbosRuntime,
) -> None:
    workflow, bindings = publish(runtime)
    run_id = RunId("v3/documentation-curation-trace-verdict-refused")
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

    detail = durable_queries(runtime.engine).get_node_detail(run_id, "trace-review")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.FAILED
    assert detail.detail.answer is None
    assert detail.detail.refusal is not None
    assert "output-schema-refused" in detail.detail.refusal
