from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from sqlalchemy import event

from atelier2.adapters.dbos.names import BOOTSTRAP_STEP_NAME, COMMIT_STEP_NAME
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import DbosDurableRunStarter
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.github.effects import GitHubEffectAdapterFactory
from atelier2.contracts.effects import (
    AdapterRevision,
    EffectAdapterBinding,
    EffectDestination,
    EffectIntent,
    EffectReadback,
    EffectUnknownOutcome,
    PerformedEffect,
)
from atelier2.contracts.run_forks import RunForkCommandId, successor_run_id_for
from atelier2.contracts.runs import RunId
from atelier2.ports.durable_run_forks import (
    DurableRunForkCreated,
    DurableRunForkExisting,
    ForkRunRequest,
)
from atelier2.ports.durable_runs import DurableRunCreated, StartPublishedRunRequestV2
from atelier2.ports.effects import EffectAdapter
from tests.crash.effect_harness import CRASHED, install_crash
from tests.integration.test_v3_open_pr_action import TREE, publish_line
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2, agent_scratch_root

HARNESS = Path(__file__)
VERSION = "run-fork-crash-v1"
ORIGIN = RunId("fork-crash-origin")
KEY = "retry-publish-after-crash"
ADAPTER_CALLS = "adapter-calls"


class _RecordingEffectAdapter:
    def __init__(self, delegate: EffectAdapter, calls: Path) -> None:
        self._delegate = delegate
        self._calls = calls

    def readback(self, intent: EffectIntent) -> EffectReadback:
        with self._calls.open("a", encoding="utf-8") as stream:
            stream.write("readback\n")
        return self._delegate.readback(intent)

    def execute(self, intent: EffectIntent) -> PerformedEffect | EffectUnknownOutcome:
        with self._calls.open("a", encoding="utf-8") as stream:
            stream.write("execute\n")
        return self._delegate.execute(intent)

    def close(self) -> None:
        self._delegate.close()


class _RecordingEffectAdapterFactory:
    def __init__(self, root: Path) -> None:
        self._delegate = GitHubEffectAdapterFactory(
            root / "github.sqlite",
            AdapterRevision("github-open-pr-v1"),
            EffectDestination("platform"),
        )
        self._calls = root / ADAPTER_CALLS

    @property
    def binding(self) -> EffectAdapterBinding:
        return self._delegate.binding

    @property
    def proves_absence(self) -> bool:
        return self._delegate.proves_absence

    def open(self) -> _RecordingEffectAdapter:
        return _RecordingEffectAdapter(self._delegate.open(), self._calls)


def _runtime(root: Path) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            VERSION,
            agent_scratch_root=agent_scratch_root(root),
        ),
        _RecordingEffectAdapterFactory(root),
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


def _crash_after_successor_insert(root: Path, marker: Path) -> None:
    runtime = _runtime(root)
    successor = successor_run_id_for(RunForkCommandId.for_request(ORIGIN, KEY))

    def kill_between_successor_and_fence(
        _connection: object,
        _cursor: object,
        statement: str,
        parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.startswith("INSERT INTO runs") and successor.value in str(
            parameters
        ):
            marker.write_text("successor-inserted-before-fence", encoding="utf-8")
            os._exit(CRASHED)

    event.listen(
        runtime.engine, "after_cursor_execute", kill_between_successor_and_fence
    )
    try:
        starter = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        )
        starter.fork_run(ForkRunRequest(ORIGIN, KEY, "publish"))
        raise AssertionError("fork transaction crossed the injected crash boundary")
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


def test_crash_between_successor_and_fence_rolls_back_before_adapter_observation(
    tmp_path: Path,
) -> None:
    _child(tmp_path, "seed")
    calls = tmp_path / ADAPTER_CALLS
    calls.write_text("", encoding="utf-8")
    marker = tmp_path / "between-successor-and-fence"

    _child(tmp_path, "fork-transaction", str(marker), expected=CRASHED)

    assert marker.read_text(encoding="utf-8") == "successor-inserted-before-fence"
    successor = successor_run_id_for(RunForkCommandId.for_request(ORIGIN, KEY))
    with sqlite3.connect(tmp_path / "atelier.sqlite", timeout=30) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM runs WHERE run_id=?", (successor.value,)
        ).fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM run_forks").fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM run_fork_effect_fences"
        ).fetchone() == (0,)

    _child(tmp_path, "run", "NONE", "NONE")

    assert calls.read_text(encoding="utf-8") == ""
    factory = GitHubEffectAdapterFactory(
        tmp_path / "github.sqlite",
        AdapterRevision("github-open-pr-v1"),
        EffectDestination("platform"),
    )
    assert len(factory.recorded_pull_requests()) == 1
    with sqlite3.connect(tmp_path / "atelier.sqlite", timeout=30) as connection:
        assert connection.execute(
            "SELECT state FROM runs WHERE run_id=?", (successor.value,)
        ).fetchone() == ("COMPLETED",)
        assert connection.execute(
            "SELECT confirmation_source FROM effect_receipts WHERE run_id=?",
            (successor.value,),
        ).fetchone() == ("FORK_REFERENCE",)


def _main() -> None:
    command, raw_root, *arguments = sys.argv[1:]
    root = Path(raw_root)
    if command == "seed":
        _seed(root)
        return
    if command == "fork-transaction":
        _crash_after_successor_insert(root, Path(arguments[0]))
        return
    raw_operation, raw_marker = arguments
    _fork_and_run(
        root,
        None if raw_operation == "NONE" else raw_operation,
        None if raw_marker == "NONE" else Path(raw_marker),
    )


if __name__ == "__main__":
    _main()
