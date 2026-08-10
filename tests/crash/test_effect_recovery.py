from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from atelier2.adapters.dbos.advancer import effect_workflow_id_for
from atelier2.adapters.dbos.workflow import (
    COMMIT_STEP_NAME,
    OBSERVE_STEP_NAME,
    RESOLVE_STEP_NAME,
)
from atelier2.contracts.effects import LogicalEffectKey

CRASHED = 86
HARNESS = Path(__file__).with_name("effect_harness.py")
VERSION = "executor-A"
DOCUMENT = b"workflow-v1"


def child(
    root: Path,
    command: str,
    *arguments: str,
    expected: int = 0,
    timeout: float = 20,
) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(HARNESS),
            command,
            str(root / "atelier.sqlite"),
            str(root / "external.sqlite"),
            VERSION,
            str(root / "force-unknown"),
            *arguments,
        ],
        capture_output=True,
        check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        text=True,
        timeout=timeout,
    )
    assert result.returncode == expected, result.stderr


def scalar(
    root: Path, database: str, statement: str, parameters: tuple[str, ...] = ()
) -> object:
    with sqlite3.connect(root / database, timeout=30) as connection:
        row = connection.execute(statement, parameters).fetchone()
    return None if row is None else row[0]


def seed(root: Path, run_id: str) -> None:
    child(root, "initialize")
    child(root, "seed", run_id, DOCUMENT.hex())


def crash(
    root: Path,
    run_id: str,
    operation: str,
    timing: str,
) -> Path:
    marker = root / f"crash-{operation.replace('/', '-')}-{timing}"
    child(
        root,
        "execute",
        run_id,
        str(marker),
        operation,
        timing,
        "8",
        expected=CRASHED,
    )
    assert marker.read_text() == f"{operation}:{timing}"
    return marker


def resume(root: Path, run_id: str) -> None:
    child(root, "execute", run_id, "NONE", "NONE", "before-record", "8")


def assert_completed_once(root: Path, run_id: str) -> None:
    logical_key = f"{run_id}/action-1"
    assert (
        scalar(
            root,
            "atelier.sqlite",
            "SELECT COUNT(*) FROM runs WHERE run_id=? AND state='COMPLETED'",
            (run_id,),
        )
        == 1
    )
    assert (
        scalar(
            root,
            "atelier.sqlite",
            "SELECT COUNT(*) FROM effect_receipts WHERE logical_key=?",
            (logical_key,),
        )
        == 1
    )
    assert (
        scalar(
            root,
            "external.sqlite",
            "SELECT calls FROM loopback_effect_calls WHERE logical_key=?",
            (logical_key,),
        )
        == 1
    )


@pytest.mark.parametrize(
    ("operation", "timing", "outer_results_before"),
    [
        pytest.param(OBSERVE_STEP_NAME, "after-record", 1, id="C1-after-observe"),
        pytest.param(RESOLVE_STEP_NAME, "before-record", 1, id="C2-after-effect"),
        pytest.param(COMMIT_STEP_NAME, "before-record", 2, id="post-confirm"),
    ],
)
def test_named_crash_boundaries_recover_without_duplicate_effect(
    tmp_path: Path,
    operation: str,
    timing: str,
    outer_results_before: int,
) -> None:
    run_id = operation.replace("/", "-")
    seed(tmp_path, run_id)

    crash(tmp_path, run_id, operation, timing)

    workflow_id = effect_workflow_id_for(LogicalEffectKey(f"{run_id}/action-1"))
    assert (
        scalar(
            tmp_path,
            "atelier.sqlite",
            "SELECT COUNT(*) FROM operation_outputs WHERE workflow_uuid=?",
            (workflow_id,),
        )
        == outer_results_before
    )
    if operation == RESOLVE_STEP_NAME:
        assert (
            scalar(
                tmp_path,
                "external.sqlite",
                "SELECT calls FROM loopback_effect_calls",
            )
            == 1
        )
        assert (
            scalar(
                tmp_path,
                "atelier.sqlite",
                "SELECT state FROM effect_intents",
            )
            == "PREPARED"
        )
    if operation == COMMIT_STEP_NAME:
        assert_completed_once(tmp_path, run_id)

    resume(tmp_path, run_id)

    assert_completed_once(tmp_path, run_id)
    assert (
        scalar(
            tmp_path,
            "atelier.sqlite",
            "SELECT status FROM workflow_status WHERE workflow_uuid=?",
            (workflow_id,),
        )
        == "SUCCESS"
    )


def test_unknown_commit_replays_waiting_even_after_provider_availability_changes(
    tmp_path: Path,
) -> None:
    run_id = "unknown-provider-change"
    seed(tmp_path, run_id)
    (tmp_path / "force-unknown").touch()

    crash(tmp_path, run_id, COMMIT_STEP_NAME, "before-record")

    assert (
        scalar(tmp_path, "atelier.sqlite", "SELECT state FROM effect_intents")
        == "WAITING_RECONCILIATION"
    )
    assert (
        scalar(tmp_path, "atelier.sqlite", "SELECT COUNT(*) FROM effect_receipts") == 0
    )
    assert (
        scalar(
            tmp_path, "external.sqlite", "SELECT COUNT(*) FROM loopback_effect_calls"
        )
        == 0
    )
    (tmp_path / "force-unknown").unlink()

    resume(tmp_path, run_id)

    assert (
        scalar(tmp_path, "atelier.sqlite", "SELECT state FROM effect_intents")
        == "WAITING_RECONCILIATION"
    )
    assert (
        scalar(
            tmp_path, "external.sqlite", "SELECT COUNT(*) FROM loopback_effect_calls"
        )
        == 0
    )
    child(tmp_path, "submit-absence", run_id)
    child(tmp_path, "execute", run_id, "NONE", "NONE", "before-record", "8")
    assert_completed_once(tmp_path, run_id)


def test_two_matching_recovery_processes_converge_after_c2(tmp_path: Path) -> None:
    run_id = "concurrent-recovery"
    seed(tmp_path, run_id)
    crash(tmp_path, run_id, RESOLVE_STEP_NAME, "before-record")
    command = [
        sys.executable,
        str(HARNESS),
        "execute",
        str(tmp_path / "atelier.sqlite"),
        str(tmp_path / "external.sqlite"),
        VERSION,
        str(tmp_path / "force-unknown"),
        run_id,
        "NONE",
        "NONE",
        "before-record",
        "8",
    ]

    processes = [
        subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        )
        for _ in range(2)
    ]
    for process in processes:
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, f"{stdout}\n{stderr}"

    assert_completed_once(tmp_path, run_id)
