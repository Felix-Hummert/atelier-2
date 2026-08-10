from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from dbos import DBOS, SQLAlchemyDatasource

from atelier2.adapters.dbos.effect_store import (
    EncodedEffectResolution,
    commit_resolution,
    observe_adapter,
    observe_reconcile_command,
    resolve_observation,
)
from atelier2.adapters.dbos.schema import runs, workflow_revisions
from atelier2.contracts.effects import ReconcileCommandId
from atelier2.contracts.runs import (
    RevisionHashCollision,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.ports.effects import EffectAdapter

WORKFLOW_NAME = "atelier2_durable_run"
EFFECT_WORKFLOW_NAME = "atelier2_effect"
RECONCILE_WORKFLOW_NAME = "atelier2_reconcile_effect"
QUEUE_NAME = "atelier2-durable-runs"

BOOTSTRAP_STEP_NAME = "bootstrap-run-binding"
OBSERVE_STEP_NAME = "observe/0"
RESOLVE_STEP_NAME = "resolve/1"
COMMIT_STEP_NAME = "commit/2"


class RunBindingConflict(RuntimeError):
    pass


def bootstrap_run_binding(
    datasource: SQLAlchemyDatasource,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
) -> RunState:
    def load_binding() -> str:
        record = (
            datasource.sql_session()
            .execute(
                sa.select(
                    runs.c.revision_hash, runs.c.state, workflow_revisions.c.document
                )
                .join(
                    workflow_revisions,
                    workflow_revisions.c.revision_hash == runs.c.revision_hash,
                )
                .where(runs.c.run_id == run_id.value)
            )
            .one_or_none()
        )
        if record is None or str(record.revision_hash) != revision_hash.value:
            raise RunBindingConflict("bootstrap requires its exact durable run binding")
        revision = WorkflowRevision(bytes(record.document))
        if revision.revision_hash != revision_hash:
            raise RevisionHashCollision(
                "durable workflow revision bytes disagree with their run binding"
            )
        return RunState(str(record.state)).value

    return RunState(datasource.run_tx_step({"name": BOOTSTRAP_STEP_NAME}, load_binding))


def _run_effect_step(
    datasource: SQLAlchemyDatasource,
    name: str,
    operation: Any,
    *arguments: Any,
) -> EncodedEffectResolution:
    def execute() -> EncodedEffectResolution:
        return operation(datasource.sql_session(), *arguments)

    return datasource.run_tx_step({"name": name}, execute)


def register_durable_run_workflow(
    datasource: SQLAlchemyDatasource, adapter: EffectAdapter
) -> None:
    @DBOS.workflow(name=WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_run(run_id: str, revision_hash: str) -> str:
        return bootstrap_run_binding(
            datasource, RunId(run_id), WorkflowRevisionHash(revision_hash)
        ).value

    @DBOS.workflow(name=EFFECT_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_effect(logical_key: str, revision_hash: str) -> str:
        observed = _run_effect_step(
            datasource,
            OBSERVE_STEP_NAME,
            observe_adapter,
            adapter,
            logical_key,
            revision_hash,
        )
        resolved = _run_effect_step(
            datasource,
            RESOLVE_STEP_NAME,
            resolve_observation,
            adapter,
            logical_key,
            revision_hash,
            observed,
        )
        return datasource.run_tx_step(
            {"name": COMMIT_STEP_NAME},
            lambda: (
                commit_resolution(
                    datasource.sql_session(), logical_key, revision_hash, resolved
                ).value
            ),
        )

    @DBOS.workflow(name=RECONCILE_WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_reconciliation(command_id: str, revision_hash: str) -> str:
        observed = _run_effect_step(
            datasource,
            OBSERVE_STEP_NAME,
            observe_reconcile_command,
            adapter,
            command_id,
            revision_hash,
        )
        command = ReconcileCommandId(command_id)
        logical_key = str(observed["logical_key"])
        resolved = _run_effect_step(
            datasource,
            RESOLVE_STEP_NAME,
            resolve_observation,
            adapter,
            logical_key,
            revision_hash,
            observed,
            command if observed.get("operator_authorized") == command_id else None,
        )
        return datasource.run_tx_step(
            {"name": COMMIT_STEP_NAME},
            lambda: (
                commit_resolution(
                    datasource.sql_session(),
                    logical_key,
                    revision_hash,
                    resolved,
                    command,
                ).value
            ),
        )
