"""Store half of serve-start run convergence: STARTED rows nothing can continue.

This module also owns `live_driver_workflow_ids`, the one answer to "is this
DBOS workflow a live driver" that both this store's own gap sweep (#645) and
`effect_store.py`'s driverless-effect-intent sweep (#646, #707) read: a
workflow PENDING, ENQUEUED, or DELAYED under a retired `application_version`
is not live either way, because DBOS scopes recovery to the version that
enqueued it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from atelier2.adapters.dbos.node_records import keep_node_receipt
from atelier2.adapters.dbos.run_transitions import lift_started_run
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    effect_intents,
    reconcile_commands,
    runs,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow_ids import (
    action_continuation_workflow_id_for,
    effect_workflow_id_for,
    node_workflow_id_for,
    reconcile_workflow_id_for,
    replacement_workflow_id_for,
    runner_lease_workflow_id_for,
)
from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    STOP_AFTER_DRIVER_LOSS,
    TERMINAL_AGENT_ATTEMPT_STATES,
    AgentAttemptId,
    AgentAttemptReplacement,
    AgentAttemptState,
)
from atelier2.contracts.effects import LogicalEffectKey, ReconcileCommandId
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

# DBOS owns this table and these tokens; `live_driver_workflow_ids` only reads
# them, to answer whether a workflow is still one DBOS itself owes a next
# step. `atelier2.adapters.dbos`'s other readers of `workflow_status` each
# keep their own narrower copy for a question this one does not answer --
# whether a workflow *raised*, not whether it is still live.
_dbos_workflow_status = sa.table(
    "workflow_status",
    sa.column("workflow_uuid"),
    sa.column("status"),
    sa.column("application_version"),
)
LIVE_DRIVER_WORKFLOW_STATUSES = ("PENDING", "ENQUEUED", "DELAYED")
"""The DBOS statuses under which a workflow is still owed its next step."""


def live_driver_workflow_ids(
    connection: Connection,
    workflow_ids: Iterable[str],
    application_version: str,
) -> frozenset[str]:
    """The ids among `workflow_ids` that DBOS itself still owes a next step.

    A row absent from `workflow_status` is not answered here at all: that
    workflow was never durably started, and whether that still counts as
    "owed" is a fact about the caller's own domain, not about DBOS recovery.
    """

    ids = tuple(workflow_ids)
    if not ids:
        return frozenset()
    return frozenset(
        connection.scalars(
            sa.select(_dbos_workflow_status.c.workflow_uuid).where(
                _dbos_workflow_status.c.workflow_uuid.in_(ids),
                _dbos_workflow_status.c.status.in_(LIVE_DRIVER_WORKFLOW_STATUSES),
                _dbos_workflow_status.c.application_version == application_version,
            )
        )
    )


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

    Ordered by `attempt_ordinal` descending, not merely limited to one row:
    a superseded ordinal-1 (`replacement=ONE` under a foreign or legacy
    command) can sit beside a live ordinal-2 on the exact same node, and an
    unordered pick could read the superseded row's foreign command instead of
    the live one's operator command -- the same current-attempt-by-ordinal
    read `request_run_cancellation`'s own current-record lookup already uses
    in `agent_attempt_store.py`.
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
            .order_by(agent_attempts.c.attempt_ordinal.desc())
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
    return bool(live_driver_workflow_ids(connection, workflow_ids, application_version))


def _gap_workflow_ids(connection: Any, record: Mapping[Any, Any]) -> tuple[str, ...]:
    """Every workflow that could still owe this apparent gap its next move.

    A gap is dead only once nothing is going to move the run, so every driver
    the run can have belongs here, and three families can. The node and
    replacement workflows of its own nodes are one. Its effect workflows are
    another (#645): an Action node prepares its effect and returns, so the
    node workflow that would name it is already SUCCESS while the effect is
    still in flight. Leaving that family out reads a healthy V3 run standing
    on an Action node as a dead gap and ends it FAILED while its effect is
    still going to be performed. The Runner-lease slot is the third, for the
    same reason (#636): a lease-carried Agent node hands its Attempt to the
    slot's queue and returns, so its node workflow reads SUCCESS while the
    Attempt is still waiting its turn or running.

    Naming a workflow that turns out not to owe anything only makes this sweep
    wait for a status read to say so, so a family is named whenever it *can*
    carry the run, never only when it does.
    """

    run_id = RunId(str(record["run_id"]))
    revision_hash = WorkflowRevisionHash(str(record["revision_hash"]))
    current_execution = NodeExecutionId.for_node(
        run_id,
        revision_hash,
        str(record["current_node_id"]),
        int(record["current_round_ordinal"]),
    )
    named = {node_workflow_id_for(current_execution)}
    named.update(_runner_lease_workflow_ids(current_execution))
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
        execution_id = NodeExecutionId(str(attempt.node_execution_id))
        if int(attempt.attempt_ordinal) == REPLACEMENT_AGENT_ATTEMPT_ORDINAL:
            named.add(
                replacement_workflow_id_for(AgentAttemptId(str(attempt.attempt_id)))
            )
        else:
            named.add(node_workflow_id_for(execution_id))
        named.update(_runner_lease_workflow_ids(execution_id))
    named.update(_effect_workflow_ids(connection, run_id))
    return tuple(named)


def _runner_lease_workflow_ids(execution_id: NodeExecutionId) -> tuple[str, ...]:
    """Both turns one node execution can take in the Runner-lease slot.

    Whether a node is lease-carried is not written on the run, so both its first
    Attempt's and its replacement's slot workflows are named and the status read
    decides -- an id no workflow was ever minted under simply matches nothing.
    """

    return tuple(
        runner_lease_workflow_id_for(execution_id, ordinal)
        for ordinal in (AGENT_ATTEMPT_ORDINAL, REPLACEMENT_AGENT_ATTEMPT_ORDINAL)
    )


def _effect_workflow_ids(connection: Any, run_id: RunId) -> tuple[str, ...]:
    """Every durable workflow an effect of this run can still be moved by.

    Three workflows carry one effect: `durable_effect` resolves it,
    `durable_reconciliation` resolves it under an operator command instead, and
    the action continuation carries the confirmed run onto its next node. Which
    of them is currently owed the step is not decided here, because the intent
    state does not say: an intent is CONFIRMED for the rest of the step in
    which the workflow that confirmed it schedules that continuation. Naming
    all three and letting the liveness read decide is exact -- a workflow
    stands in a driving status only while DBOS still owes it a step, and every
    step any of the three is owed moves this run.
    """

    logical_keys = tuple(
        LogicalEffectKey(str(value))
        for value in connection.scalars(
            sa.select(effect_intents.c.logical_key).where(
                effect_intents.c.run_id == run_id.value
            )
        )
    )
    if not logical_keys:
        return ()
    command_ids = connection.scalars(
        sa.select(reconcile_commands.c.command_id).where(
            reconcile_commands.c.logical_key.in_(
                logical_key.value for logical_key in logical_keys
            )
        )
    )
    return (
        *(effect_workflow_id_for(logical_key) for logical_key in logical_keys),
        *(
            action_continuation_workflow_id_for(logical_key)
            for logical_key in logical_keys
        ),
        *(
            reconcile_workflow_id_for(ReconcileCommandId(str(command_id)))
            for command_id in command_ids
        ),
    )


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
