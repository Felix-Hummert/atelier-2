from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from atelier2.adapters.dbos.names import BOOTSTRAP_STEP_NAME, COMMIT_STEP_NAME
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.run_forks import RunForkCommandId, successor_run_id_for
from atelier2.contracts.runs import RunId
from atelier2.ports.durable_run_forks import (
    DurableRunForkCreated,
    DurableRunForkExisting,
    ForkRunRequest,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from tests.crash.effect_harness import CRASHED, install_crash
from tests.integration.test_v3_open_pr_action import TREE, publish_line
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2, agent_scratch_root

HARNESS = Path(__file__)
VERSION = "run-fork-crash-v1"
ORIGIN = RunId("fork-crash-origin")
KEY = "retry-publish-after-crash"


def _runtime(root: Path) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            VERSION,
            agent_scratch_root=agent_scratch_root(root),
        ),
        GitHubEffectAdapterFactory(
            root / "github.sqlite",
            AdapterRevision("github-open-pr-v1"),
            EffectDestination("platform"),
        ),
        ExactOutputAgentExecutorFactory(),
        (
            RecordingAgentExecutorFactoryV2(
                "exact", "exact/v1", "exact-operation", TREE
            ),
        ),
    )


def _wait_for_state(root: Path, run_id: RunId, expected: str) -> None:
    deadline = time.monotonic() + 12
    observed = ""
    while time.monotonic() < deadline:
        with sqlite3.connect(root / "atelier.sqlite", timeout=30) as connection:
            row = connection.execute(
                "SELECT state FROM runs WHERE run_id=?", (run_id.value,)
            ).fetchone()
        observed = "" if row is None else str(row[0])
        if observed == expected:
            return
        time.sleep(0.025)
    raise TimeoutError(f"run {run_id.value!r} stayed {observed!r}")


def _seed(root: Path) -> None:
    runtime = _runtime(root)
    try:
        runtime.initialize_storage()
        workflow, bindings = publish_line(runtime)
        starter = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        )
        started = starter.start_published(
            StartPublishedRunRequestV2(ORIGIN, workflow.revision_hash, bindings)
        )
        if not isinstance(started, DurableRunCreated):
            raise TypeError(f"origin start was refused: {started!r}")
        runtime.launch()
        _wait_for_state(root, ORIGIN, "COMPLETED")
    finally:
        runtime.close()


def _fork_and_run(root: Path, operation: str | None, marker: Path | None) -> None:
    runtime = _runtime(root)
    try:
        starter = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        )
        result = starter.fork_run(ForkRunRequest(ORIGIN, KEY, "publish"))
        if not isinstance(result, (DurableRunForkCreated, DurableRunForkExisting)):
            raise TypeError(f"fork was refused: {result!r}")
        if operation is not None and marker is not None:
            install_crash(marker, operation, "before-record")
        runtime.launch()
        successor = successor_run_id_for(RunForkCommandId.for_request(ORIGIN, KEY))
        _wait_for_state(root, successor, "COMPLETED")
    finally:
        runtime.close()


def _child(root: Path, command: str, *arguments: str, expected: int = 0) -> None:
    result = subprocess.run(
        [sys.executable, str(HARNESS), command, str(root), *arguments],
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
        timeout=25,
    )
    assert result.returncode == expected, result.stderr


def test_fork_and_effect_reference_recover_without_a_second_run_or_pr(
    tmp_path: Path,
) -> None:
    _child(tmp_path, "seed")
    bootstrap_marker = tmp_path / "after-fork-product-commit"
    _child(
        tmp_path,
        "run",
        BOOTSTRAP_STEP_NAME,
        str(bootstrap_marker),
        expected=CRASHED,
    )
    assert bootstrap_marker.read_text() == f"{BOOTSTRAP_STEP_NAME}:before-record"

    successor = successor_run_id_for(RunForkCommandId.for_request(ORIGIN, KEY))
    with sqlite3.connect(tmp_path / "atelier.sqlite", timeout=30) as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_forks").fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id=?", (successor.value,)
        ).fetchone() == (1,)

    effect_marker = tmp_path / "after-effect-reference-product-commit"
    _child(
        tmp_path,
        "run",
        COMMIT_STEP_NAME,
        str(effect_marker),
        expected=CRASHED,
    )
    assert effect_marker.read_text() == f"{COMMIT_STEP_NAME}:before-record"
    _child(tmp_path, "run", "NONE", "NONE")

    factory = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    assert len(factory.recorded_pull_requests()) == 1
    with sqlite3.connect(tmp_path / "atelier.sqlite", timeout=30) as connection:
        assert connection.execute("SELECT COUNT(*) FROM run_forks").fetchone() == (1,)
        assert connection.execute(
            "SELECT state FROM runs WHERE run_id=?", (successor.value,)
        ).fetchone() == ("COMPLETED",)
        assert connection.execute(
            "SELECT confirmation_source, fork_source_run_id FROM effect_receipts "
            "WHERE run_id=?",
            (successor.value,),
        ).fetchone() == ("FORK_REFERENCE", ORIGIN.value)


def _main() -> None:
    command, raw_root, *arguments = sys.argv[1:]
    root = Path(raw_root)
    if command == "seed":
        _seed(root)
        return
    raw_operation, raw_marker = arguments
    _fork_and_run(
        root,
        None if raw_operation == "NONE" else raw_operation,
        None if raw_marker == "NONE" else Path(raw_marker),
    )


if __name__ == "__main__":
    _main()
