from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

CRASHED = 86
HARNESS = Path(__file__).with_name("agent_attempt_harness.py")


def child(root: Path, mode: str, expected: int = 0) -> None:
    result = subprocess.run(
        [sys.executable, str(HARNESS), str(root), mode],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        text=True,
        timeout=20,
    )
    assert result.returncode == expected, result.stderr


def rows(root: Path, statement: str) -> tuple[tuple[object, ...], ...]:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        return tuple(tuple(record) for record in connection.execute(statement))


def test_restart_reclaims_prepared_but_only_projects_launch_armed_as_possibly_ran(
    tmp_path: Path,
) -> None:
    prepared = tmp_path / "prepared"
    child(prepared, "crash-prepared", CRASHED)
    assert rows(prepared, "SELECT state,state_version FROM agent_attempts") == (
        ("PREPARED", 0),
    )
    child(prepared, "recover")
    assert (prepared / "counter").read_bytes() == b"x"
    assert rows(prepared, "SELECT state,state_version FROM agent_attempts") == (
        ("SUCCEEDED", 2),
    )

    armed = tmp_path / "armed"
    child(armed, "crash-armed", CRASHED)
    assert (armed / "counter").read_bytes() == b"x"
    assert rows(armed, "SELECT state,state_version FROM agent_attempts") == (
        ("LAUNCH_ARMED", 1),
    )
    child(armed, "recover")
    assert (armed / "counter").read_bytes() == b"x"
    assert (armed / "projected-attempt-state").read_text(encoding="utf-8") == (
        "POSSIBLY_RAN"
    )
    assert rows(armed, "SELECT state,state_version FROM agent_attempts") == (
        ("LAUNCH_ARMED", 1),
    )


def test_controlled_process_counter_proves_the_launch_armed_boundary(
    tmp_path: Path,
) -> None:
    child(tmp_path, "crash-armed", CRASHED)
    child(tmp_path, "recover")

    assert (tmp_path / "counter").read_bytes() == b"x"
    assert rows(
        tmp_path,
        "SELECT state,current_node_id,state_version,last_event_sequence FROM runs",
    ) == (("STARTED", "build", 0, 0),)
    assert rows(tmp_path, "SELECT COUNT(*) FROM agent_receipts_v2") == ((0,),)
    assert rows(tmp_path, "SELECT COUNT(*) FROM run_events") == ((0,),)
