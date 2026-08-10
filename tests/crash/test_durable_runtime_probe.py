from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from atelier2.adapters.dbos.runtime import EXECUTOR_ID

CRASHED = 86
PROBE = Path("spikes/durable-runtime/probe.py")
WORKFLOW_ID = "atelier2-c3-probe-run"


def child(root: Path, command: str, expected: int = 0) -> None:
    result = subprocess.run(
        [sys.executable, str(PROBE), command, str(root), "8"],
        check=False,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        text=True,
        timeout=15,
    )
    assert result.returncode == expected, result.stderr


def scalar(root: Path, statement: str, parameters: tuple[str, ...] = ()) -> object:
    with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
        row = connection.execute(statement, parameters).fetchone()
    return None if row is None else row[0]


def test_c3_replays_durable_input_before_following_work(tmp_path: Path) -> None:
    child(tmp_path, "initialize")
    child(tmp_path, "seed")

    child(tmp_path, "execute", expected=CRASHED)

    assert (tmp_path / "c3-after-input").read_text() == "C3:after-durable-input"
    assert (
        scalar(
            tmp_path,
            "SELECT COUNT(*) FROM datasource_outputs WHERE workflow_id=?",
            (WORKFLOW_ID,),
        )
        == 1
    )
    assert scalar(tmp_path, "SELECT COUNT(*) FROM probe_c3_results") == 0

    child(tmp_path, "execute")

    assert scalar(tmp_path, "SELECT COUNT(*) FROM probe_c3_results") == 1
    assert (
        scalar(
            tmp_path,
            "SELECT status FROM workflow_status WHERE workflow_uuid=?",
            (WORKFLOW_ID,),
        )
        == "SUCCESS"
    )
    assert (
        scalar(
            tmp_path,
            "SELECT executor_id FROM workflow_status WHERE workflow_uuid=?",
            (WORKFLOW_ID,),
        )
        == EXECUTOR_ID
    )
