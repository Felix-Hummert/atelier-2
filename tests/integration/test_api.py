from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from atelier2.adapters.dbos.advancer import DbosDurableRunAdvancer, graph_action_intent
from atelier2.adapters.dbos.effect_store import commit_resolution, encode_found
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.reconciler import (
    DbosEffectReconcileCommander,
    reconcile_workflow_id_for,
)
from atelier2.adapters.dbos.run_store import (
    DbosWaitAnswerer,
    commit_agent_completed,
    commit_reconciliation_required,
    commit_waiting_input,
)
from atelier2.adapters.dbos.runtime import (
    DbosRuntime,
    DbosRuntimeSettings,
    canonical_write_transaction,
)
from atelier2.adapters.dbos.schema import (
    effect_intents,
    reconcile_commands,
    runs,
    wait_answers,
    workflow_revisions,
)
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
    bootstrap_workflow_id_for,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.api.app import ApiPorts, create_app
from atelier2.api.references import encode_canonical_base64, encode_public_run_reference
from atelier2.application.advance_run import advance_run
from atelier2.application.start_published_run import StartPublishedRunRequest
from atelier2.contracts.effects import (
    AdapterRevision,
    ConfirmationSource,
    EffectDestination,
    EffectId,
    EffectIntent,
    EffectIntentState,
    EffectIntentStateVersion,
    EffectResult,
    OperatorFoundEffect,
    PerformedEffect,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    SubmitWaitAnswerRequest,
    answer_workflow_id_for,
)
from atelier2.contracts.runs import (
    RunId,
    StartRunRequest,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.durable_runs import (
    DurableAnswerCreated,
    DurableAnswerExisting,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunRevisionMissing,
)
from atelier2.ports.effects import (
    DurableReconciliationCommandConflict,
    DurableReconciliationCreated,
    DurableReconciliationDeterminationConflict,
    DurableReconciliationExisting,
)
from atelier2.ports.workflow_revisions import (
    DurableRevisionCreated,
    DurableRevisionExisting,
)

DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: wait, type: wait, answer_type: integer, next: final}
  - {id: agent, type: agent, job: test, output: payload, next: wait}
"""
ACTION_DOCUMENT = b"""format_version: 1
start: agent
nodes:
  - {id: final, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: wait, type: wait, answer_type: integer, next: final}
  - {id: action, type: action, next: wait}
  - {id: agent, type: agent, job: test, output: request, next: action}
