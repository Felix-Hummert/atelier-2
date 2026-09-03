"""Wait for the durable workflow event a scenario needs before reading its state."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Never

import sqlalchemy as sa
from dbos import DBOS, WorkflowStatus, WorkflowStatusString
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.schema import runs
from atelier2.contracts.runs import RunId, RunState

_WORKFLOW_CREATION_TIMEOUT_SECONDS = 16.0
_WORKFLOW_CREATION_POLL_SECONDS = 0.025
# dbos.WorkflowHandle.get_result has no timeout parameter (only
# polling_interval_sec), so a stuck awaited workflow would otherwise hang the
# caller for the rest of the suite's 30-minute budget with no diagnostic. The
# ceiling must clear every legitimate wait observed in this suite (5-16 s) plus
# a crash-restart handoff, which can legitimately take seconds; 30 s matches
# the existing crash-restart-handoff ceiling in
# tests/witness/runner_candidate_core.py::_ACCEPT_TIMEOUT_SECONDS.
_WORKFLOW_COMPLETION_TIMEOUT_SECONDS = 30.0
_ACTIVE_WORKFLOW_STATUSES = frozenset(
    {
        WorkflowStatusString.PENDING.value,
        WorkflowStatusString.ENQUEUED.value,
        WorkflowStatusString.DELAYED.value,
    }
)
# A run can cross a child-workflow or crash-restart handoff before its state is
# durable; retain the existing 15-second ceiling while returning immediately.
_RUN_STATE_TIMEOUT_SECONDS = 15.0
_RUN_STATE_POLL_SECONDS = 0.025


def wait_for_workflow_completion(workflow_id: str, awaited: str) -> Any:
    """Return a workflow's result after DBOS records its completion.

    The workflow may be enqueued by preceding durable work, so its status is
    observed until DBOS creates it. Once present, its status is polled until
    it reaches a terminal status, then ``get_result`` returns immediately and
    still propagates any workflow failure unchanged. A stuck workflow raises
    loudly instead of hanging on DBOS's unbounded ``get_result`` wait.
    """
    status = _wait_for_workflow_creation(workflow_id, awaited)
    _wait_for_terminal_status(workflow_id, awaited, status)
    return DBOS.retrieve_workflow(workflow_id).get_result()


def _wait_for_workflow_creation(workflow_id: str, awaited: str) -> WorkflowStatus:
    deadline = time.monotonic() + _WORKFLOW_CREATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        status = DBOS.get_workflow_status(workflow_id)
        if status is not None:
            return status
        time.sleep(_WORKFLOW_CREATION_POLL_SECONDS)
    raise AssertionError(f"never observed {awaited}")


def _wait_for_terminal_status(
    workflow_id: str, awaited: str, status: WorkflowStatus
) -> None:
    deadline = time.monotonic() + _WORKFLOW_COMPLETION_TIMEOUT_SECONDS
    while status.status in _ACTIVE_WORKFLOW_STATUSES:
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"awaited {awaited} did not complete within "
                f"{_WORKFLOW_COMPLETION_TIMEOUT_SECONDS} s; "
                f"last status {status.status}"
            )
        time.sleep(_WORKFLOW_CREATION_POLL_SECONDS)
        observed = DBOS.get_workflow_status(workflow_id)
        if observed is None:
            raise TimeoutError(
                f"awaited {awaited} lost its status after being observed; "
                f"last status {status.status}"
            )
        status = observed


def wait_for_run_state(engine: Engine, run_id: RunId, expected: RunState) -> None:
    """Wait until the live runtime persists ``expected`` for ``run_id``."""
    deadline = time.monotonic() + _RUN_STATE_TIMEOUT_SECONDS
    observed: str | None = None
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            observed = connection.scalar(
                sa.select(runs.c.state).where(runs.c.run_id == run_id.value)
            )
        if observed == expected.value:
            return
        time.sleep(_RUN_STATE_POLL_SECONDS)
    _raise_run_state_timeout(run_id, expected, observed)


def wait_for_sqlite_run_state(
    database_path: Path, run_id: RunId, expected: RunState
) -> None:
    """Wait for a crashed runtime's SQLite file to persist ``expected``."""
    deadline = time.monotonic() + _RUN_STATE_TIMEOUT_SECONDS
    observed: str | None = None
    while time.monotonic() < deadline:
        with sqlite3.connect(database_path, timeout=30) as connection:
            row = connection.execute(
                "SELECT state FROM runs WHERE run_id=?", (run_id.value,)
            ).fetchone()
        observed = None if row is None else str(row[0])
        if observed == expected.value:
            return
        time.sleep(_RUN_STATE_POLL_SECONDS)
    _raise_run_state_timeout(run_id, expected, observed)


def _raise_run_state_timeout(
    run_id: RunId, expected: RunState, observed: str | None
) -> Never:
    raise TimeoutError(
        f"run {run_id.value!r} stayed {observed!r}, expected {expected.value!r}"
    )
