"""The committed vision-variants workflow waits for an acknowledged reaction.

This is the catalog-side proof for #370: the shipped workflow takes the
operator's fragments, owners, and context, gives them to a headless visioner,
holds at a Wait for a closed JSON answer, and stores requirement sentences only
when they trace to that answer and write nothing to the repository.
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
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import runs, wait_answers
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_executable_workflow_document
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    UnanswerableWait,
    answer_wait_result,
)
from atelier2.application.compose_node_job import RESULT_HEADING
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
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.orders import ArtifactOrderValue, InlineOrderValue
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.runs import (
    FIRST_ROUND_ORDINAL,
    RunId,
    RunState,
    WorkflowRevision,
)
from atelier2.contracts.schemas_v3 import (
    InstanceAccepted,
    InstanceRefused,
    SchemaAccepted,
    read_instance_document,
    read_schema_document,
)
from atelier2.contracts.workflows_v3 import WaitNodeV3, WorkflowGraphV3
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
    answering_each_execution,
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
OPERATOR_ANSWER_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "vision_operator_answer.json").read_bytes(),
)
REQUIREMENT_SENTENCES_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (
        WORKFLOWS_DIRECTORY / "schemas" / "vision_requirement_sentences.json"
    ).read_bytes(),
)

FRAGMENTS_TEXT = (
    "Make our product vision easy to react to, even from a sentence or screenshot."
)
OWNER_DOCUMENTS_TEXT = "Requirements are traceable to an acknowledged operator answer."
CONTEXT_TEXT = "The owners win when they contradict a fragment."
FRAGMENTS = json.dumps(FRAGMENTS_TEXT, ensure_ascii=False).encode()
OWNER_DOCUMENTS = json.dumps(OWNER_DOCUMENTS_TEXT, ensure_ascii=False).encode()
CONTEXT = json.dumps(CONTEXT_TEXT, ensure_ascii=False).encode()

COMPLIANT_RESULT = {
    "variants": [
        {
            "id": "reaction-cards",
            "title": "Reaction cards",
            "sketch": "Show three concrete product directions side by side.",
            "tradeoffs": ["Fast comparison", "Needs visual production"],
        },
        {
            "id": "walkthrough",
            "title": "Walkthrough",
            "sketch": "Play one end-to-end operator flow for each direction.",
            "tradeoffs": ["Makes consequences tangible", "Takes longer to read"],
        },
    ],
    "decisions": [
        {
            "id": "first-reaction-cards",
            "question": "Should the first reaction be a choice between cards?",
            "default_answer": "Yes, present cards first.",
            "why": "A concrete choice is easier to answer than a blank prompt.",
        }
    ],
    "contradictions": [],
}
VISIONER_ANSWER = json.dumps(COMPLIANT_RESULT, ensure_ascii=False).encode()

OPERATOR_ANSWER_OBJECT = {
    "chosen_variant_ids": ["reaction-cards"],
    "decision_answers": [
        {
            "decision_id": "first-reaction-cards",
            "answer": "Yes, present cards first.",
        }
    ],
    "reaction": "Ship the cards first.",
}
OPERATOR_ANSWER = json.dumps(OPERATOR_ANSWER_OBJECT, ensure_ascii=False).encode()

COMPLIANT_SENTENCES = {
    "sentences": [
        {
            "text": "The first reaction is a choice between concrete variant cards.",
            "derives_from": {"kind": "variant", "id": "reaction-cards"},
            "owner_document": "docs/requirements/0003-ziel-ui.md",
            "traceable_to": Sha256Hash.of(OPERATOR_ANSWER).value,
        }
    ]
}
WRITER_ANSWER = json.dumps(COMPLIANT_SENTENCES, ensure_ascii=False).encode()
COMPLIANT_AGENT_ANSWERS = {
    ("develop_variants", FIRST_ROUND_ORDINAL): VISIONER_ANSWER,
    ("write_requirements", FIRST_ROUND_ORDINAL): WRITER_ANSWER,
}
SENTENCES_WITHOUT_TRACEABLE_TO = {
    "sentences": [
        {
            "text": "The first reaction is a choice between concrete variant cards.",
            "derives_from": {"kind": "variant", "id": "reaction-cards"},
            "owner_document": "docs/requirements/0003-ziel-ui.md",
        }
    ]
}

REFUSED_RESULTS = {
    "an object with one variant": {
        **COMPLIANT_RESULT,
        "variants": [COMPLIANT_RESULT["variants"][0]],
    },
    "an object with a decision without a default": {
        **COMPLIANT_RESULT,
        "decisions": [
            {
                "id": "first-reaction-cards",
                "question": "Should the first reaction be a choice between cards?",
                "why": "A concrete choice is easier to answer than a blank prompt.",
            }
        ],
    },
    "a variant missing id": {
        **COMPLIANT_RESULT,
        "variants": [
            {
                key: value
                for key, value in COMPLIANT_RESULT["variants"][0].items()
                if key != "id"
            },
            COMPLIANT_RESULT["variants"][1],
        ],
    },
    "a variant with an empty id": {
        **COMPLIANT_RESULT,
        "variants": [
            {**COMPLIANT_RESULT["variants"][0], "id": ""},
            COMPLIANT_RESULT["variants"][1],
        ],
    },
    "a decision missing id": {
        **COMPLIANT_RESULT,
        "decisions": [
            {
                key: value
                for key, value in COMPLIANT_RESULT["decisions"][0].items()
                if key != "id"
            }
        ],
    },
    "a decision with an empty id": {
        **COMPLIANT_RESULT,
        "decisions": [{**COMPLIANT_RESULT["decisions"][0], "id": ""}],
    },
}

UNANSWERABLE_WAIT_ANSWERS = {
    "missing chosen_variant_ids": {
        "decision_answers": [],
        "reaction": "cards first",
    },
    "empty chosen_variant_ids": {
        "chosen_variant_ids": [],
        "decision_answers": [],
        "reaction": "cards first",
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
        "exact",
        "exact/v1",
        "exact-op",
        b"",
        command=answering_each_execution(
            getattr(request, "param", COMPLIANT_AGENT_ANSWERS)
        ),
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
    for revision in (
        TEXT_SCHEMA,
        VISION_VARIANTS_SCHEMA,
        OPERATOR_ANSWER_SCHEMA,
        REQUIREMENT_SENTENCES_SCHEMA,
    ):
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
        (
            AgentBinding(AgentRole("visioner"), configuration.revision_hash),
            AgentBinding(AgentRole("requirement_writer"), configuration.revision_hash),
        )
    )
    return workflow, bindings


def artifact_order(runtime: DbosRuntime, name: str, content: bytes) -> AuthoredOrder:
    published = DbosArtifactStore(runtime.engine).publish_artifact(Artifact(content))
    assert isinstance(published, (ArtifactCreated, ArtifactExisting)), published
    return AuthoredOrder(name, ArtifactOrderValue(published.artifact.artifact_hash))


def start_orders(runtime: DbosRuntime) -> tuple[AuthoredOrder, ...]:
    return (
        artifact_order(runtime, "fragments", FRAGMENTS),
        artifact_order(runtime, "owner_documents", OWNER_DOCUMENTS),
        AuthoredOrder("context", InlineOrderValue(CONTEXT)),
    )


def start(
    runtime: DbosRuntime,
    workflow: WorkflowRevision,
    bindings: AgentBindingSet,
    run_id: RunId,
    *,
    authored_orders: tuple[AuthoredOrder, ...] | None = None,
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
                start_orders(runtime) if authored_orders is None else authored_orders
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


def answer_operator_reaction(
    runtime: DbosRuntime,
    workflow: WorkflowRevision,
    run_id: RunId,
    value: bytes,
) -> object:
    return answer_wait_result(
        run_id,
        workflow.revision_hash,
        "operator_reaction",
        value,
        DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
    )


def job_bytes_for(provider: RecordingAgentExecutorFactoryV2, node_id: str) -> bytes:
    assert provider.opened is not None
    matching = [
        request.job_bytes
        for request in provider.opened.requests
        if request.node_id == node_id
    ]
    assert len(matching) == 1
    return matching[0]


def node_detail(runtime: DbosRuntime, run_id: RunId, node_id: str) -> NodeDetailFound:
    detail = durable_queries(runtime.engine).get_node_detail(run_id, node_id)
    assert isinstance(detail, NodeDetailFound), detail
    return detail


def admit_instance(schema: PublishedRevision, instance: bytes) -> None:
    document = read_schema_document(schema.document)
    assert isinstance(document, SchemaAccepted), document
    assert isinstance(read_instance_document(instance, document), InstanceAccepted)


def refuse_instance(schema: PublishedRevision, instance: bytes) -> None:
    document = read_schema_document(schema.document)
    assert isinstance(document, SchemaAccepted), document
    assert isinstance(read_instance_document(instance, document), InstanceRefused)


def test_a_vision_variants_run_without_context_is_refused_before_a_run_exists(
    runtime: DbosRuntime,
) -> None:
    workflow, bindings = publish_vision_variants(runtime)
    refused = start(
        runtime,
        workflow,
        bindings,
        RunId("v3/vision-variants-without-context"),
        authored_orders=start_orders(runtime)[:-1],
    )
    assert isinstance(refused, DurableV3StartInputRefused), refused
    assert refused.name == "context"
    assert refused.refusal is V3InputRefusal.MISSING
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


def test_an_acknowledged_reaction_carries_the_vision_line_on_to_requirement_sentences(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    admit_instance(VISION_VARIANTS_SCHEMA, VISIONER_ANSWER)
    admit_instance(OPERATOR_ANSWER_SCHEMA, OPERATOR_ANSWER)
    admit_instance(REQUIREMENT_SENTENCES_SCHEMA, WRITER_ANSWER)

    workflow, bindings = publish_vision_variants(runtime)
    run_id = RunId("v3/vision-variants-accepted")

    created = start(runtime, workflow, bindings, run_id)
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.WAITING_INPUT)

    graph = parse_executable_workflow_document(VISION_VARIANTS_DOCUMENT)
    assert isinstance(graph, WorkflowGraphV3)
    wait_node = graph.node("operator_reaction")
    assert isinstance(wait_node, WaitNodeV3)
    wait_detail = node_detail(runtime, run_id, "operator_reaction")
    assert wait_detail.detail.state is NodeState.NEEDS_YOU
    wait_job = wait_detail.detail.job
    assert wait_job is not None
    assert wait_job == wait_node.prompt.encode()
    assert b"chosen_variant_ids" in wait_job
    assert b"decision_answers" in wait_job
    assert b"reaction" in wait_job
    assert b"acknowledged reaction" in wait_job
    assert b"preceding visioner result" in wait_job

    visioner_job = job_bytes_for(provider, "develop_variants")
    assert b"--- order: fragments ---" in visioner_job
    assert FRAGMENTS_TEXT.encode() in visioner_job
    assert b"--- order: owner_documents ---" in visioner_job
    assert OWNER_DOCUMENTS_TEXT.encode() in visioner_job
    assert b"--- order: context ---" in visioner_job
    assert CONTEXT_TEXT.encode() in visioner_job
    assert b"first hunt contradictions against the existing owners" in visioner_job
    assert b"variants are not questions" in visioner_job
    assert b"proposed default answer" in visioner_job
    assert b"Every variant and every decision must carry a nonempty id" in visioner_job
    visioner_detail = node_detail(runtime, run_id, "develop_variants")
    assert visioner_detail.detail.state is NodeState.SUCCEEDED
    assert visioner_detail.detail.answer is not None
    assert visioner_detail.detail.answer.value == VISIONER_ANSWER
    assert provider.opened is not None
    assert [request.node_id for request in provider.opened.requests] == [
        "develop_variants"
    ]

    accepted = answer_operator_reaction(runtime, workflow, run_id, OPERATOR_ANSWER)
    assert isinstance(accepted, AnswerAcceptedPending), accepted
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    wait_detail = node_detail(runtime, run_id, "operator_reaction")
    assert wait_detail.detail.state is NodeState.SUCCEEDED
    assert wait_detail.detail.answer is not None
    assert wait_detail.detail.answer.value == OPERATOR_ANSWER

    writer_job = job_bytes_for(provider, "write_requirements")
    assert (
        RESULT_HEADING.format(node="operator_reaction", name="answer").encode()
        in writer_job
    )
    assert OPERATOR_ANSWER in writer_job
    assert (
        RESULT_HEADING.format(node="develop_variants", name="result").encode()
        in writer_job
    )
    assert VISIONER_ANSWER in writer_job
    assert b"--- order: context ---" in writer_job
    assert CONTEXT_TEXT.encode() in writer_job
    assert b"--- order: owner_documents ---" in writer_job
    assert OWNER_DOCUMENTS_TEXT.encode() in writer_job
    assert b"Write nothing to the repository" in writer_job

    writer_detail = node_detail(runtime, run_id, "write_requirements")
    assert writer_detail.detail.state is NodeState.SUCCEEDED
    assert writer_detail.detail.answer is not None
    assert writer_detail.detail.answer.value == WRITER_ANSWER
    digest = Sha256Hash.of(wait_detail.detail.answer.value).value
    for sentence in json.loads(writer_detail.detail.answer.value)["sentences"]:
        assert sentence["traceable_to"] == digest


@pytest.mark.parametrize(
    "rejected",
    [
        pytest.param(json.dumps(payload, ensure_ascii=False).encode(), id=case)
        for case, payload in UNANSWERABLE_WAIT_ANSWERS.items()
    ],
)
def test_a_wait_answer_without_a_chosen_variant_leaves_the_run_waiting(
    runtime: DbosRuntime,
    rejected: bytes,
) -> None:
    refuse_instance(OPERATOR_ANSWER_SCHEMA, rejected)
    workflow, bindings = publish_vision_variants(runtime)
    run_id = RunId("v3/vision-variants-unanswerable")

    created = start(runtime, workflow, bindings, run_id)
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.WAITING_INPUT)

    refused = answer_operator_reaction(runtime, workflow, run_id, rejected)
    assert isinstance(refused, UnanswerableWait), refused
    with runtime.engine.connect() as connection:
        stored = connection.scalar(sa.select(sa.func.count()).select_from(wait_answers))
        state = connection.scalar(
            sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
        )
    assert stored == 0
    assert str(state) == RunState.WAITING_INPUT.value
    wait_detail = node_detail(runtime, run_id, "operator_reaction")
    assert wait_detail.detail.state is NodeState.NEEDS_YOU
    assert wait_detail.detail.answer is None

    accepted = answer_operator_reaction(runtime, workflow, run_id, OPERATOR_ANSWER)
    assert isinstance(accepted, AnswerAcceptedPending), accepted
    wait_for_state(runtime, run_id, RunState.COMPLETED)


@pytest.mark.parametrize(
    "provider",
    [
        {
            ("develop_variants", FIRST_ROUND_ORDINAL): VISIONER_ANSWER,
            ("write_requirements", FIRST_ROUND_ORDINAL): json.dumps(
                SENTENCES_WITHOUT_TRACEABLE_TO, ensure_ascii=False
            ).encode(),
        }
    ],
    indirect=True,
)
def test_a_writer_sentence_without_traceable_to_never_becomes_a_success(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    refuse_instance(
        REQUIREMENT_SENTENCES_SCHEMA,
        json.dumps(SENTENCES_WITHOUT_TRACEABLE_TO, ensure_ascii=False).encode(),
    )
    workflow, bindings = publish_vision_variants(runtime)
    run_id = RunId("v3/vision-variants-untraced")

    created = start(runtime, workflow, bindings, run_id)
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.WAITING_INPUT)
    accepted = answer_operator_reaction(runtime, workflow, run_id, OPERATOR_ANSWER)
    assert isinstance(accepted, AnswerAcceptedPending), accepted
    wait_for_state(runtime, run_id, RunState.FAILED)

    writer_detail = node_detail(runtime, run_id, "write_requirements")
    assert writer_detail.detail.state is NodeState.FAILED
    assert writer_detail.detail.answer is None
    assert writer_detail.detail.refusal is not None
    assert "output-schema-refused" in writer_detail.detail.refusal


@pytest.mark.parametrize(
    ("provider", "rejected"),
    [
        pytest.param(
            {
                ("develop_variants", FIRST_ROUND_ORDINAL): json.dumps(
                    result, ensure_ascii=False
                ).encode()
            },
            json.dumps(result, ensure_ascii=False).encode(),
            id=case,
        )
        for case, result in REFUSED_RESULTS.items()
    ],
    indirect=["provider"],
)
def test_a_vision_result_the_schema_refuses_never_becomes_a_success(
    runtime: DbosRuntime,
    provider: RecordingAgentExecutorFactoryV2,
    rejected: bytes,
) -> None:
    refuse_instance(VISION_VARIANTS_SCHEMA, rejected)
    workflow, bindings = publish_vision_variants(runtime)
    run_id = RunId("v3/vision-variants-refused")

    created = start(runtime, workflow, bindings, run_id)
    assert isinstance(created, DurableRunCreated), created

    runtime.launch()
    wait_for_state(runtime, run_id, RunState.FAILED)

    detail = node_detail(runtime, run_id, "develop_variants")
    assert detail.detail.state is NodeState.FAILED
    assert detail.detail.answer is None
    assert detail.detail.refusal is not None
    assert "output-schema-refused" in detail.detail.refusal
