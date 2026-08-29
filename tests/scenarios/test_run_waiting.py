from __future__ import annotations

from dataclasses import dataclass

import pytest

from tests.scenarios import run_waiting


@dataclass
class _CompletedWorkflow:
    result: object

    def get_result(self) -> object:
        return self.result


def test_waits_for_the_named_workflow_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _CompletedWorkflow("WAITING_INPUT")
    monkeypatch.setattr(run_waiting.DBOS, "get_workflow_status", lambda _: object())
    monkeypatch.setattr(run_waiting.DBOS, "retrieve_workflow", lambda _: completed)

    result = run_waiting.wait_for_workflow_completion(
        "wait-node-workflow", "the wait node to write WAITING_INPUT"
    )

    assert result == "WAITING_INPUT"


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
