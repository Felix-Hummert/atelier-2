"""The committed `workflows/diff-review.yaml` is a real, executable document.

**What this file is about.** #392 found the catalog carried no `diff-review`
workflow at all -- only a hand-typed toy document inside a host CLI test,
never the durable definition source `docs/PRODUCT.md`'s Git-source intake
promises (#660). This suite is the fixture-side proof, one layer under
`tests/domain/test_authored_workflows.py`'s generic parse check: it loads the
exact bytes shipped at `workflows/diff-review.yaml` and `workflows/schemas/`,
publishes them through the same door an operator's Git-source import uses, and
launches a real run against them with a fake provider -- once with a review
object the document's own schema admits, and once for every incomplete or
invalid review object its contract refuses. The schema's refusal is proven on
the committed document itself rather than on an inline fixture that could
quietly drift from it.
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
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import (
    context_packages_v3,
    node_execution_requests_v3,
    runs,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
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
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import RunInput
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
from atelier2.ports.durable_runs import (
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
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)

WORKFLOWS_DIRECTORY = Path(__file__).parents[2] / "workflows"
DIFF_REVIEW_DOCUMENT = (WORKFLOWS_DIRECTORY / "diff-review.yaml").read_bytes()
DIFF_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "diff_review_diff.json").read_bytes(),
)
REVIEW_QUESTIONS_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "nonempty_string.json").read_bytes(),
)
FINDING_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "diff_review_finding.json").read_bytes(),
)

DIFF_ORDER_TEXT = (
    "diff --git a/workflows/diff-review.yaml b/workflows/diff-review.yaml\n"
    "+one guard clause added"
)
DIFF_ORDER_VALUE = json.dumps(DIFF_ORDER_TEXT, ensure_ascii=False).encode()
REVIEW_QUESTIONS_TEXT = "Does this diff leave an obsolete schema reference?"
REVIEW_QUESTIONS_VALUE = json.dumps(REVIEW_QUESTIONS_TEXT, ensure_ascii=False).encode()

COMPLIANT_REVIEW = {
    "findings": [
        {
            "file": "workflows/diff-review.yaml",
            "text": "The guard clause does not block this diff.",
        }
    ],
    "verdict": "accepted",
}
COMPLIANT_ANSWER = json.dumps(COMPLIANT_REVIEW, ensure_ascii=False).encode()

REFUSED_REVIEWS = {
    "an object with no verdict": {
        "findings": [{"file": "workflows/diff-review.yaml", "text": "Needs work."}]
    },
    "an object with a finding missing its text": {
        "findings": [{"file": "workflows/diff-review.yaml"}],
        "verdict": "revise",
    },
    "an object with an unknown verdict": {"findings": [], "verdict": "maybe"},
}


def test_the_review_schema_accepts_only_the_review_object_contract() -> None:
    schema = json.loads(FINDING_SCHEMA.document)
    schema_verdict = read_schema_document(FINDING_SCHEMA.document)

    assert schema["type"] == "object"
    assert schema["required"] == ["findings", "verdict"]
    assert isinstance(schema_verdict, SchemaAccepted), schema_verdict

    accepted = read_instance_document(
        json.dumps(COMPLIANT_REVIEW).encode(), schema_verdict
    )
    refused = read_instance_document(
        json.dumps(REFUSED_REVIEWS["an object with no verdict"]).encode(),
        schema_verdict,
    )

    assert isinstance(accepted, InstanceAccepted)
    assert isinstance(refused, InstanceRefused)


def runtime_over(
    root: Path,
    provider: RecordingAgentExecutorFactoryV2,
    scratch_root: Path,
) -> DbosRuntime:
    return DbosRuntime(
        canonical_runtime_settings(root, "diff-review-test", scratch_root),
        canonical_loopback_effects(root),
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
        prefix="atelier2-diff-review-scratch-", dir="/var/tmp"
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


def publish_diff_review(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    """Everything an operator's Git-source import would publish, from the shipped bytes."""
    store = DbosCatalogStore(runtime.engine)
    for revision in (DIFF_SCHEMA, REVIEW_QUESTIONS_SCHEMA, FINDING_SCHEMA):
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
    workflow = WorkflowRevision(DIFF_REVIEW_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    bindings = AgentBindingSet(
        (AgentBinding(AgentRole("reviewer"), configuration.revision_hash),)
    )
    return workflow, bindings


def start(
    runtime: DbosRuntime,
    workflow: WorkflowRevision,
    bindings: AgentBindingSet,
    run_id: RunId,
    inputs: tuple[RunInput, ...] | None = None,
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
            inputs
            if inputs is not None
            else (
                RunInput("diff", DIFF_SCHEMA.revision_hash, DIFF_ORDER_VALUE),
                RunInput(
                    "review_questions",
                    REVIEW_QUESTIONS_SCHEMA.revision_hash,
                    REVIEW_QUESTIONS_VALUE,
                ),
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
def test_a_compliant_review_completes_the_run_with_the_diff_the_order_carried(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    workflow, bindings = publish_diff_review(runtime)
    run_id = RunId("v3/diff-review-accepted")

    created = start(runtime, workflow, bindings, run_id)
    assert isinstance(created, DurableRunCreated), created

    with runtime.engine.connect() as connection:
        package_hash = connection.scalar(
            sa.select(node_execution_requests_v3.c.context_package_hash).where(
                node_execution_requests_v3.c.node_execution_id
                == NodeExecutionId.for_node(
                    run_id, workflow.revision_hash, "review"
                ).value,
            )
        )
        manifest = connection.scalar(
            sa.select(context_packages_v3.c.manifest).where(
                context_packages_v3.c.package_hash == package_hash
            )
        )
    assert manifest is not None
    assert Sha256Hash.of(DIFF_ORDER_VALUE).value.encode() in manifest
    assert Sha256Hash.of(REVIEW_QUESTIONS_VALUE).value.encode() in manifest

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    assert provider.opened is not None
    handed = provider.opened.requests[0].job_bytes
    assert b"--- order: diff ---" in handed
    assert DIFF_ORDER_TEXT.encode() in handed
    assert DIFF_ORDER_VALUE not in handed
    assert b"--- order: review_questions ---" in handed
    assert REVIEW_QUESTIONS_TEXT.encode() in handed
    assert REVIEW_QUESTIONS_VALUE not in handed
    assert b"cannot read files, run anything, or use tools" in handed
    assert b"the diff text is your only evidence" in handed
    assert b"Answer every question" in handed
    assert b"List findings with their file and text" in handed
    assert b"Accept the diff when nothing you find blocks it" in handed

    detail = durable_queries(runtime.engine).get_node_detail(run_id, "review")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.SUCCEEDED
    assert detail.detail.answer is not None
    assert detail.detail.answer.value == COMPLIANT_ANSWER


@pytest.mark.parametrize("provider", [COMPLIANT_ANSWER], indirect=True)
def test_a_review_without_questions_is_refused_before_a_run_exists(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    workflow, bindings = publish_diff_review(runtime)

    refused = start(
        runtime,
        workflow,
        bindings,
        RunId("v3/diff-review-without-questions"),
        (RunInput("diff", DIFF_SCHEMA.revision_hash, DIFF_ORDER_VALUE),),
    )

    assert isinstance(refused, DurableV3StartInputRefused), refused
    assert refused.refusal is V3InputRefusal.MISSING
    assert refused.name == "review_questions"
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


@pytest.mark.proves("bytes-their-own-schema-refuses-never-become-a-success")
@pytest.mark.parametrize(
    "provider",
    [
        pytest.param(json.dumps(review, ensure_ascii=False).encode(), id=case)
        for case, review in REFUSED_REVIEWS.items()
    ],
    indirect=True,
)
def test_a_review_object_the_schema_refuses_never_becomes_a_success(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    """An incomplete or invalid review object cannot write a success artifact."""
    workflow, bindings = publish_diff_review(runtime)
    run_id = RunId("v3/diff-review-refused")

    created = start(runtime, workflow, bindings, run_id)
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.FAILED)

    detail = durable_queries(runtime.engine).get_node_detail(run_id, "review")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.FAILED
    assert detail.detail.answer is None
    assert detail.detail.refusal is not None
    assert "output-schema-refused" in detail.detail.refusal
