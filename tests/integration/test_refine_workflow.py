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
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.dbos.schema import runs, wait_answers
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.application.answer_wait import AnswerAcceptedPending, answer_wait_result
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
from atelier2.contracts.executions import NodeExecutionId, WaitAnswerActor
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
from atelier2.contracts.verdicts import VERDICT_ANSWER_SCHEMA
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
    answering_each_execution,
    publish_checked_model_registry,
)
from tests.scenarios.api import durable_queries
from tests.scenarios.durable_state import (
    canonical_loopback_effects,
    canonical_runtime_settings,
)

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
RULING_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA,
    (WORKFLOWS_DIRECTORY / "schemas" / "refine_ruling.json").read_bytes(),
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
    "expectation": {
        "lens": "create_change_remove",
        "sentence": "Ein Projekt, das du anlegst, kannst du wieder entfernen.",
        "example": "Wenn du ein Projekt anlegst, dann kannst du es entfernen.",
        "counterexample": "Sagst du nein, dann bleibt es bestehen.",
        "technical": "A removal action needs the same project identity as creation.",
        "default": "yes",
        "status": "proposed",
    },
    "verdict": "needs_more",
}
ANSWER = json.dumps(REFINEMENT, ensure_ascii=False).encode()
SECOND_REFINEMENT = {
    **REFINEMENT,
    "expectation": {
        "lens": "identity",
        "sentence": "Ein Projekt bleibt dasselbe, auch wenn du seinen Namen änderst.",
        "example": "Wenn du den Namen änderst, dann bleibt es dein Projekt.",
        "counterexample": "Sagst du nein, dann entsteht ein neues Projekt.",
        "technical": "Persist identity separately from a mutable display name.",
        "default": "yes",
        "status": "proposed",
    },
    "verdict": "complete",
}
SECOND_ANSWER = json.dumps(SECOND_REFINEMENT, ensure_ascii=False).encode()
THIRD_REFINEMENT = {
    **SECOND_REFINEMENT,
    "expectation": {
        **SECOND_REFINEMENT["expectation"],
        "example": "Wenn du dein Projekt umbenennst, dann bleibt es dein Projekt.",
    },
}
THIRD_ANSWER = json.dumps(THIRD_REFINEMENT, ensure_ascii=False).encode()
RULE_YES = b'"ja"'
RULE_NO = b'"nein"'
RULE_SHOW_ME = b'"zeig-mir"'
CONVERSATION_ANSWERS = {
    ("refine", 1): ANSWER,
    ("decide_next_round", 1): b'{"verdict":"revise"}',
    ("refine", 2): SECOND_ANSWER,
    ("decide_next_round", 2): b'{"verdict":"revise"}',
    ("refine", 3): THIRD_ANSWER,
    ("decide_next_round", 3): b'{"verdict":"accepted"}',
}
INVALID_REFINEMENT = {
    "mirror": "Ich kann keinen Vorschlag machen.",
    "expectation": {},
    "verdict": "refused",
}


def runtime_over(
    root: Path, provider: RecordingAgentExecutorFactoryV2, scratch_root: Path
) -> DbosRuntime:
    return DbosRuntime(
        canonical_runtime_settings(root, "refine-test", scratch_root),
        canonical_loopback_effects(root),
        (provider,),
    )


