from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace

import pytest

from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import (
    Run,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)


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


def _revision_hash() -> WorkflowRevisionHash:
    return WorkflowRevisionHash("2" * 64)


def _terminal_hash() -> Sha256Hash:
    return Sha256Hash.of(b"terminal")


def _run() -> Run:
    return Run(
        RunId("run-1"),
        _revision_hash(),
        RunState.STARTED,
        "agent",
        0,
        0,
    )


def _run_v2() -> RunV2:
    return RunV2(
        RunId("run-1"),
        _revision_hash(),
        AgentBindingSet(()).binding_set_hash,
        (),
        RunState.STARTED,
        "agent",
        0,
        0,
    )


def test_the_run_head_owner_accepts_an_open_head_and_a_completed_head() -> None:
    Run.validate_head("agent", RunState.STARTED, 0, 0, None)
    Run.validate_head("done", RunState.COMPLETED, 2, 4, _terminal_hash())
    Run.validate_head("agent", RunState.FAILED, 2, 1, _terminal_hash())


@pytest.mark.parametrize(
    (
        "current_node_id",
        "state",
        "state_version",
        "last_event_sequence",
        "terminal_hash",
        "match",
    ),
    (
        ("", RunState.STARTED, 0, 0, None, "current_node_id"),
        ("agent", RunState.STARTED, -1, 0, None, "nonnegative"),
        ("agent", RunState.STARTED, 0, -1, None, "nonnegative"),
        ("agent", RunState.COMPLETED, 2, 4, None, "terminal hash"),
        ("agent", RunState.STARTED, 0, 0, _terminal_hash(), "terminal hash"),
    ),
    ids=(
        "empty current node",
        "negative state version",
        "negative event sequence",
        "completed without terminal hash",
        "open head with terminal hash",
    ),
)
def test_the_run_head_owner_refuses_every_noncanonical_head(
    current_node_id: str,
    state: RunState,
    state_version: int,
    last_event_sequence: int,
    terminal_hash: Sha256Hash | None,
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        Run.validate_head(
            current_node_id,
            state,
            state_version,
            last_event_sequence,
            terminal_hash,
        )


@pytest.mark.parametrize("build", (_run, _run_v2), ids=("v1", "v2"))
def test_both_run_formats_accept_the_same_completed_head(
    build: Callable[[], Run | RunV2],
) -> None:
    completed = replace(
        build(),
        state=RunState.COMPLETED,
        state_version=2,
        last_event_sequence=4,
        terminal_hash=_terminal_hash(),
    )

    assert completed.state is RunState.COMPLETED
    assert completed.terminal_hash == _terminal_hash()


@pytest.mark.parametrize("build", (_run, _run_v2), ids=("v1", "v2"))
@pytest.mark.parametrize(
    ("mutate", "match"),
    (
        (lambda run: replace(run, current_node_id=""), "current_node_id"),
        (lambda run: replace(run, state_version=-1), "nonnegative"),
        (lambda run: replace(run, last_event_sequence=-1), "nonnegative"),
        (lambda run: replace(run, state=RunState.COMPLETED), "terminal hash"),
        (lambda run: replace(run, terminal_hash=_terminal_hash()), "terminal hash"),
    ),
    ids=(
        "empty current node",
        "negative state version",
        "negative event sequence",
        "completed without terminal hash",
        "open head with terminal hash",
    ),
)
def test_both_run_formats_refuse_the_same_noncanonical_head(
    build: Callable[[], Run | RunV2],
    mutate: Callable[[Run | RunV2], Run | RunV2],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        mutate(build())
