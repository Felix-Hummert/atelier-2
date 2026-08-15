from __future__ import annotations

import pytest

from atelier2.contracts.agent_attempts import AgentAttemptState
from atelier2.contracts.run_projections import (
    PublicAgentAttemptState,
    public_agent_attempt_state,
)


@pytest.mark.parametrize("durable_state", sorted(AgentAttemptState))
def test_every_durable_attempt_state_has_a_decided_public_name(
    durable_state: AgentAttemptState,
) -> None:
    public_state = public_agent_attempt_state(durable_state)

    assert public_state is None or isinstance(public_state, PublicAgentAttemptState)


def test_only_a_successful_attempt_is_absent_from_the_public_vocabulary() -> None:
    unprojectable = {
        durable_state
        for durable_state in AgentAttemptState
        if public_agent_attempt_state(durable_state) is None
    }

    assert unprojectable == {AgentAttemptState.SUCCEEDED}


def test_the_public_vocabulary_is_exactly_what_the_durable_states_project_to() -> None:
    projected = {
        public_agent_attempt_state(durable_state) for durable_state in AgentAttemptState
    }

    assert projected - {None} == set(PublicAgentAttemptState)


def test_an_armed_launch_is_told_as_a_run_that_possibly_ran() -> None:
    assert (
        public_agent_attempt_state(AgentAttemptState.LAUNCH_ARMED)
        is PublicAgentAttemptState.POSSIBLY_RAN
    )
