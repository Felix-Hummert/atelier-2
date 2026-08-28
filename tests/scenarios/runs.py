"""Drivers that reach durable runs through the entry points production uses.

The API publishes a workflow revision and then starts a published run; the run
workflow prepares a graph action and starts the effect workflow from inside its
own workflow context. A test cannot enter that context, so the effect launch is
enqueued through the DBOS client here instead. Everything else below calls the
same use cases and adapter methods the served application calls.
"""

from __future__ import annotations

from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.advancer import prepare_graph_action as _prepare
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.names import EFFECT_WORKFLOW_NAME, QUEUE_NAME
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntimeSettings
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow_ids import effect_workflow_id_for
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.answer_wait import (
    AnswerAcceptedPending,
    AnswerExistingPending,
    answer_wait_result,
)
from atelier2.application.publish_workflow_revision import (
    PublicationCreated,
    PublicationExisting,
    publish_workflow_revision,
)
from atelier2.application.reconcile_effect import (
    ReconciliationAcceptedPending,
    ReconciliationExistingApplied,
    ReconciliationExistingPending,
    ReconciliationExistingRejected,
    reconcile_effect_result,
)
from atelier2.application.start_published_run import (
    RunCreated,
    RunExisting,
    start_published_run,
)
from atelier2.contracts.effects import (
    EffectAdapterBinding,
    EffectIntent,
    EffectIntentSnapshot,
    ReconcileCommand,
    ReconcileCommandSnapshot,
)
from atelier2.contracts.executions import SubmitWaitAnswerRequest, WaitAnswerSnapshot
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, WorkflowRevision, WorkflowRevisionHash
from atelier2.ports.agent_executions import AgentExecutorRegistry
from tests.scenarios.api import permissive_projection_limit

NO_AGENT_EXECUTORS = AgentExecutorRegistry()
"""What a V1 run binds: no executor at all, said rather than defaulted into."""


def publish_revision(engine: Engine, revision: WorkflowRevision) -> None:
    result = publish_workflow_revision(
        revision.document,
        DbosWorkflowRevisionPublisher(engine),
        parse_workflow_document,
        permissive_projection_limit(),
        DbosCatalogStore(engine),
    )
    assert isinstance(result, (PublicationCreated, PublicationExisting)), result


def start_published_v1_run(
    engine: Engine,
    settings: DbosRuntimeSettings,
    run_id: RunId,
    revision: WorkflowRevision,
    agent_executor_registry: AgentExecutorRegistry = NO_AGENT_EXECUTORS,
) -> AnyRun:
    publish_revision(engine, revision)
    result = start_published_run(
        run_id,
        revision.revision_hash,
        None,
        DbosDurableRunStarter(
            engine,
            settings,
            agent_executor_registry,
        ),
    )
    assert isinstance(result, (RunCreated, RunExisting)), result
    return result.run


def prepare_graph_action(
    engine: Engine,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    effect_adapter_binding: EffectAdapterBinding,
) -> EffectIntentSnapshot:
    """Run the run workflow's action-preparation transaction step alone."""
    with canonical_write_transaction(engine) as connection:
        return _prepare(connection, run_id, revision_hash, effect_adapter_binding)


def launch_effect_workflow(
    engine: Engine, settings: DbosRuntimeSettings, intent: EffectIntent
) -> None:
    """Start the effect workflow the run workflow starts after preparing."""
    client = DBOSClient(system_database_engine=engine, use_listen_notify=False)
    try:
        options: EnqueueOptions = {
            "workflow_name": EFFECT_WORKFLOW_NAME,
            "queue_name": QUEUE_NAME,
            "workflow_id": effect_workflow_id_for(intent.binding.logical_key),
            "app_version": settings.application_version,
        }
        client.enqueue(
            options,
            intent.binding.logical_key.value,
            intent.binding.workflow_revision_hash.value,
        )
    finally:
        client.destroy()


def prepare_and_launch_graph_action(
    engine: Engine,
    settings: DbosRuntimeSettings,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    effect_adapter_binding: EffectAdapterBinding,
) -> EffectIntent:
    snapshot = prepare_graph_action(
        engine, run_id, revision_hash, effect_adapter_binding
    )
    launch_effect_workflow(engine, settings, snapshot.intent)
    return snapshot.intent


def submit_wait_answer(
    engine: Engine, application_version: str, request: SubmitWaitAnswerRequest
) -> WaitAnswerSnapshot:
    result = answer_wait_result(
        request.run_id,
        request.revision_hash,
        request.node_id,
        request.expected_node_execution_id,
        request.actor,
        request.answer_bytes,
        DbosWaitAnswerer(engine, application_version),
    )
    assert isinstance(result, (AnswerAcceptedPending, AnswerExistingPending)), result
    return result.snapshot


def submit_reconcile_command(
    engine: Engine, settings: DbosRuntimeSettings, command: ReconcileCommand
) -> ReconcileCommandSnapshot:
    result = reconcile_effect_result(
        command, DbosEffectReconcileCommander(engine, settings)
    )
    assert isinstance(
        result,
        (
            ReconciliationAcceptedPending,
            ReconciliationExistingPending,
            ReconciliationExistingApplied,
            ReconciliationExistingRejected,
        ),
    ), result
    return result.snapshot
