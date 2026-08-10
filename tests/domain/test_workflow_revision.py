from __future__ import annotations

import hashlib
from typing import Any

import pytest

from atelier2.contracts.runs import RunId, RunState, StartRunRequest, WorkflowRevision


@pytest.mark.parametrize("document", [b"workflow-v1", b"", b"\x00\xffrevision"])
def test_workflow_revision_hashes_the_exact_document(document: bytes) -> None:
    revision = WorkflowRevision(document)

    assert revision.document == document
    assert revision.revision_hash.value == hashlib.sha256(document).hexdigest()


@pytest.mark.parametrize("value", ["run-1", " run-1 ", "\x00run", "\N{SNOWMAN}"])
def test_run_id_preserves_the_callers_exact_nonempty_string(value: str) -> None:
    assert RunId(value).value == value


def test_run_id_rejects_only_empty_input() -> None:
    with pytest.raises(ValueError, match="nonempty"):
        RunId("")


@pytest.mark.parametrize("state", list(RunState))
def test_run_state_round_trips_through_its_durable_token(state: RunState) -> None:
    assert RunState(state.value) is state


def test_a_run_awaiting_reconciliation_has_a_durable_token() -> None:
    assert RunState("WAITING_RECONCILIATION") is RunState.WAITING_RECONCILIATION


def test_start_request_is_frozen() -> None:
    request = StartRunRequest(RunId("run-1"), WorkflowRevision(b"workflow-v1"))
    mutable_view: Any = request

    with pytest.raises((AttributeError, TypeError)):
        mutable_view.run_id = RunId("changed")
