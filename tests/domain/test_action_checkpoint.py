"""Recorded action-checkpoint/0 outputs are read by arity, not by a new step name."""

from __future__ import annotations

import pytest

from atelier2.adapters.dbos.continuation import decode_action_checkpoint
from atelier2.contracts.runs import FIRST_ROUND_ORDINAL, RunState


def test_a_two_tuple_replay_does_not_indexerror_and_yields_started() -> None:
    run_id = "run-1"
    successor = "waiting"
    assert decode_action_checkpoint((run_id, successor)) == (
        run_id,
        successor,
        FIRST_ROUND_ORDINAL,
        RunState.STARTED.value,
    )


def test_a_three_tuple_replay_keeps_the_recorded_state() -> None:
    run_id = "run-1"
    head = "action"
    state = RunState.COMPLETED.value
    assert decode_action_checkpoint((run_id, head, state)) == (
        run_id,
        head,
        FIRST_ROUND_ORDINAL,
        state,
    )


def test_a_current_checkpoint_preserves_the_successor_round() -> None:
    assert decode_action_checkpoint(("run-1", "implement", 2, "STARTED")) == (
        "run-1",
        "implement",
        2,
        "STARTED",
    )


@pytest.mark.parametrize(
    "recorded",
    ((), ("run-1",), ("run-1", "waiting", "head", "extra"), "run-1", None),
)
def test_any_other_recorded_checkpoint_fails_loud(recorded: object) -> None:
    with pytest.raises((TypeError, ValueError), match="action checkpoint"):
        decode_action_checkpoint(recorded)
