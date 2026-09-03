from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
import sqlalchemy as sa

from atelier2.contracts.runs import RunId, RunState
from tests.scenarios import run_waiting


@dataclass
class _CompletedWorkflow:
    result: object

    def get_result(self) -> object:
        return self.result


@dataclass
class _FailingWorkflow:
    error: Exception

    def get_result(self) -> object:
        raise self.error


@dataclass
class _WorkflowStatusStub:
    status: str


def _run_database(database_path: Path, state: RunState) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE runs (run_id TEXT, state TEXT)")
        connection.execute(
            "INSERT INTO runs VALUES (?, ?)", ("waited-run", state.value)
        )


def _timeout_after_one_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    moments = iter((0.0, 0.0, 15.0))
    monkeypatch.setattr(run_waiting.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(run_waiting.time, "sleep", lambda _: None)


def test_waits_for_the_named_workflow_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _CompletedWorkflow("WAITING_INPUT")
    statuses = iter(
        (
            _WorkflowStatusStub(run_waiting.WorkflowStatusString.PENDING.value),
            _WorkflowStatusStub(run_waiting.WorkflowStatusString.PENDING.value),
            _WorkflowStatusStub(run_waiting.WorkflowStatusString.SUCCESS.value),
        )
    )
    monkeypatch.setattr(
        run_waiting.DBOS, "get_workflow_status", lambda _: next(statuses)
    )
    monkeypatch.setattr(run_waiting.DBOS, "retrieve_workflow", lambda _: completed)
    monkeypatch.setattr(run_waiting.time, "sleep", lambda _: None)

    result = run_waiting.wait_for_workflow_completion(
        "wait-node-workflow", "the wait node to write WAITING_INPUT"
    )

    assert result == "WAITING_INPUT"


def test_completion_wait_still_propagates_the_workflow_own_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failing = _FailingWorkflow(RuntimeError("boom"))
    status = _WorkflowStatusStub(run_waiting.WorkflowStatusString.ERROR.value)
    monkeypatch.setattr(run_waiting.DBOS, "get_workflow_status", lambda _: status)
    monkeypatch.setattr(run_waiting.DBOS, "retrieve_workflow", lambda _: failing)

    with pytest.raises(RuntimeError, match="boom"):
        run_waiting.wait_for_workflow_completion(
            "wait-node-workflow", "the wait node to fail"
        )


def test_completion_wait_fails_loudly_when_the_workflow_never_completes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pending_status = _WorkflowStatusStub(run_waiting.WorkflowStatusString.PENDING.value)
    monkeypatch.setattr(
        run_waiting.DBOS, "get_workflow_status", lambda _: pending_status
    )
    monkeypatch.setattr(run_waiting, "_WORKFLOW_COMPLETION_TIMEOUT_SECONDS", 1.0)
    moments = iter((0.0, 0.0, 0.0, 2.0))
    monkeypatch.setattr(run_waiting.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(run_waiting.time, "sleep", lambda _: None)

    with pytest.raises(
        TimeoutError,
        match=(
            r"awaited the wait node to complete did not complete within "
            r"1\.0 s; last status PENDING"
        ),
    ):
        run_waiting.wait_for_workflow_completion(
            "wait-node-workflow", "the wait node to complete"
        )


def test_refuses_to_succeed_when_the_named_workflow_never_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    moments = iter((0.0, 0.0, 1.0))
    monkeypatch.setattr(run_waiting, "_WORKFLOW_CREATION_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(run_waiting.time, "monotonic", lambda: next(moments))
    monkeypatch.setattr(run_waiting.time, "sleep", lambda _: None)
    monkeypatch.setattr(run_waiting.DBOS, "get_workflow_status", lambda _: None)

    with pytest.raises(AssertionError, match="the wait node to write WAITING_INPUT"):
        run_waiting.wait_for_workflow_completion(
            "wait-node-workflow", "the wait node to write WAITING_INPUT"
        )


def test_waits_for_a_live_run_to_reach_its_state(tmp_path: Path) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _run_database(database_path, RunState.COMPLETED)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    try:
        run_waiting.wait_for_run_state(engine, RunId("waited-run"), RunState.COMPLETED)
    finally:
        engine.dispose()


def test_live_run_wait_names_the_last_observed_state_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _run_database(database_path, RunState.STARTED)
    engine = sa.create_engine(f"sqlite:///{database_path}")
    _timeout_after_one_observation(monkeypatch)
    try:
        with pytest.raises(TimeoutError, match="waited-run.*STARTED.*COMPLETED"):
            run_waiting.wait_for_run_state(
                engine, RunId("waited-run"), RunState.COMPLETED
            )
    finally:
        engine.dispose()


def test_waits_for_a_crashed_runtime_database_to_reach_its_state(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _run_database(database_path, RunState.COMPLETED)

    run_waiting.wait_for_sqlite_run_state(
        database_path, RunId("waited-run"), RunState.COMPLETED
    )


def test_crashed_runtime_wait_names_the_last_observed_state_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_path = tmp_path / "atelier.sqlite"
    _run_database(database_path, RunState.STARTED)
    _timeout_after_one_observation(monkeypatch)

    with pytest.raises(TimeoutError, match="waited-run.*STARTED.*COMPLETED"):
        run_waiting.wait_for_sqlite_run_state(
            database_path, RunId("waited-run"), RunState.COMPLETED
        )
