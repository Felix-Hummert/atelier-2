from __future__ import annotations

from typing import Any

from dbos import DBOS, SetWorkflowID, SQLAlchemyDatasource

from atelier2.adapters.dbos.names import ACTION_CHECKPOINT_STEP_NAME
from atelier2.adapters.dbos.run_store import commit_confirmed_effect
from atelier2.adapters.dbos.workflow_ids import action_continuation_workflow_id_for
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL, RunState, WorkflowRevisionHash


def decode_action_checkpoint(recorded: object) -> tuple[str, str, int, str]:
    """Read a recorded action-checkpoint/0 output by arity.

    DBOS replays this step by ordinal, so a V1 2-tuple stays a 2-tuple after
    the live step started returning more values. V1/V2 Action cannot be a
    sink: two values are `(run_id, successor)` and mean STARTED. The prior
    three-value form lacks a round, so it remains round one. The current form is
    `(run_id, head, round_ordinal, state)`, preserving a looped effect grant's
    exact continuation. Any other form is refused rather than indexed.
    """
    if not isinstance(recorded, tuple):
        raise TypeError("a recorded action checkpoint is not a tuple")
    match recorded:
        case (run_id, successor):
            return (
                str(run_id),
                str(successor),
                FIRST_ROUND_ORDINAL,
                RunState.STARTED.value,
            )
        case (run_id, head, state):
            return str(run_id), str(head), FIRST_ROUND_ORDINAL, str(state)
        case (run_id, head, round_ordinal, state):
            try:
                round_value = int(round_ordinal)
            except (TypeError, ValueError) as error:
                raise ValueError(
                    "a recorded action checkpoint has no integer round"
                ) from error
            return str(run_id), str(head), round_value, str(state)
        case _:
            raise ValueError(
                "a recorded action checkpoint has no arity this adapter writes"
            )


def checkpoint_confirmed_effect(
    datasource: SQLAlchemyDatasource,
    logical_key: LogicalEffectKey,
    revision_hash: WorkflowRevisionHash,
) -> tuple[str, str, int, str]:
    def checkpoint() -> tuple[str, str, int, str]:
        snapshot = commit_confirmed_effect(
            datasource.sql_session(), logical_key, revision_hash
        )
        return (
            snapshot.run_id.value,
            snapshot.current_node_id,
            snapshot.current_round_ordinal,
            snapshot.state.value,
        )

    return decode_action_checkpoint(
        datasource.run_tx_step(
            {"name": ACTION_CHECKPOINT_STEP_NAME},
            checkpoint,
        )
    )


def schedule_confirmed_effect_continuation(
    workflow: Any, logical_key: LogicalEffectKey, revision_hash: WorkflowRevisionHash
) -> None:
    with SetWorkflowID(action_continuation_workflow_id_for(logical_key)):
        DBOS.start_workflow(workflow, logical_key.value, revision_hash.value)
