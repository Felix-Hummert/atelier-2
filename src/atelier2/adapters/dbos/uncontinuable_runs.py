"""Store half of serve-start run convergence: STARTED rows nothing can continue."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.node_records import keep_node_receipt
from atelier2.adapters.dbos.run_transitions import lift_started_run
from atelier2.adapters.dbos.schema import agent_attempts, runs
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow_ids import (
    node_workflow_id_for,
    replacement_workflow_id_for,
)
from atelier2.contracts.agent_attempts import (
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    STOP_AFTER_DRIVER_LOSS,
    TERMINAL_AGENT_ATTEMPT_STATES,
    AgentAttemptId,
    AgentAttemptReplacement,
    AgentAttemptState,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.node_records_v3 import PersistedReceiptDisposition
from atelier2.contracts.run_cancellations import is_operator_run_cancel
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflow_formats import WorkflowFormatVersion

_UNCONTINUABLE_ATTEMPT_STATES = (
    AgentAttemptState.FAILED,
    AgentAttemptState.INTERRUPTED,
)
"""The current-node endings that leave a STARTED run with nowhere to go, under
no command this inventory itself has to identify.

CANCELLED stays out of this tuple on purpose, and so does an INTERRUPTED
attempt carrying an operator run-cancel command: both close the run under
`RunState.CANCELLED`, not `RunState.FAILED`, and only when the command is
identifiably the operator's own (#439 Bauplan P3) -- `_attempt_family_target_state`
decides that, this tuple only ever names the plain word.
"""

# DBOS owns this table and these tokens; the store only reads them, and only
# to answer whether a node workflow it would recover is still going to run.
_dbos_workflow_status = sa.table(
    "workflow_status",
    sa.column("workflow_uuid"),
    sa.column("status"),
    sa.column("application_version"),
)
_DRIVING_WORKFLOW_STATUSES = ("PENDING", "ENQUEUED", "DELAYED")
"""The DBOS statuses under which a workflow is still owed its next step."""


class DbosUncontinuableRunStore:
    def __init__(self, engine: Engine, application_version: str) -> None:
        if not application_version.strip():
            raise ValueError("application_version must be nonempty")
        self._engine = engine
        self._application_version = application_version

    def uncontinuable_runs(self) -> tuple[RunId, ...]:
        with self._engine.connect() as connection:
            named = set(_attempt_family_run_ids(connection))
            named.update(_gap_family_run_ids(connection, self._application_version))
            return tuple(sorted(named, key=lambda run_id: run_id.value))

    def end_uncontinuable_run(self, run_id: RunId) -> bool:
        with canonical_write_transaction(self._engine) as connection:
            record = (
                connection.execute(
                    sa.select(
                        runs.c.run_id,
                        runs.c.revision_hash,
                        runs.c.current_node_id,
                        runs.c.current_round_ordinal,
                        runs.c.state,
                        runs.c.state_version,
                        runs.c.last_event_sequence,
                        runs.c.workflow_format_version,
                    ).where(runs.c.run_id == run_id.value)
                )
                .mappings()
                .one_or_none()
            )
            if record is None or str(record["state"]) != RunState.STARTED.value:
                return False
            if int(record["workflow_format_version"]) == int(WorkflowFormatVersion.V1):
                return False
            attempt_family_target = _attempt_family_target_state(connection, record)
            gap_family = _current_node_is_a_dead_gap(
                connection, record, self._application_version
            )
            if attempt_family_target is None and not gap_family:
                return False
            if gap_family:
                _name_gap_ending(connection, record)
            target_state = (
                RunState.FAILED
                if attempt_family_target is None
                else attempt_family_target
            )
            return lift_started_run(
                connection,
                run_id,
                WorkflowRevisionHash(str(record["revision_hash"])),
                int(record["state_version"]),
                int(record["last_event_sequence"]),
                target_state,
            )


def _attempt_family_run_ids(connection: Any) -> tuple[RunId, ...]:
    """List candidates only; `_attempt_family_target_state` decides precisely.

    The `CANCELLED`-with-no-replacement arm here is deliberately loose: it
    lists any such attempt, operator command or not, rather than repeat
    `is_operator_run_cancel`'s namespace check in SQL. `end_uncontinuable_run`
    reopens the exact row inside its own write transaction and answers `False`
    for a listed run that turns out not to qualify -- a wider candidate set
    costs one extra read, never a wrong ending.
    """
    terminal_attempt = tuple(state.value for state in TERMINAL_AGENT_ATTEMPT_STATES)
    uncontinuable_attempt = tuple(
        state.value for state in _UNCONTINUABLE_ATTEMPT_STATES
    )
    rows = connection.execute(
        sa.select(runs.c.run_id)
        .where(
            runs.c.state == RunState.STARTED.value,
            runs.c.workflow_format_version != int(WorkflowFormatVersion.V1),
            sa.exists(
                sa.select(1)
                .select_from(agent_attempts)
                .where(
                    agent_attempts.c.run_id == runs.c.run_id,
                    agent_attempts.c.node_id == runs.c.current_node_id,
                    sa.or_(
                        agent_attempts.c.state.in_(uncontinuable_attempt),
                        sa.and_(
                            agent_attempts.c.state == AgentAttemptState.CANCELLED.value,
                            agent_attempts.c.replacement
                            == AgentAttemptReplacement.NONE.value,
                        ),
                    ),
                )
            ),
            ~sa.exists(
                sa.select(1)
                .select_from(agent_attempts)
                .where(
                    agent_attempts.c.run_id == runs.c.run_id,
                    agent_attempts.c.state.notin_(terminal_attempt),
                )
            ),
        )
        .order_by(runs.c.run_id)
    )
    return tuple(RunId(str(run_id)) for (run_id,) in rows)


def _gap_family_run_ids(connection: Any, application_version: str) -> tuple[RunId, ...]:
    found: list[RunId] = []
    for record in connection.execute(
        sa.select(
            runs.c.run_id,
            runs.c.revision_hash,
            runs.c.current_node_id,
            runs.c.current_round_ordinal,
        ).where(*_gap_store_predicates())
    ).mappings():
        if not _gap_has_recoverable_driver(connection, record, application_version):
            found.append(RunId(str(record["run_id"])))
    return tuple(found)


def _gap_store_predicates() -> tuple[Any, ...]:
    terminal_attempt = tuple(state.value for state in TERMINAL_AGENT_ATTEMPT_STATES)
    return (
        runs.c.state == RunState.STARTED.value,
        runs.c.workflow_format_version != int(WorkflowFormatVersion.V1),
        ~sa.exists(
            sa.select(1)
            .select_from(agent_attempts)
            .where(
                agent_attempts.c.run_id == runs.c.run_id,
                agent_attempts.c.node_id == runs.c.current_node_id,
            )
        ),
        ~sa.exists(
            sa.select(1)
            .select_from(agent_attempts)
            .where(
                agent_attempts.c.run_id == runs.c.run_id,
                agent_attempts.c.state.notin_(terminal_attempt),
            )
        ),
        sa.exists(
            sa.select(1)
            .select_from(agent_attempts)
            .where(
                agent_attempts.c.run_id == runs.c.run_id,
                agent_attempts.c.state == AgentAttemptState.SUCCEEDED.value,
            )
        ),
    )


def _attempt_family_target_state(
    connection: Any, record: Mapping[Any, Any]
) -> RunState | None:
    """The word the current node's own terminal attempt earns the run, if any.

    An `INTERRUPTED` or `CANCELLED` attempt under an operator run-cancel
    command with no replacement in flight lifts the run to `CANCELLED` --
    the same command-identity gate an attempt store's own in-transaction
    cancel lift uses (#439 Bauplan P3), not the disposition that happened to
    end the process. Every other `FAILED`/`INTERRUPTED` ending in
    `_UNCONTINUABLE_ATTEMPT_STATES` still lifts to `FAILED`, unchanged. A
    `CANCELLED` attempt under any other command names a replacement that may
    still be in flight or a cancel this inventory does not own, and answers
    `None`.
    """
    row = (
        connection.execute(
            sa.select(
                agent_attempts.c.state,
                agent_attempts.c.cancellation_command_id,
                agent_attempts.c.replacement,
            )
            .where(
                agent_attempts.c.run_id == str(record["run_id"]),
                agent_attempts.c.node_id == str(record["current_node_id"]),
                sa.or_(
                    agent_attempts.c.state.in_(
                        tuple(state.value for state in _UNCONTINUABLE_ATTEMPT_STATES)
                    ),
                    agent_attempts.c.state == AgentAttemptState.CANCELLED.value,
                ),
            )
            .limit(1)
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    state = AgentAttemptState(str(row["state"]))
    command_id = row["cancellation_command_id"]
    is_operator_command = (
        state in {AgentAttemptState.CANCELLED, AgentAttemptState.INTERRUPTED}
        and command_id is not None
        and is_operator_run_cancel(str(command_id))
        and str(row["replacement"]) == AgentAttemptReplacement.NONE.value
    )
    if is_operator_command:
        return RunState.CANCELLED
    if state in _UNCONTINUABLE_ATTEMPT_STATES:
        return RunState.FAILED
    return None


def _current_node_is_a_dead_gap(
    connection: Any, record: Mapping[Any, Any], application_version: str
) -> bool:
    still_a_gap = connection.scalar(
        sa.select(1)
        .where(*_gap_store_predicates())
        .where(runs.c.run_id == str(record["run_id"]))
    )
    if still_a_gap is None:
        return False
    return not _gap_has_recoverable_driver(connection, record, application_version)


def _gap_has_recoverable_driver(
    connection: Any, record: Mapping[Any, Any], application_version: str
) -> bool:
    workflow_ids = _gap_workflow_ids(connection, record)
    found = connection.scalar(
        sa.select(_dbos_workflow_status.c.workflow_uuid)
        .where(
            _dbos_workflow_status.c.workflow_uuid.in_(workflow_ids),
            _dbos_workflow_status.c.status.in_(_DRIVING_WORKFLOW_STATUSES),
            _dbos_workflow_status.c.application_version == application_version,
        )
        .limit(1)
    )
    return found is not None


def _gap_workflow_ids(connection: Any, record: Mapping[Any, Any]) -> tuple[str, ...]:
    run_id = RunId(str(record["run_id"]))
    revision_hash = WorkflowRevisionHash(str(record["revision_hash"]))
    named = {
        node_workflow_id_for(
            NodeExecutionId.for_node(
                run_id,
                revision_hash,
                str(record["current_node_id"]),
                int(record["current_round_ordinal"]),
            )
        )
    }
    for attempt in connection.execute(
        sa.select(
            agent_attempts.c.attempt_id,
            agent_attempts.c.node_execution_id,
            agent_attempts.c.attempt_ordinal,
        ).where(
            agent_attempts.c.run_id == run_id.value,
            agent_attempts.c.state == AgentAttemptState.SUCCEEDED.value,
        )
    ):
        if int(attempt.attempt_ordinal) == REPLACEMENT_AGENT_ATTEMPT_ORDINAL:
            named.add(
                replacement_workflow_id_for(AgentAttemptId(str(attempt.attempt_id)))
            )
        else:
            named.add(
                node_workflow_id_for(NodeExecutionId(str(attempt.node_execution_id)))
            )
    return tuple(named)


def _name_gap_ending(connection: Any, record: Mapping[Any, Any]) -> None:
    """Write the failed receipt when the current node has a durable request.

    keep_node_receipt stays honestly receipt-less when no request was
    written — that is not a refusal of the lift. Hashes are not invented.
    """

    keep_node_receipt(
        connection,
        NodeExecutionId.for_node(
            RunId(str(record["run_id"])),
            WorkflowRevisionHash(str(record["revision_hash"])),
            str(record["current_node_id"]),
            int(record["current_round_ordinal"]),
        ),
        PersistedReceiptDisposition.FAILED,
        STOP_AFTER_DRIVER_LOSS,
    )
