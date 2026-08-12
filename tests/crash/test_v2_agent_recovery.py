from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from atelier2.adapters.dbos.workflow import AGENT_COMMIT_STEP_NAME

CRASHED = 86
HARNESS = Path(__file__).with_name("v2_agent_harness.py")


def child(
    database: Path,
    command: str,
    identity: str,
    output: bytes,
    marker: Path | None = None,
    *,
    expected: int = 0,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(HARNESS),
            command,
            str(database),
            identity,
            output.hex(),
            "NONE" if marker is None else str(marker),
        ],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        text=True,
        timeout=20,
    )
    assert result.returncode == expected, result.stderr


def rows(database: Path, statement: str) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(database, timeout=30) as connection:
        return tuple(tuple(row) for row in connection.execute(statement))


def test_v2_agent_commit_crash_recovers_without_rebinding_or_duplicates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "atelier.sqlite"
    marker = tmp_path / "agent-v2-crash"
    original_output = b"\x00\xfforiginal"

    child(database, "seed", "before-crash", original_output)
    child(
        database,
        "execute",
        "before-crash",
        original_output,
        marker,
        expected=CRASHED,
    )

    assert marker.read_text() == AGENT_COMMIT_STEP_NAME
    assert rows(
        database,
        "SELECT state,current_node_id,state_version,last_event_sequence FROM runs",
    ) == (("STARTED", "done", 1, 1),)
    assert rows(
        database,
        "SELECT role,executor_operational_identity,output_bytes FROM agent_receipts_v2",
    ) == (("builder", "before-crash", original_output),)
    assert rows(
        database,
        "SELECT event_sequence,event_kind,payload FROM run_events ORDER BY event_sequence",
    ) == ((1, "AGENT_COMPLETED", original_output),)

    child(database, "execute", "after-restart", b"different-output")

    assert rows(
        database,
        "SELECT state,current_node_id,state_version,last_event_sequence FROM runs",
    ) == (("COMPLETED", "done", 2, 2),)
    assert rows(
        database,
        "SELECT role,executor_operational_identity,output_bytes FROM agent_receipts_v2",
    ) == (("builder", "before-crash", original_output),)
    assert rows(
        database,
        "SELECT event_sequence,event_kind,payload FROM run_events ORDER BY event_sequence",
    ) == (
        (1, "AGENT_COMPLETED", original_output),
        (2, "SUBWORKFLOW_COMPLETED", b"5"),
    )
