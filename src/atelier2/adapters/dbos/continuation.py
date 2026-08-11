from __future__ import annotations

from typing import Any

from dbos import DBOS, SetWorkflowID, SQLAlchemyDatasource

from atelier2.adapters.dbos.run_store import commit_action_completed
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import WorkflowRevisionHash

ACTION_CONTINUATION_WORKFLOW_NAME = "atelier2_action_continuation"
ACTION_CHECKPOINT_STEP_NAME = "action-checkpoint/0"


def action_continuation_workflow_id_for(logical_key: LogicalEffectKey) -> str:
    digest = Sha256Hash.of(
        frame(
            "action-continuation-workflow-id/v1",
            logical_key.value.encode("utf-8"),
        )
    )
    return f"atelier2-action-continuation-{digest.value}"


def checkpoint_confirmed_action(
    datasource: SQLAlchemyDatasource,
    logical_key: LogicalEffectKey,
    revision_hash: WorkflowRevisionHash,
) -> tuple[str, str]:
    def checkpoint() -> tuple[str, str]:
        snapshot = commit_action_completed(
            datasource.sql_session(), logical_key, revision_hash
        )
        return snapshot.run_id.value, snapshot.current_node_id

    result = datasource.run_tx_step(
        {"name": ACTION_CHECKPOINT_STEP_NAME},
        checkpoint,
    )
    return str(result[0]), str(result[1])


def schedule_confirmed_action_continuation(
    workflow: Any, logical_key: LogicalEffectKey, revision_hash: WorkflowRevisionHash
) -> None:
    with SetWorkflowID(action_continuation_workflow_id_for(logical_key)):
        DBOS.start_workflow(workflow, logical_key.value, revision_hash.value)
