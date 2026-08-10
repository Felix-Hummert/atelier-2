from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from atelier2.adapters.dbos.starter import dbos_workflow_id_for
from atelier2.contracts.runs import RunId

CRASHED = 86
HARNESS = Path(__file__).with_name("durable_run_harness.py")


def child(
    database: Path,
    command: str,
    version: str,
    *arguments: str,
    expected: int = 0,
    timeout: float = 15,
) -> None:
    result = subprocess.run(
        [sys.executable, str(HARNESS), command, str(database), version, *arguments],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        text=True,
        timeout=timeout,
    )
    assert result.returncode == expected, result.stderr


def scalar(database: Path, statement: str, parameters: tuple[str, ...] = ()) -> object:
    with sqlite3.connect(database, timeout=30) as connection:
        row = connection.execute(statement, parameters).fetchone()
    return None if row is None else row[0]


def test_matching_executor_recovers_after_datasource_commit(tmp_path: Path) -> None:
    database = tmp_path / "atelier.sqlite"
    marker = tmp_path / "after-datasource"
    run_id = "recover-me"
    document = b"workflow-v1"
    revision = hashlib.sha256(document).hexdigest()
    workflow_id = dbos_workflow_id_for(RunId(run_id))
    child(database, "initialize", "executor-A")
    child(database, "seed", "executor-A", run_id, document.hex())

    child(
        database,
        "execute",
        "executor-A",
        run_id,
        str(marker),
        "5",
        expected=CRASHED,
    )

    assert scalar(database, "SELECT COUNT(*) FROM runs") == 1
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM runs WHERE run_id=? AND revision_hash=? AND state='COMPLETED'",
            (run_id, revision),
        )
        == 1
    )
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM datasource_outputs WHERE workflow_id=?",
            (workflow_id,),
        )
        == 1
    )
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM operation_outputs WHERE workflow_uuid=? AND function_name='complete-run'",
            (workflow_id,),
        )
        == 0
    )
    assert (
        scalar(
            database,
            "SELECT status FROM workflow_status WHERE workflow_uuid=?",
            (workflow_id,),
        )
        == "PENDING"
    )

    child(database, "execute", "executor-B", run_id, "NONE", "0.5")
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM operation_outputs WHERE workflow_uuid=? AND function_name='complete-run'",
            (workflow_id,),
        )
        == 0
    )

    child(database, "execute", "executor-A", run_id, "NONE", "5")
    assert scalar(database, "SELECT COUNT(*) FROM runs") == 1
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM datasource_outputs WHERE workflow_id=?",
            (workflow_id,),
        )
        == 1
    )
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM operation_outputs WHERE workflow_uuid=? AND function_name='complete-run'",
            (workflow_id,),
        )
        == 1
    )
    assert (
        scalar(
            database,
            "SELECT status FROM workflow_status WHERE workflow_uuid=?",
            (workflow_id,),
        )
        == "SUCCESS"
    )


@pytest.mark.parametrize("version", ["executor-A", "executor-B"])
def test_executor_version_is_explicit_test_configuration(
    tmp_path: Path, version: str
) -> None:
    database = tmp_path / "atelier.sqlite"
    child(database, "initialize", version)
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM application_versions WHERE version_name=?",
            (version,),
        )
        == 1
    )
