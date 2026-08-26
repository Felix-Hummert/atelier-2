"""The committed `workflows/diff-review.yaml` is a real, executable document.

**What this file is about.** #392 found the catalog carried no `diff-review`
workflow at all -- only a hand-typed toy document inside a host CLI test,
never the durable definition source `docs/PRODUCT.md`'s Git-source intake
promises (#660). This suite is the fixture-side proof, one layer under
`tests/domain/test_authored_workflows.py`'s generic parse check: it loads the
exact bytes shipped at `workflows/diff-review.yaml` and `workflows/schemas/`,
publishes them through the same door an operator's Git-source import uses, and
launches a real run against them with a fake provider -- once with a finding
the document's own schema admits, and once for each shape of answer #392's
review criteria name as refused: an announcement instead of a finding (the
exact defect #392 measured against a live provider), a finding with no
verdict, and a finding whose verdict is not one of this product's two words.
The schema's refusal is proven on the committed document itself rather than
on an inline fixture that could quietly drift from it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
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
from atelier2.contracts.effects import AdapterRevision, EffectDestination
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
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV3
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionExisting,
)
from atelier2.ports.run_queries import NodeDetailFound
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2, agent_scratch_root
from tests.scenarios.api import durable_queries

WORKFLOWS_DIRECTORY = Path(__file__).parents[2] / "workflows"
DIFF_REVIEW_DOCUMENT = (WORKFLOWS_DIRECTORY / "diff-review.yaml").read_bytes()
DIFF_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "diff_review_diff.json").read_bytes(),
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

COMPLIANT_FINDING = (
    "Befund 1: workflows/diff-review.yaml adds one guard clause; nothing here "
    "blocks it.\nVerdict: accepted"
)
COMPLIANT_ANSWER = json.dumps(COMPLIANT_FINDING, ensure_ascii=False).encode()

REFUSED_FINDINGS = {
    "an announcement instead of a finding": (
        "Ich pruefe zuerst die Projektregeln und dann den Diff."
    ),
    "a finding with no verdict line": (
        "Befund 1: workflows/diff-review.yaml adds one guard clause."
    ),
    "a finding whose verdict is not one of the two product owns": (
        "Befund 1: workflows/diff-review.yaml adds one guard clause.\nVerdict: maybe"
    ),
}


def test_the_finding_schema_avoids_unsupported_regex_tokens_and_refuses_befund_10() -> (
    None
):
    """The provider's response-format regex engine rejects word boundaries."""
    schema = json.loads(FINDING_SCHEMA.document)
    pattern = schema["pattern"]
    schema_verdict = read_schema_document(FINDING_SCHEMA.document)

    assert all(
        token not in pattern for token in (r"\b", r"\B", "(?=", "(?!", "(?<=", "(?<!")
    )
    assert isinstance(schema_verdict, SchemaAccepted), schema_verdict

    accepted = read_instance_document(
        json.dumps("Befund 1: keine.\nVerdict: accepted").encode(), schema_verdict
    )
    refused = read_instance_document(
        json.dumps("Befund 10: keine.\nVerdict: accepted").encode(), schema_verdict
    )

    assert isinstance(accepted, InstanceAccepted)
    assert isinstance(refused, InstanceRefused)


def runtime_over(root: Path, provider: RecordingAgentExecutorFactoryV2) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "diff-review-test",
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
def provider(request: pytest.FixtureRequest) -> RecordingAgentExecutorFactoryV2:
    return RecordingAgentExecutorFactoryV2(
        "exact", "exact/v1", "exact-op", request.param
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


def publish_diff_review(
    runtime: DbosRuntime,
) -> tuple[WorkflowRevision, AgentBindingSet]:
    """Everything an operator's Git-source import would publish, from the shipped bytes."""
    store = DbosCatalogStore(runtime.engine)
    for revision in (DIFF_SCHEMA, FINDING_SCHEMA):
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
) -> object:
    return DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
    ).start_published(
        StartPublishedRunRequestV3(
            run_id,
            workflow.revision_hash,
            bindings,
            (RunInput("diff", DIFF_SCHEMA.revision_hash, DIFF_ORDER_VALUE),),
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
def test_a_compliant_finding_completes_the_run_with_the_diff_the_order_carried(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    workflow, bindings = publish_diff_review(runtime)
    run_id = RunId("v3/diff-review-accepted")

    created = start(runtime, workflow, bindings, run_id)
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    assert provider.opened is not None
    handed = provider.opened.requests[0].job_bytes
    assert b"--- order: diff ---" in handed
    assert DIFF_ORDER_VALUE in handed

    detail = durable_queries(runtime.engine).get_node_detail(run_id, "review")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.SUCCEEDED
    assert detail.detail.answer is not None
    assert detail.detail.answer.value == COMPLIANT_ANSWER


@pytest.mark.proves("bytes-their-own-schema-refuses-never-become-a-success")
@pytest.mark.parametrize(
    "provider",
    [
        pytest.param(json.dumps(text, ensure_ascii=False).encode(), id=case)
        for case, text in REFUSED_FINDINGS.items()
    ],
    indirect=True,
)
def test_a_finding_the_review_criteria_refuse_never_becomes_a_success(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    """The exact defects #392 named, each refused by the shipped schema.

    A reviewer that answers with a sentence about what it is about to do
    instead of leading with "Befund 1" produced the `COMPLETED` run with a
    semantically empty artifact #392 measured against a live provider. A
    reviewer that never states whether the diff stands, or names a verdict
    this product does not own, is exactly as unusable to an operator reading
    the run. All three are the schema-level guarantee the document now
    carries: none of these answers can ever write a success artifact for
    `review`, whatever the provider says.
    """
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
