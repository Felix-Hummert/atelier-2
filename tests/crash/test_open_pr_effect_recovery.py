"""Crash twin of test_effect_recovery against the GitHub open-pr fake."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.adapters.github.marker import body_carries_request_hash
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from tests.crash.effect_harness import ADAPTER_EXECUTE_AFTER_COMMIT, CRASHED
from tests.crash.open_pr_harness import CANARY_TOKEN
from tests.crash.test_effect_recovery import DOCUMENT

HARNESS = Path(__file__).with_name("open_pr_harness.py")
VERSION = "executor-A"


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
            str(root / "github.sqlite"),
            VERSION,
            *arguments,
        ],
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


def seed(root: Path, run_id: str) -> None:
    child(root, "initialize")
    child(root, "seed", run_id, DOCUMENT.hex())


def recorded_pull_requests(root: Path) -> tuple[tuple[str, int, str, str], ...]:
    factory = GitHubEffectAdapterFactory(
        root / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    return tuple(
        (item.branch, item.pr_number, item.body, item.request_hash)
        for item in factory.recorded_pull_requests()
    )


@pytest.mark.proves("a-v3-action-opens-one-pr-and-a-replay-does-not-create-a-twin")
def test_c2_crash_after_github_create_before_adapter_return_recovers_one_pr(
    tmp_path: Path,
) -> None:
    run_id = "c2-open-pr-after-create"
    seed(tmp_path, run_id)
    marker = tmp_path / "crash-after-create"

    child(
        tmp_path,
        "execute",
        run_id,
        str(marker),
        ADAPTER_EXECUTE_AFTER_COMMIT,
        "after-external-commit",
        "8",
        expected=CRASHED,
    )
    assert marker.read_text() == f"{ADAPTER_EXECUTE_AFTER_COMMIT}:after-external-commit"
    first = recorded_pull_requests(tmp_path)
    assert len(first) == 1
    assert first[0][1] == 1
    with sqlite3.connect(tmp_path / "atelier.sqlite", timeout=30) as connection:
        receipts = connection.execute("SELECT COUNT(*) FROM effect_receipts").fetchone()
        assert receipts is not None and receipts[0] == 0

    child(tmp_path, "execute", run_id, "NONE", "NONE", "before-record", "8")

    recovered = recorded_pull_requests(tmp_path)
    assert recovered == first
    with sqlite3.connect(tmp_path / "atelier.sqlite", timeout=30) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM effect_receipts"
        ).fetchone() == (1,)
        request_hash = str(
            connection.execute("SELECT request_hash FROM effect_intents").fetchone()[0]
        )
        result = bytes(
            connection.execute("SELECT result FROM effect_receipts").fetchone()[0]
        )
        events = tuple(
            row[0]
            for row in connection.execute(
                "SELECT event_kind FROM run_events ORDER BY event_sequence"
            )
        )
    assert body_carries_request_hash(recovered[0][2], request_hash)
    assert b'"pr_number":1' in result
    assert CANARY_TOKEN.encode() not in result
    assert CANARY_TOKEN not in recovered[0][2]
    assert events[-2:] == ("ACTION_COMPLETED", "WAITING_INPUT")
