from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

CRITERIA = (
    "canonical_sqlite",
    "atomic_start",
    "datasource_recovery",
    "version_fence",
    "effect_reconciliation",
    "concurrent_recovery",
    "crash_boundaries",
)


@pytest.mark.parametrize("criterion", CRITERIA)
def test_dbos_satisfies_the_durable_runtime_contract(
    criterion: str, tmp_path: Path
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "spikes/durable-runtime/probe.py",
            "criterion",
            criterion,
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[2] / "src")},
        text=True,
        timeout=45,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "criterion": criterion,
        "decision": "PASS_DBOS",
        "passed": True,
    }
    assert list(tmp_path.iterdir()) == []
