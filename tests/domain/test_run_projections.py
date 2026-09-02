from __future__ import annotations

import pytest

from atelier2.contracts.agent_attempts import AgentAttemptState
from atelier2.contracts.effects import (
    AdapterOperationalIdentity,
    AdapterRevision,
    CanonicalRequest,
    EffectBinding,
    EffectDestination,
    EffectIntent,
    EffectIntentSnapshot,
    EffectIntentState,
    EffectIntentStateVersion,
    LogicalEffectKey,
)
from atelier2.contracts.run_projections import (
    NodeState,
    PublicAgentAttemptState,
    WaitingReconciliationProjection,
    public_agent_attempt_state,
    run_awaits_effect_reconciliation,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash


def waiting_reconciliation(state: EffectIntentState) -> WaitingReconciliationProjection:
    """One run's effect, standing where the durable store says it stands."""
    intent = EffectIntent(
        EffectBinding(
            LogicalEffectKey("run-projection/effect"),
            RunId("run-projection"),
            WorkflowRevisionHash("a" * 64),
            AdapterRevision("run-projection-adapter"),
            EffectDestination("run-projection-destination"),
            AdapterOperationalIdentity("run-projection-operation"),
        ),
        CanonicalRequest(b"request"),
    )
    return WaitingReconciliationProjection(
        EffectIntentSnapshot(intent, state, EffectIntentStateVersion(1)), None
    )


@pytest.mark.proves("a-node-ending-in-success-has-exactly-one-name")
def test_a_node_ending_in_success_has_exactly_one_name() -> None:
    assert {state.value for state in NodeState} == {
        "queued",
        "working",
        "needs_you",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
    }


@pytest.mark.parametrize("effect_awaits_reconciliation", (False, True))
@pytest.mark.parametrize("durable_state", sorted(AgentAttemptState))
def test_every_durable_attempt_state_has_a_decided_public_name(
    durable_state: AgentAttemptState, effect_awaits_reconciliation: bool
) -> None:
    public_state = public_agent_attempt_state(
        durable_state, effect_awaits_reconciliation=effect_awaits_reconciliation
    )

    assert public_state is None or isinstance(public_state, PublicAgentAttemptState)


def test_a_success_no_effect_waits_on_is_absent_from_the_public_vocabulary() -> None:
    unprojectable = {
        durable_state
        for durable_state in AgentAttemptState
        if public_agent_attempt_state(durable_state, effect_awaits_reconciliation=False)
        is None
    }

    assert unprojectable == {AgentAttemptState.SUCCEEDED}


def test_a_success_whose_effect_awaits_the_operator_is_told_as_succeeded() -> None:
    assert (
        public_agent_attempt_state(
            AgentAttemptState.SUCCEEDED, effect_awaits_reconciliation=True
        )
        is PublicAgentAttemptState.SUCCEEDED
    )


@pytest.mark.parametrize(
    "durable_state", sorted(set(AgentAttemptState) - {AgentAttemptState.SUCCEEDED})
)
def test_a_waiting_effect_renames_no_attempt_but_the_succeeded_one(
    durable_state: AgentAttemptState,
) -> None:
    assert public_agent_attempt_state(
        durable_state, effect_awaits_reconciliation=True
    ) is public_agent_attempt_state(durable_state, effect_awaits_reconciliation=False)


def test_the_public_vocabulary_is_exactly_what_the_durable_states_project_to() -> None:
    projected = {
        public_agent_attempt_state(durable_state, effect_awaits_reconciliation=awaiting)
        for durable_state in AgentAttemptState
        for awaiting in (False, True)
    }

    assert projected - {None} == set(PublicAgentAttemptState)


def test_an_armed_launch_is_told_as_a_run_that_possibly_ran() -> None:
    assert (
        public_agent_attempt_state(
            AgentAttemptState.LAUNCH_ARMED, effect_awaits_reconciliation=False
        )
        is PublicAgentAttemptState.POSSIBLY_RAN
    )


@pytest.mark.parametrize(
    ("run_state", "intent_state", "awaits"),
    (
        (
            RunState.WAITING_RECONCILIATION,
            EffectIntentState.WAITING_RECONCILIATION,
            True,
        ),
        (RunState.WAITING_RECONCILIATION, EffectIntentState.RECONCILING, True),
        (RunState.WAITING_RECONCILIATION, EffectIntentState.ABANDONED, False),
        (RunState.STARTED, EffectIntentState.WAITING_RECONCILIATION, False),
        (RunState.COMPLETED, EffectIntentState.ABANDONED, False),
    ),
)
def test_a_run_awaits_its_effect_only_while_it_parks_on_an_unresolved_intent(
    run_state: RunState, intent_state: EffectIntentState, awaits: bool
) -> None:
    assert (
        run_awaits_effect_reconciliation(
            run_state, waiting_reconciliation(intent_state)
        )
        is awaits
    )


def test_a_run_with_no_intent_of_its_own_awaits_no_effect() -> None:
    assert (
        run_awaits_effect_reconciliation(RunState.WAITING_RECONCILIATION, None) is False
    )
