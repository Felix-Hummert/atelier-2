from __future__ import annotations

from typing import Any

from dbos import DBOS, SetWorkflowID, SQLAlchemyDatasource

from atelier2.adapters.dbos.names import ACTION_CHECKPOINT_STEP_NAME
from atelier2.adapters.dbos.run_store import commit_action_completed
from atelier2.adapters.dbos.workflow_ids import action_continuation_workflow_id_for
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.runs import RunState, WorkflowRevisionHash


def decode_action_checkpoint(recorded: object) -> tuple[str, str, str]:
    """Read a recorded action-checkpoint/0 output by arity.

    DBOS replays this step by ordinal, so a V1 2-tuple stays a 2-tuple after
    the live step started returning three values. V1/V2 Action cannot be a
    sink: two values are `(run_id, successor)` and mean STARTED. Three values
    are `(run_id, head, state)`. Any other form is refused rather than indexed.
    """
    if not isinstance(recorded, tuple):
        raise TypeError("a recorded action checkpoint is not a tuple")
    match recorded:
        case (run_id, successor):
            return str(run_id), str(successor), RunState.STARTED.value
        case (run_id, head, state):
            return str(run_id), str(head), str(state)
        case _:
            raise ValueError(
                "a recorded action checkpoint has no arity this adapter writes"
            )


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

    return decode_action_checkpoint(
        datasource.run_tx_step(
            {"name": ACTION_CHECKPOINT_STEP_NAME},
            checkpoint,
        )
    )


def schedule_confirmed_action_continuation(
    workflow: Any, logical_key: LogicalEffectKey, revision_hash: WorkflowRevisionHash
) -> None:
    with SetWorkflowID(action_continuation_workflow_id_for(logical_key)):
        DBOS.start_workflow(workflow, logical_key.value, revision_hash.value)