"""


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    configured = DbosRuntime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "api-tests"),
        LoopbackEffectAdapterFactory(
            tmp_path / "external.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("loopback-test"),
        ),
    )
    configured.initialize_storage()
    try:
        yield configured
    finally:
        configured.close()


def test_revision_publication_created_and_existing_are_decided_by_one_write(
    runtime: DbosRuntime,
) -> None:
    publisher = DbosWorkflowRevisionPublisher(runtime.engine)
    revision = WorkflowRevision(DOCUMENT)

    first = publisher.publish(revision)
    retry = publisher.publish(revision)

    assert first == DurableRevisionCreated(revision)
    assert retry == DurableRevisionExisting(revision)
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(workflow_revisions)
            )
            == 1
        )


def test_concurrent_publication_has_one_created_fact(runtime: DbosRuntime) -> None:
    publisher = DbosWorkflowRevisionPublisher(runtime.engine)
    revision = WorkflowRevision(DOCUMENT)
    barrier = Barrier(4)

    def publish(_: int) -> object:
        barrier.wait(timeout=5)
        return publisher.publish(revision)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(publish, range(4)))

    assert sum(isinstance(result, DurableRevisionCreated) for result in results) == 1
    assert sum(isinstance(result, DurableRevisionExisting) for result in results) == 3


def test_missing_revision_is_a_typed_in_transaction_start_result(
    runtime: DbosRuntime,
) -> None:
    starter = DbosDurableRunStarter(runtime.engine, runtime.settings)

    result = starter.start_published(
        StartPublishedRunRequest(
            RunId("missing-parent"), WorkflowRevisionHash("0" * 64)
        )
    )

    assert isinstance(result, DurableRunRevisionMissing)
    with runtime.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0


def test_concurrent_start_enqueues_only_the_transaction_that_created_the_run(
    runtime: DbosRuntime,
) -> None:
    revision = WorkflowRevision(DOCUMENT)
    assert isinstance(
        DbosWorkflowRevisionPublisher(runtime.engine).publish(revision),
        DurableRevisionCreated,
    )
    starter = DbosDurableRunStarter(runtime.engine, runtime.settings)
    request = StartPublishedRunRequest(
        RunId("concurrent/start"), revision.revision_hash
    )
    barrier = Barrier(4)

    def start(_: int) -> object:
        barrier.wait(timeout=5)
        return starter.start_published(request)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(start, range(4)))

    assert sum(isinstance(result, DurableRunCreated) for result in results) == 1
    assert sum(isinstance(result, DurableRunExisting) for result in results) == 3
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                ),
                {"workflow_id": bootstrap_workflow_id_for(request.run_id)},
            )
            == 1
        )


def test_concurrent_wait_answer_enqueues_only_the_created_answer(
    runtime: DbosRuntime,
) -> None:
    revision = WorkflowRevision(DOCUMENT)
    DbosDurableRunStarter(runtime.engine, runtime.settings).start(
        StartRunRequest(RunId("wait-run"), revision)
    )
    with canonical_write_transaction(runtime.engine) as connection:
        commit_agent_completed(
            connection, RunId("wait-run"), revision.revision_hash, "agent", b"payload"
        )
        commit_waiting_input(
            connection, RunId("wait-run"), revision.revision_hash, "wait"
        )
    answerer = DbosWaitAnswerer(runtime.engine, runtime.settings.application_version)
    request = SubmitWaitAnswerRequest(
        RunId("wait-run"), revision.revision_hash, "wait", b"17"
    )
    barrier = Barrier(4)

    def answer(_: int) -> object:
        barrier.wait(timeout=5)
        return answerer.submit_result(request)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(answer, range(4)))

    assert sum(isinstance(result, DurableAnswerCreated) for result in results) == 1
    assert sum(isinstance(result, DurableAnswerExisting) for result in results) == 3
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(wait_answers)) == 1
        )


def test_concurrent_http_retries_create_one_revision_run_answer_and_workflow(
    runtime: DbosRuntime,
) -> None:
    parallelism = 4
    publication_barrier = Barrier(parallelism)

    def publish(_: int) -> int:
        publication_barrier.wait(timeout=5)
        return (
            _client(runtime)
            .post(
                "/atelier/api/v1/workflow-revisions",
                content=DOCUMENT,
                headers={"content-type": "application/yaml"},
            )
            .status_code
        )

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        publication_statuses = list(pool.map(publish, range(parallelism)))

    revision = WorkflowRevision(DOCUMENT)
    assert publication_statuses.count(201) == 1
    assert publication_statuses.count(200) == parallelism - 1

    start_run_id = RunId("http-concurrent-start")
    start_barrier = Barrier(parallelism)

    def start(_: int) -> int:
        start_barrier.wait(timeout=5)
        return (
            _client(runtime)
            .post(
                "/atelier/api/v1/runs",
                json={
                    "run_id": start_run_id.value,
                    "workflow_revision_hash": revision.revision_hash.value,
                },
            )
            .status_code
        )

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        start_statuses = list(pool.map(start, range(parallelism)))

    assert start_statuses.count(201) == 1
    assert start_statuses.count(200) == parallelism - 1

    answer_run_id = RunId("http-concurrent-answer")
    DbosDurableRunStarter(runtime.engine, runtime.settings).start(
        StartRunRequest(answer_run_id, revision)
    )
    with canonical_write_transaction(runtime.engine) as connection:
        commit_agent_completed(
            connection,
            answer_run_id,
            revision.revision_hash,
            "agent",
            b"payload",
        )
        commit_waiting_input(connection, answer_run_id, revision.revision_hash, "wait")
    answer_path = (
        f"/atelier/api/v1/runs/{encode_public_run_reference(answer_run_id)}/answers"
    )
    answer_body = {
        "revision_hash": revision.revision_hash.value,
        "node_id": "wait",
        "answer_base64": encode_canonical_base64(b"17"),
    }
    answer_barrier = Barrier(parallelism)

    def answer(_: int) -> int:
        answer_barrier.wait(timeout=5)
        return _client(runtime).post(answer_path, json=answer_body).status_code

    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        answer_statuses = list(pool.map(answer, range(parallelism)))

    assert set(answer_statuses) <= {200, 202}
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(workflow_revisions)
            )
            == 1
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 2
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(wait_answers)) == 1
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                ),
                {"workflow_id": bootstrap_workflow_id_for(start_run_id)},
            )
            == 1
        )
        wait_execution = NodeExecutionId.for_node(
            answer_run_id, revision.revision_hash, "wait"
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                ),
                {"workflow_id": answer_workflow_id_for(wait_execution)},
            )
            == 1
        )


def _waiting_reconciliation(runtime: DbosRuntime) -> EffectIntent:
    revision = WorkflowRevision(ACTION_DOCUMENT)
    DbosDurableRunStarter(runtime.engine, runtime.settings).start(
        StartRunRequest(RunId("reconcile-run"), revision)
    )
    with canonical_write_transaction(runtime.engine) as connection:
        commit_agent_completed(
            connection,
            RunId("reconcile-run"),
            revision.revision_hash,
            "agent",
            b"request",
        )
        intent = graph_action_intent(
            connection,
            RunId("reconcile-run"),
            revision.revision_hash,
            runtime.effect_adapter_binding,
        )
    advance_run(
        intent,
        DbosDurableRunAdvancer(
            runtime.engine, runtime.settings, runtime.effect_adapter_binding
        ),
    )
    with canonical_write_transaction(runtime.engine) as connection:
        connection.execute(
            effect_intents.update().values(
                state=EffectIntentState.WAITING_RECONCILIATION.value,
                state_version=1,
            )
        )
        commit_reconciliation_required(
            connection,
            intent.binding.run_id,
            intent.binding.workflow_revision_hash,
            "action",
            intent.request.payload,
        )
    return intent


def _command(intent: EffectIntent, *, evidence: str = "inspected") -> ReconcileCommand:
    return ReconcileCommand(
        ReconcileCommandId("command"),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator"),
        evidence,
        OperatorFoundEffect(EffectId("effect"), EffectResult(b"result")),
    )


def test_reconciliation_created_and_existing_come_from_the_serialized_write(
    runtime: DbosRuntime,
) -> None:
    intent = _waiting_reconciliation(runtime)
    commander = DbosEffectReconcileCommander(runtime.engine, runtime.settings)
    submitted = _command(intent)

    first = commander.submit_result(submitted)
    retry = commander.submit_result(submitted)

    assert isinstance(first, DurableReconciliationCreated)
    assert isinstance(retry, DurableReconciliationExisting)
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(reconcile_commands)
            )
            == 1
        )
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM workflow_status WHERE workflow_uuid=:workflow_id"
                ),
                {"workflow_id": reconcile_workflow_id_for(submitted.command_id)},
            )
            == 1
        )


def _client(runtime: DbosRuntime) -> TestClient:
    queries = DbosQueries(runtime.engine)
    return TestClient(
        create_app(
            source_commit="commit-under-test",
            source_tree="tree-under-test",
            ports=ApiPorts(
                workflow_revision_publisher=DbosWorkflowRevisionPublisher(
                    runtime.engine
                ),
                published_run_starter=DbosDurableRunStarter(
                    runtime.engine, runtime.settings
                ),
                wait_answerer=DbosWaitAnswerer(
                    runtime.engine, runtime.settings.application_version
                ),
                reconcile_commander=DbosEffectReconcileCommander(
                    runtime.engine, runtime.settings
                ),
                workflow_revision_queries=queries,
                run_queries=queries,
                run_event_queries=queries,
            ),
        )
    )


def test_http_publishes_lists_starts_and_reads_exact_durable_resources(
    runtime: DbosRuntime,
) -> None:
    client = _client(runtime)

    published = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=DOCUMENT,
        headers={"content-type": "application/yaml; charset=utf-8"},
    )
    retry = client.post(
        "/atelier/api/v1/workflow-revisions",
        content=DOCUMENT,
        headers={"content-type": "application/yaml"},
    )

    assert published.status_code == 201
    assert retry.status_code == 200
    revision_hash = published.json()["revision_hash"]
    assert published.json() == retry.json()
    assert published.json()["document_base64"] == encode_canonical_base64(DOCUMENT)
    assert [node["node_id"] for node in published.json()["graph"]["nodes"]] == [
        "agent",
        "final",
        "wait",
    ]
    assert (
        client.get(f"/atelier/api/v1/workflow-revisions/{revision_hash}").json()
        == published.json()
    )
    assert client.get("/atelier/api/v1/workflow-revisions").json() == {
        "items": [{"revision_hash": revision_hash}],
        "next_after_revision_hash": None,
    }

    run_ids = ("slash/run", "nul\0run", "Grüße-東京")
    for run_id in run_ids:
        created = client.post(
            "/atelier/api/v1/runs",
            json={"run_id": run_id, "workflow_revision_hash": revision_hash},
        )
        existing = client.post(
            "/atelier/api/v1/runs",
            json={"run_id": run_id, "workflow_revision_hash": revision_hash},
        )
        assert created.status_code == 201
        assert existing.status_code == 200
        assert created.json() == existing.json()
        reference = encode_public_run_reference(RunId(run_id))
        assert created.json()["public_run_reference"] == reference
        assert client.get(f"/atelier/api/v1/runs/{reference}").json() == created.json()

    listed = client.get("/atelier/api/v1/runs?limit=100").json()["items"]
    assert [item["run_id"] for item in listed] == sorted(
        run_ids, key=lambda value: value.encode("utf-8")
    )


def test_http_wait_answer_retries_preserve_exact_bytes_and_status(
    runtime: DbosRuntime,
) -> None:
    revision = WorkflowRevision(DOCUMENT)
    run_id = RunId("answer/run")
    DbosDurableRunStarter(runtime.engine, runtime.settings).start(
        StartRunRequest(run_id, revision)
    )
    with canonical_write_transaction(runtime.engine) as connection:
        commit_agent_completed(
            connection, run_id, revision.revision_hash, "agent", b"payload"
        )
        commit_waiting_input(connection, run_id, revision.revision_hash, "wait")
    client = _client(runtime)
    path = f"/atelier/api/v1/runs/{encode_public_run_reference(run_id)}/answers"
    body = {
        "revision_hash": revision.revision_hash.value,
        "node_id": "wait",
        "answer_base64": encode_canonical_base64(b"17"),
    }

    accepted = client.post(path, json=body)
    existing = client.post(path, json=body)
    conflict = client.post(
        path,
        json={**body, "answer_base64": encode_canonical_base64(b"18")},
    )

    assert accepted.status_code == 202
    assert existing.status_code == 202
    assert accepted.json() == existing.json()
    assert accepted.json()["waiting"] == {
        "type": "WAITING_INPUT",
        "node_id": "wait",
        "answer_type": "integer",
    }
    assert conflict.status_code == 409
    assert conflict.json()["type"].endswith(":answer-bytes-conflict")


@pytest.mark.parametrize("found", [True, False])
def test_http_reconciliation_preserves_accountable_binding_without_adapter_identity(
    runtime: DbosRuntime, found: bool
) -> None:
    intent = _waiting_reconciliation(runtime)
    client = _client(runtime)
    path = (
        f"/atelier/api/v1/runs/{encode_public_run_reference(intent.binding.run_id)}"
        "/reconciliations"
    )
    determination = (
        {
            "type": "operator_found",
            "effect_id": "effect-http",
            "result_base64": encode_canonical_base64(b"result-http"),
        }
        if found
        else {"type": "operator_authoritative_absence"}
    )
    body = {
        "command_id": "http-command",
        "expected_intent_state_version": 1,
        "actor": "operator-http",
        "evidence": "inspected exact destination and request",
        "determination": determination,
    }

    accepted = client.post(path, json=body)
    existing = client.post(path, json=body)

    assert accepted.status_code == 202
    assert existing.status_code == 202
    assert accepted.json() == existing.json()
    waiting = accepted.json()["waiting"]
    assert waiting["pending_command"] == {
        "command_id": "http-command",
        "actor": "operator-http",
        "evidence": "inspected exact destination and request",
        "state": "PENDING",
        "determination": determination,
    }
    encoded = str(accepted.json())
    assert "adapter" not in encoded
    assert "workflow_id" not in encoded
    assert "sqlite" not in encoded


def test_http_reconciliation_exact_applied_retry_survives_run_advancement(
    runtime: DbosRuntime,
) -> None:
    intent = _waiting_reconciliation(runtime)
    client = _client(runtime)
    path = (
        f"/atelier/api/v1/runs/{encode_public_run_reference(intent.binding.run_id)}"
        "/reconciliations"
    )
    determination = OperatorFoundEffect(
        EffectId("effect-applied"), EffectResult(b"result-applied")
    )
    command = ReconcileCommand(
        ReconcileCommandId("applied-command"),
        intent.reference,
        EffectIntentStateVersion(1),
        ReconcileActor("operator-applied"),
        "inspected exact applied request",
        determination,
    )
    body = {
        "command_id": command.command_id.value,
        "expected_intent_state_version": 1,
        "actor": command.actor.value,
        "evidence": command.evidence,
        "determination": {
            "type": "operator_found",
            "effect_id": determination.effect_id.value,
            "result_base64": encode_canonical_base64(determination.result.payload),
        },
    }

    accepted = client.post(path, json=body)
    assert accepted.status_code == 202
    with Session(runtime.engine) as session, session.begin():
        commit_resolution(
            session,
            intent.binding.logical_key.value,
            intent.binding.workflow_revision_hash.value,
            encode_found(
                PerformedEffect(determination.effect_id, determination.result),
                ConfirmationSource.OPERATOR_FOUND,
                command.command_id,
            ),
            command.command_id,
        )

    retry = client.post(path, json=body)

    assert retry.status_code == 200
    assert retry.json()["state"] == "STARTED"
    assert retry.json()["waiting"] == {"type": "NONE"}


def test_reconciliation_retry_conflicts_are_typed_without_mutation(
    runtime: DbosRuntime,
) -> None:
    intent = _waiting_reconciliation(runtime)
    commander = DbosEffectReconcileCommander(runtime.engine, runtime.settings)
    submitted = _command(intent)
    assert isinstance(commander.submit_result(submitted), DurableReconciliationCreated)

    changed_command = commander.submit_result(_command(intent, evidence="changed"))
    changed_determination = commander.submit_result(
        ReconcileCommand(
            submitted.command_id,
            submitted.intent_reference,
            submitted.expected_intent_state_version,
            submitted.actor,
            submitted.evidence,
            OperatorFoundEffect(EffectId("different"), EffectResult(b"different")),
        )
    )

    assert isinstance(changed_command, DurableReconciliationCommandConflict)
    assert isinstance(changed_determination, DurableReconciliationDeterminationConflict)
    with runtime.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(reconcile_commands)
            )
            == 1
        )
