"""Store half of serve-start run convergence: STARTED rows nothing can continue.

This module also owns `live_driver_workflow_ids`, the one answer to "is this
DBOS workflow a live driver" that both this store's own gap sweep (#645) and
`effect_store.py`'s driverless-effect-intent sweep (#646, #707) read: a
workflow PENDING, ENQUEUED, or DELAYED under a retired `application_version`
is not live either way, because DBOS scopes recovery to the version that
enqueued it.
"""

from __future__ import annotations

import logging
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
    wait_answers,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.adapters.dbos.workflow_ids import (
    action_continuation_workflow_id_for,
    answer_workflow_id_for,
    effect_workflow_id_for,
    node_workflow_id_for,
    reconcile_workflow_id_for,
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
from atelier2.contracts.effects import (
    EFFECT_INTENT_VERSION_RECONCILING,
    EFFECT_INTENT_VERSION_WAITING,
    EffectIntentState,
    LogicalEffectKey,
    ReconcileCommandId,
    ReconcileCommandState,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    WaitAnswerState,
    logical_effect_key_for_node,
)
from atelier2.contracts.node_records_v3 import PersistedReceiptDisposition
from atelier2.contracts.run_cancellations import is_operator_run_cancel
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflow_formats import WorkflowFormatVersion

_LOG = logging.getLogger("atelier2")

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
# whether a workflow *raised*, not whether it is still live. DBOS takes its
# recovery snapshot at launch, and `_insert_workflow_status` does not update an
# existing version; therefore this DBOS-owned table permits only the narrow,
# conditional version update in `retag_stranded_continuations` below.
_dbos_workflow_status = sa.table(
    "workflow_status",
    sa.column("workflow_uuid"),
    sa.column("status"),
    sa.column("application_version"),
)
LIVE_DRIVER_WORKFLOW_STATUSES = ("PENDING", "ENQUEUED", "DELAYED")
"""The DBOS statuses under which a workflow is still owed its next step."""
_RETAGGABLE_WORKFLOW_STATUSES = ("PENDING", "ENQUEUED")
"""The stranded continuation statuses DBOS can recover after their retag."""


def retag_stranded_continuations(engine: Engine, application_version: str) -> None:
    """Move safely identified parked continuations to this launch's version.

    The scans only make a finite candidate list. Every predicate that grants
    ownership is repeated under that candidate's canonical write transaction,
    where the version compare-and-swap is the sole write to DBOS state.

    Whether the retiring version's own process is still live is not observable
    here: DBOS keeps its active workflow set in that process's memory, and
    `workflow_status` names only a version, never a process. Liveness of the
    retiring version is therefore a precondition this sweep depends on rather
    than checks: `scripts/serve_live_update.sh` stops `atelier2-serve.service`
    synchronously, and only then runs the launch that calls here, before
    starting the new one -- the invariant `docs/decisions/0001-durable-runtime.md`
    names.
    """

    if not sa.inspect(engine).has_table("workflow_status"):
        return
    with engine.connect() as connection:
        answer_candidates = _stranded_answer_workflow_ids(
            connection, application_version
        )
        reconciliation_candidates = _stranded_reconciliation_workflow_ids(
            connection, application_version
        )
    for workflow_id in answer_candidates:
        with canonical_write_transaction(engine) as connection:
            retagged_from = _retag_stranded_answer(
                connection, workflow_id, application_version
            )
        _log_retagged_continuation(
            "answer", workflow_id, retagged_from, application_version
        )
    for workflow_id, command_id in reconciliation_candidates:
        with canonical_write_transaction(engine) as connection:
            retagged_from = _retag_stranded_reconciliation(
                connection, workflow_id, command_id, application_version
            )
        _log_retagged_continuation(
            "reconciliation", workflow_id, retagged_from, application_version
        )


def _log_retagged_continuation(
    family: str,
    workflow_id: str,
    retagged_from: str | None,
    application_version: str,
) -> None:
    if retagged_from is None:
        return
    _LOG.info(
        "Retagged stranded %s continuation %s from application version %s to %s.",
        family,
        workflow_id,
        retagged_from,
        application_version,
        extra={
            "event": "stranded_continuation_retagged",
            "family": family,
            "workflow_id": workflow_id,
            "old_application_version": retagged_from,
            "new_application_version": application_version,
        },
    )


def _stranded_answer_workflow_ids(
    connection: Connection, application_version: str
) -> tuple[str, ...]:
    rows = connection.execute(
        sa.select(wait_answers.c.answer_workflow_id)
        .select_from(
            wait_answers.join(runs, wait_answers.c.run_id == runs.c.run_id).join(
                _dbos_workflow_status,
                _dbos_workflow_status.c.workflow_uuid
                == wait_answers.c.answer_workflow_id,
            )
        )
        .where(
            runs.c.state == RunState.WAITING_INPUT.value,
            wait_answers.c.state == WaitAnswerState.PENDING.value,
            wait_answers.c.state_version == 0,
            _dbos_workflow_status.c.application_version != application_version,
            _dbos_workflow_status.c.status.in_(_RETAGGABLE_WORKFLOW_STATUSES),
        )
    )
    return tuple(str(workflow_id) for (workflow_id,) in rows)


def _stranded_reconciliation_workflow_ids(
    connection: Connection, application_version: str
) -> tuple[tuple[str, ReconcileCommandId], ...]:
    rows = connection.execute(
        sa.select(reconcile_commands.c.command_id)
        .select_from(
            reconcile_commands.join(
                effect_intents,
                reconcile_commands.c.logical_key == effect_intents.c.logical_key,
            ).join(runs, effect_intents.c.run_id == runs.c.run_id)
        )
        .where(
            runs.c.state == RunState.WAITING_RECONCILIATION.value,
            effect_intents.c.state == EffectIntentState.RECONCILING.value,
            reconcile_commands.c.state == ReconcileCommandState.PENDING.value,
        )
    )
    candidates: list[tuple[str, ReconcileCommandId]] = []
    for (command_id,) in rows:
        typed_command_id = ReconcileCommandId(str(command_id))
        workflow_id = reconcile_workflow_id_for(typed_command_id)
        status = connection.execute(
            sa.select(
                _dbos_workflow_status.c.application_version,
                _dbos_workflow_status.c.status,
            ).where(_dbos_workflow_status.c.workflow_uuid == workflow_id)
        ).one_or_none()
        if (
            status is not None
            and str(status.application_version) != application_version
            and str(status.status) in _RETAGGABLE_WORKFLOW_STATUSES
        ):
            candidates.append((workflow_id, typed_command_id))
    return tuple(candidates)


def _retag_stranded_answer(
    connection: Connection, workflow_id: str, application_version: str
) -> str | None:
    record = (
        connection.execute(
            sa.select(
                runs.c.run_id,
                runs.c.revision_hash,
                runs.c.current_node_id,
                runs.c.current_round_ordinal,
                runs.c.state,
                wait_answers.c.run_id.label("answer_run_id"),
                wait_answers.c.revision_hash.label("answer_revision_hash"),
                wait_answers.c.node_id.label("answer_node_id"),
                wait_answers.c.round_ordinal.label("answer_round_ordinal"),
                wait_answers.c.node_execution_id,
                wait_answers.c.answer_workflow_id,
                wait_answers.c.state.label("answer_state"),
                wait_answers.c.state_version.label("answer_state_version"),
            )
            .select_from(
                wait_answers.join(runs, wait_answers.c.run_id == runs.c.run_id)
            )
            .where(wait_answers.c.answer_workflow_id == workflow_id)
        )
        .mappings()
        .one_or_none()
    )
    if record is None or str(record["state"]) != RunState.WAITING_INPUT.value:
        return None
    run_id = RunId(str(record["run_id"]))
    revision_hash = WorkflowRevisionHash(str(record["revision_hash"]))
    execution = NodeExecutionId.for_node(
        run_id,
        revision_hash,
        str(record["current_node_id"]),
        int(record["current_round_ordinal"]),
    )
    if (
        str(record["answer_state"]) != WaitAnswerState.PENDING.value
        or int(record["answer_state_version"]) != 0
        or str(record["answer_run_id"]) != run_id.value
        or str(record["answer_revision_hash"]) != revision_hash.value
        or str(record["answer_node_id"]) != str(record["current_node_id"])
        or int(record["answer_round_ordinal"]) != int(record["current_round_ordinal"])
        or str(record["node_execution_id"]) != execution.value
        or str(record["answer_workflow_id"]) != workflow_id
        or answer_workflow_id_for(execution) != workflow_id
    ):
        return None
    return _retag_workflow_status(connection, workflow_id, application_version)


def _retag_stranded_reconciliation(
    connection: Connection,
    workflow_id: str,
    command_id: ReconcileCommandId,
    application_version: str,
) -> str | None:
    candidate = (
        connection.execute(
            sa.select(
                runs.c.run_id,
                runs.c.revision_hash,
                runs.c.current_node_id,
                runs.c.current_round_ordinal,
                runs.c.state,
                effect_intents.c.logical_key,
                effect_intents.c.run_id.label("intent_run_id"),
                effect_intents.c.workflow_revision_hash.label("intent_revision_hash"),
                effect_intents.c.state.label("intent_state"),
                effect_intents.c.state_version.label("intent_state_version"),
                effect_intents.c.reconciliation_owner_command_id,
                reconcile_commands.c.logical_key.label("command_logical_key"),
                reconcile_commands.c.expected_intent_version,
                reconcile_commands.c.state.label("command_state"),
            )
            .select_from(
                effect_intents.join(
                    runs, effect_intents.c.run_id == runs.c.run_id
                ).join(
                    reconcile_commands,
                    reconcile_commands.c.command_id
                    == effect_intents.c.reconciliation_owner_command_id,
                )
            )
            .where(reconcile_commands.c.command_id == command_id.value)
        )
        .mappings()
        .one_or_none()
    )
    if candidate is None or reconcile_workflow_id_for(command_id) != workflow_id:
        return None
    if str(candidate["state"]) != RunState.WAITING_RECONCILIATION.value:
        return None
    run_id = RunId(str(candidate["run_id"]))
    revision_hash = WorkflowRevisionHash(str(candidate["revision_hash"]))
    logical_key = logical_effect_key_for_node(
        run_id,
        revision_hash,
        str(candidate["current_node_id"]),
        int(candidate["current_round_ordinal"]),
    )
    if (
        str(candidate["logical_key"]) != logical_key.value
        or str(candidate["intent_run_id"]) != run_id.value
        or str(candidate["intent_revision_hash"]) != revision_hash.value
        or str(candidate["intent_state"]) != EffectIntentState.RECONCILING.value
        or int(candidate["intent_state_version"])
        != EFFECT_INTENT_VERSION_RECONCILING.value
        or str(candidate["reconciliation_owner_command_id"]) != command_id.value
        or str(candidate["command_logical_key"]) != logical_key.value
        or int(candidate["expected_intent_version"])
        != EFFECT_INTENT_VERSION_WAITING.value
        or str(candidate["command_state"]) != ReconcileCommandState.PENDING.value
    ):
        return None
    return _retag_workflow_status(connection, workflow_id, application_version)


def _retag_workflow_status(
    connection: Connection, workflow_id: str, application_version: str
) -> str | None:
    """Conditionally move one row's `application_version`, or refuse and say why.

    Liveness of the row's own retiring version is not checked here: DBOS's
    active workflow set lives in the process that enqueued the row, and
    `workflow_status` only ever names a version, never a process, so a query
    filtered on the *launching* version's `application_version` can never
    match a row still tagged with a *different*, retiring one -- it would
    always read empty and only look like a guard. That liveness is instead an
    external precondition this sweep depends on, documented on
    `retag_stranded_continuations`.
    """
    workflow_status = (
        connection.execute(
            sa.select(
                _dbos_workflow_status.c.application_version,
                _dbos_workflow_status.c.status,
            ).where(_dbos_workflow_status.c.workflow_uuid == workflow_id)
        )
        .mappings()
        .one_or_none()
    )
    if (
        workflow_status is None
        or str(workflow_status["application_version"]) == application_version
        or str(workflow_status["status"]) not in _RETAGGABLE_WORKFLOW_STATUSES
    ):
        return None
    observed_version = str(workflow_status["application_version"])
    observed_status = str(workflow_status["status"])
    updated = connection.execute(
        _dbos_workflow_status.update()
        .where(
            _dbos_workflow_status.c.workflow_uuid == workflow_id,
            _dbos_workflow_status.c.application_version == observed_version,
            _dbos_workflow_status.c.status == observed_status,
            _dbos_workflow_status.c.status.in_(_RETAGGABLE_WORKFLOW_STATUSES),
        )
        .values(application_version=application_version)
    )
    return observed_version if updated.rowcount == 1 else None


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


def minted_workflow_ids(
    connection: Connection, workflow_ids: Iterable[str]
) -> frozenset[str]:
    """The ids among `workflow_ids` a workflow was ever durably started under.

    The other half of the question `live_driver_workflow_ids` answers, and the
    reason a caller may name ids speculatively at all: a derived id no workflow
    was ever minted under matches nothing here, so "this workflow existed" and
    "this workflow is still going to run" stay two separate facts. A caller that
    needs to tell a run nothing ever carried from one whose carrier is dead needs
    the first, in any application version -- a workflow a retired version left
    behind still proves the run got that far.
    """

    ids = tuple(workflow_ids)
    if not ids:
        return frozenset()
    return frozenset(
        connection.scalars(
            sa.select(_dbos_workflow_status.c.workflow_uuid).where(
                _dbos_workflow_status.c.workflow_uuid.in_(ids)
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
        if _no_workflow_will_move_this_run(connection, record, application_version):
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
    return _no_workflow_will_move_this_run(connection, record, application_version)


def _no_workflow_will_move_this_run(
    connection: Any, record: Mapping[Any, Any], application_version: str
) -> bool:
    """Whether nothing is ever going to move this run again.

    Two questions, in order. Did this run ever get anywhere -- so that its
    silence is an ending rather than a beginning? And of whatever carried it, is
    anything still one recovery will resume? Only a run that got somewhere and
    has no live carrier left is a dead gap.
    """

    workflow_ids = _gap_workflow_ids(connection, record)
    if not _the_run_got_somewhere(connection, record, workflow_ids):
        return False
    return not live_driver_workflow_ids(connection, workflow_ids, application_version)


def _the_run_got_somewhere(
    connection: Any, record: Mapping[Any, Any], workflow_ids: Iterable[str]
) -> bool:
    """Whether anything ever carried this run, in either of the two ways it can show.

    An attempt that succeeded proves it. So does a workflow genuinely minted for
    the run, and that second proof is the only one a carrier leaves when it dies
    or is retired before preparing its first Attempt (#636): a node workflow
    left `ENQUEUED` by a version that will never run again has no attempt to
    its name, and demanding one would leave the run `STARTED` for as long as
    the store exists.

    A run nothing has carried yet has neither, which is exactly the answer
    wanted: every id derived for it is speculative and matches no row, so a run
    whose first workflow has not been picked up is young rather than dead.
    """

    succeeded = connection.scalar(
        sa.select(1)
        .select_from(agent_attempts)
        .where(
            agent_attempts.c.run_id == str(record["run_id"]),
            agent_attempts.c.state == AgentAttemptState.SUCCEEDED.value,
        )
    )
    if succeeded is not None:
        return True
    return bool(minted_workflow_ids(connection, workflow_ids))


def _gap_workflow_ids(connection: Any, record: Mapping[Any, Any]) -> tuple[str, ...]:
    """Every workflow that could still owe this apparent gap its next move.

    A gap is dead only once nothing is going to move the run, so every driver
    the run can have belongs here, and three families can. The node and
    replacement workflows of its own nodes are one. Its effect workflows are
    another (#645): an Action node prepares its effect and returns, so the
    node workflow that would name it is already SUCCESS while the effect is
    still in flight. Leaving that family out reads a healthy V3 run standing
    on an Action node as a dead gap and ends it FAILED while its effect is
    still going to be performed. The answer workflows of its submitted Wait
    answers are the third (#923): `durable_answer` commits WAIT_ANSWERED and
    only then starts the heir, so between those two steps the run already
    stands STARTED on a node whose only driver is the pending answer
    workflow.

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
    named.update(_effect_workflow_ids(connection, run_id))
    named.update(_wait_answer_workflow_ids(connection, run_id))
    return tuple(named)


def _wait_answer_workflow_ids(connection: Any, run_id: RunId) -> tuple[str, ...]:
    """Every answer workflow that can still carry this run onto an heir.

    The row keeps the workflow id its answer was enqueued under, so it is read
    rather than derived, and an id whose workflow already finished simply
    stands in no driving status.
    """

    return tuple(
        str(value)
        for value in connection.scalars(
            sa.select(wait_answers.c.answer_workflow_id).where(
                wait_answers.c.run_id == run_id.value
            )
        )
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
