"""Wait for the durable workflow event a scenario needs before reading its state."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Never

import sqlalchemy as sa
from dbos import DBOS
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.schema import runs
from atelier2.contracts.runs import RunId, RunState

_WORKFLOW_CREATION_TIMEOUT_SECONDS = 16.0
_WORKFLOW_CREATION_POLL_SECONDS = 0.025
# A run can cross a child-workflow or crash-restart handoff before its state is
# durable; retain the existing 15-second ceiling while returning immediately.
_RUN_STATE_TIMEOUT_SECONDS = 15.0
_RUN_STATE_POLL_SECONDS = 0.025


def wait_for_workflow_completion(workflow_id: str, awaited: str) -> Any:
    """Return a workflow's result after DBOS records its completion.

    The workflow may be enqueued by preceding durable work, so its status is
    observed until DBOS creates it. Once present, ``get_result`` waits for its
    completion event and propagates any workflow failure unchanged.
    """
    deadline = time.monotonic() + _WORKFLOW_CREATION_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if DBOS.get_workflow_status(workflow_id) is not None:
            return DBOS.retrieve_workflow(workflow_id).get_result()
        time.sleep(_WORKFLOW_CREATION_POLL_SECONDS)
    raise AssertionError(f"never observed {awaited}")


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
