"""Store half of serve-start run convergence: STARTED rows that already failed."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.schema import agent_attempts, run_events, runs
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.agent_attempts import (
    TERMINAL_AGENT_ATTEMPT_STATES,
    AgentAttemptState,
)
from atelier2.contracts.executions import terminal_hash_for
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash


class DbosUncontinuableRunStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def uncontinuable_runs(self) -> tuple[RunId, ...]:
        terminal_attempt = tuple(state.value for state in TERMINAL_AGENT_ATTEMPT_STATES)
        with self._engine.connect() as connection:
            rows = connection.execute(
                sa.select(runs.c.run_id)
                .where(
                    runs.c.state == RunState.STARTED.value,
                    sa.exists(
                        sa.select(1)
                        .select_from(agent_attempts)
                        .where(
                            agent_attempts.c.run_id == runs.c.run_id,
                            agent_attempts.c.node_id == runs.c.current_node_id,
                            agent_attempts.c.state == AgentAttemptState.FAILED.value,
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

    def end_uncontinuable_run(self, run_id: RunId) -> bool:
        with canonical_write_transaction(self._engine) as connection:
            record = (
                connection.execute(
                    sa.select(
                        runs.c.run_id,
                        runs.c.revision_hash,
                        runs.c.current_node_id,
                        runs.c.state,
                        runs.c.state_version,
                        runs.c.last_event_sequence,
                    ).where(runs.c.run_id == run_id.value)
                )
                .mappings()
                .one_or_none()
            )
            if record is None or str(record["state"]) != RunState.STARTED.value:
                return False
            if not _current_node_has_failed_attempt(connection, record):
                return False
            event_hashes = tuple(
                Sha256Hash(str(value))
                for value in connection.execute(
                    sa.select(run_events.c.event_hash)
                    .where(run_events.c.run_id == run_id.value)
                    .order_by(run_events.c.event_sequence)
                ).scalars()
            )
            if not event_hashes:
                return False
            terminal_hash = terminal_hash_for(
                WorkflowRevisionHash(str(record["revision_hash"])),
                event_hashes,
            )
            updated = connection.execute(
                runs.update()
                .where(
                    runs.c.run_id == run_id.value,
                    runs.c.state == RunState.STARTED.value,
                    runs.c.state_version == int(record["state_version"]),
                    runs.c.last_event_sequence == int(record["last_event_sequence"]),
                )
                .values(
                    state=RunState.FAILED.value,
                    state_version=int(record["state_version"]) + 1,
                    terminal_hash=terminal_hash.value,
                )
            )
            return updated.rowcount == 1


def _current_node_has_failed_attempt(
    connection: Any, record: Mapping[Any, Any]
) -> bool:
    found = connection.scalar(
        sa.select(agent_attempts.c.attempt_id)
        .where(
            agent_attempts.c.run_id == str(record["run_id"]),
            agent_attempts.c.node_id == str(record["current_node_id"]),
            agent_attempts.c.state == AgentAttemptState.FAILED.value,
        )
        .limit(1)
    )
    return found is not None
