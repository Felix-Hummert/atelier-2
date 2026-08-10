from __future__ import annotations

import sqlalchemy as sa
from dbos import DBOS, SQLAlchemyDatasource

from atelier2.adapters.dbos.schema import runs
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash

WORKFLOW_NAME = "atelier2_durable_run"
QUEUE_NAME = "atelier2-durable-runs"
TRANSITION_STEP_NAME = "complete-run"


class RunTransitionConflict(RuntimeError):
    pass


def transition_started_run(
    datasource: SQLAlchemyDatasource,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
) -> RunState:
    def transition() -> str:
        updated_run_id = (
            datasource.sql_session()
            .execute(
                sa.update(runs)
                .where(
                    runs.c.run_id == run_id.value,
                    runs.c.revision_hash == revision_hash.value,
                    runs.c.state == RunState.STARTED.value,
                )
                .values(state=RunState.COMPLETED.value)
                .returning(runs.c.run_id)
            )
            .scalar_one_or_none()
        )
        if updated_run_id != run_id.value:
            raise RunTransitionConflict(
                "durable transition requires exactly one STARTED run binding"
            )
        return RunState.COMPLETED.value

    state = datasource.run_tx_step({"name": TRANSITION_STEP_NAME}, transition)
    return RunState(state)


def register_durable_run_workflow(datasource: SQLAlchemyDatasource) -> None:
    @DBOS.workflow(name=WORKFLOW_NAME, max_recovery_attempts=None)
    def durable_run(run_id: str, revision_hash: str) -> str:
        return transition_started_run(
            datasource,
            RunId(run_id),
            WorkflowRevisionHash(revision_hash),
        ).value