@pytest.fixture
def provider(request: pytest.FixtureRequest) -> RecordingAgentExecutorFactoryV2:
    output = getattr(request, "param", ANSWER)
    return RecordingAgentExecutorFactoryV2(
        "exact",
        "exact/v1",
        "exact-op",
        b"" if isinstance(output, dict) else output,
        command=answering_each_execution(output) if isinstance(output, dict) else None,
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
    for revision in (TEXT_SCHEMA, RESULT_SCHEMA, RULING_SCHEMA, VERDICT_ANSWER_SCHEMA):
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


def wait_for_round(runtime: DbosRuntime, run_id: RunId, round_ordinal: int) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        with runtime.engine.connect() as connection:
            observed = connection.execute(
                sa.select(runs.c.state, runs.c.current_round_ordinal).where(
                    runs.c.run_id == run_id.value
                )
            ).one()
        if (str(observed.state), int(observed.current_round_ordinal)) == (
            RunState.WAITING_INPUT.value,
            round_ordinal,
        ):
            return
        time.sleep(0.025)
    raise AssertionError(f"refine run did not wait in round {round_ordinal}")


def rule_expectation(
    runtime: DbosRuntime,
    workflow: WorkflowRevision,
    run_id: RunId,
    round_ordinal: int,
    ruling: bytes,
) -> object:
    return answer_wait_result(
        run_id,
        workflow.revision_hash,
        "rule_expectation",
        NodeExecutionId.for_node(
            run_id, workflow.revision_hash, "rule_expectation", round_ordinal
        ),
        WaitAnswerActor.OPERATOR,
        ruling,
        DbosWaitAnswerer(runtime.engine, runtime.settings.application_version),
    )


@pytest.mark.proves(
    "a-refine-round-proposes-expectations-before-a-picture-or-breakdown"
)
def test_a_refine_round_returns_schema_valid_expectation_proposal(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    schema = read_schema_document(RESULT_SCHEMA.document)
    assert isinstance(schema, SchemaAccepted), schema
    assert isinstance(read_instance_document(ANSWER, schema), InstanceAccepted)

    workflow, bindings = publish(runtime)
    run_id = RunId("v3/refine-object")
    start_refine(runtime, workflow, bindings, run_id)

    runtime.launch()
    wait_for_round(runtime, run_id, 1)

    assert provider.opened is not None
    handed = provider.opened.requests[0].job_bytes
    assert VISION_TEXT.encode() in handed
    assert OWNER_DOCUMENTS_TEXT.encode() in handed
    detail = durable_queries(runtime.engine).get_node_detail(run_id, "refine")
    assert isinstance(detail, NodeDetailFound), detail
    assert detail.detail.state is NodeState.SUCCEEDED
    assert detail.detail.answer is not None
    assert detail.detail.answer.value == ANSWER


@pytest.mark.parametrize("provider", [CONVERSATION_ANSWERS], indirect=True)
@pytest.mark.proves("a-refine-run-rules-one-proposed-line-per-round")
def test_a_refine_run_waits_for_each_ruling_and_carries_it_to_the_next_round(
    runtime: DbosRuntime, provider: RecordingAgentExecutorFactoryV2
) -> None:
    workflow, bindings = publish(runtime)
    run_id = RunId("v3/refine-rulings")
    start_refine(runtime, workflow, bindings, run_id)

    runtime.launch()
    wait_for_round(runtime, run_id, 1)
    first_question = durable_queries(runtime.engine).get_node_detail(
        run_id, "rule_expectation"
    )
    assert isinstance(first_question, NodeDetailFound), first_question
    assert first_question.detail.job is not None
    assert ANSWER in first_question.detail.job
    assert isinstance(
        rule_expectation(runtime, workflow, run_id, 1, RULE_YES), AnswerAcceptedPending
    )

    wait_for_round(runtime, run_id, 2)
    assert provider.opened is not None
    first_round_decision = next(
        request.job_bytes
        for request in provider.opened.requests
        if (request.node_id, request.round_ordinal) == ("decide_next_round", 1)
    )
    assert ANSWER in first_round_decision
    assert RULE_YES in first_round_decision
    second_refine_job = next(
        request.job_bytes
        for request in provider.opened.requests
        if (request.node_id, request.round_ordinal) == ("refine", 2)
    )
    assert RULE_YES in second_refine_job
    second_question = durable_queries(runtime.engine).get_node_detail(
        run_id, "rule_expectation"
    )
    assert isinstance(second_question, NodeDetailFound), second_question
    assert second_question.detail.job is not None
    assert SECOND_ANSWER in second_question.detail.job
    assert isinstance(
        rule_expectation(runtime, workflow, run_id, 2, RULE_SHOW_ME),
        AnswerAcceptedPending,
    )
    wait_for_round(runtime, run_id, 3)
    second_round_decision = next(
        request.job_bytes
        for request in provider.opened.requests
        if (request.node_id, request.round_ordinal) == ("decide_next_round", 2)
    )
    assert SECOND_ANSWER in second_round_decision
    assert RULE_SHOW_ME in second_round_decision
    third_refine_job = next(
        request.job_bytes
        for request in provider.opened.requests
        if (request.node_id, request.round_ordinal) == ("refine", 3)
    )
    assert RULE_SHOW_ME in third_refine_job
    third_question = durable_queries(runtime.engine).get_node_detail(
        run_id, "rule_expectation"
    )
    assert isinstance(third_question, NodeDetailFound), third_question
    assert third_question.detail.job is not None
    assert THIRD_ANSWER in third_question.detail.job
    assert isinstance(
        rule_expectation(runtime, workflow, run_id, 3, RULE_NO), AnswerAcceptedPending
    )
    wait_for_state(runtime, run_id, RunState.COMPLETED)

    with runtime.engine.connect() as connection:
        run = (
            connection.execute(sa.select(runs).where(runs.c.run_id == run_id.value))
            .mappings()
            .one()
        )
        ruling_rounds = tuple(
            connection.execute(
                sa.select(wait_answers.c.round_ordinal)
                .where(wait_answers.c.run_id == run_id.value)
                .order_by(wait_answers.c.round_ordinal)
            ).scalars()
        )
    assert int(run["current_round_ordinal"]) == 3
    assert ruling_rounds == (1, 2, 3)


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
