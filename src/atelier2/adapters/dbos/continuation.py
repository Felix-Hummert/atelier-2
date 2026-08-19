from __future__ import annotations

from typing import Any

from dbos import DBOS, SetWorkflowID, SQLAlchemyDatasource

from atelier2.adapters.dbos.names import ACTION_CHECKPOINT_STEP_NAME
from atelier2.adapters.dbos.run_store import commit_action_completed
from atelier2.adapters.dbos.workflow_ids import action_continuation_workflow_id_for
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.runs import WorkflowRevisionHash


def checkpoint_confirmed_action(
    datasource: SQLAlchemyDatasource,
    logical_key: LogicalEffectKey,
    revision_hash: WorkflowRevisionHash,
) -> tuple[str, str, str]:
    def checkpoint() -> tuple[str, str, str]:
        snapshot = commit_action_completed(
            datasource.sql_session(), logical_key, revision_hash
        )
        return snapshot.run_id.value, snapshot.current_node_id, snapshot.state.value

    result = datasource.run_tx_step(
        {"name": ACTION_CHECKPOINT_STEP_NAME},
        checkpoint,
    )
    return str(result[0]), str(result[1]), str(result[2])


def schedule_confirmed_action_continuation(
    workflow: Any, logical_key: LogicalEffectKey, revision_hash: WorkflowRevisionHash
) -> None:
    with SetWorkflowID(action_continuation_workflow_id_for(logical_key)):
        DBOS.start_workflow(workflow, logical_key.value, revision_hash.value)
