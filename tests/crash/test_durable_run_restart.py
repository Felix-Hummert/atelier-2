from __future__ import annotations

import hashlib
import inspect
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from atelier2.adapters.dbos.names import (
    BOOTSTRAP_STEP_NAME,
    COMMIT_STEP_NAME,
    OBSERVE_STEP_NAME,
    RESOLVE_STEP_NAME,
)
from atelier2.adapters.dbos.workflow_ids import bootstrap_workflow_id_for
from atelier2.contracts.runs import RunId
from tests.scenarios.workflows import V3_EFFECT_LINE_DOCUMENT

CRASHED = 86
HARNESS = Path(__file__).with_name("durable_run_harness.py")


def child(
    database: Path,
    command: str,
    version: str,
    *arguments: str,
    expected: int = 0,
    timeout: float = 15,
    stderr_contains: str | None = None,
) -> None:
    result = subprocess.run(
        [sys.executable, str(HARNESS), command, str(database), version, *arguments],
        capture_output=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                (
                    str(Path(__file__).parents[2]),
                    str(Path(__file__).parents[2] / "src"),
                )
            ),
        },
        text=True,
        timeout=timeout,
    )
    assert result.returncode == expected, result.stderr
    if stderr_contains is not None:
        assert stderr_contains in result.stderr


def scalar(database: Path, statement: str, parameters: tuple[str, ...] = ()) -> object:
    with sqlite3.connect(database, timeout=30) as connection:
        row = connection.execute(statement, parameters).fetchone()
    return None if row is None else row[0]


def test_matching_executor_recovers_after_datasource_commit(tmp_path: Path) -> None:
    database = tmp_path / "atelier.sqlite"
    marker = tmp_path / "after-datasource"
    run_id = "recover-me"
    document = V3_EFFECT_LINE_DOCUMENT
    revision = hashlib.sha256(document).hexdigest()
    workflow_id = bootstrap_workflow_id_for(RunId(run_id))
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
            "SELECT COUNT(*) FROM runs WHERE run_id=? AND revision_hash=? AND state='STARTED'",
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
            "SELECT COUNT(*) FROM operation_outputs WHERE workflow_uuid=? AND function_name=?",
            (workflow_id, BOOTSTRAP_STEP_NAME),
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

    child(
        database,
        "execute",
        "executor-B",
        run_id,
        "NONE",
        "0.5",
        expected=1,
        stderr_contains="bootstrap workflow stayed PENDING",
    )
    assert (
        scalar(
            database,
            "SELECT COUNT(*) FROM operation_outputs WHERE workflow_uuid=? AND function_name=?",
            (workflow_id, BOOTSTRAP_STEP_NAME),
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
            "SELECT COUNT(*) FROM operation_outputs WHERE workflow_uuid=? AND function_name=?",
            (workflow_id, BOOTSTRAP_STEP_NAME),
        )
        == 1
    )

    assert (
        scalar(
            database,
            "SELECT state FROM runs WHERE run_id=?",
            (run_id,),
        )
        == "WAITING_INPUT"
    )
    assert (
        scalar(
            database,
            "SELECT status FROM workflow_status WHERE workflow_uuid=?",
            (workflow_id,),
        )
        == "SUCCESS"
    )


def test_private_crash_hook_signature_and_named_sentinel_match_dbos_2_29() -> None:
    from dbos._sys_db import SystemDatabase

    signature = inspect.signature(SystemDatabase.record_operation_result)

    assert tuple(signature.parameters) == (
        "self",
        "result",
        "completed_at_epoch_ms",
    )
    assert (
        signature.parameters["completed_at_epoch_ms"].kind
        is inspect.Parameter.KEYWORD_ONLY
    )
    assert (
        BOOTSTRAP_STEP_NAME,
        OBSERVE_STEP_NAME,
        RESOLVE_STEP_NAME,
        COMMIT_STEP_NAME,
    ) == ("bootstrap-run-binding", "observe/0", "resolve/1", "commit/2")


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
