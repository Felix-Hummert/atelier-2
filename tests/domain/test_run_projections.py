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
)
from atelier2.contracts.executions import NodeExecutionId, logical_effect_key_for
from atelier2.contracts.run_projections import (
    MAXIMUM_RUN_ROW_DEFECT_DETAIL_CHARACTERS,
    NodeState,
    PublicAgentAttemptState,
    WaitingReconciliationProjection,
    bounded_run_row_defect_detail,
    execution_awaits_effect_reconciliation,
    public_agent_attempt_state,
)
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash

RUN = RunId("run-projection")
REVISION = WorkflowRevisionHash("a" * 64)
PARKED_NODE = "the-node-holding-the-effect"
ANOTHER_NODE = "the-node-the-run-already-left"


def node_execution(node_id: str) -> NodeExecutionId:
    return NodeExecutionId.for_node(RUN, REVISION, node_id)


def waiting_reconciliation(state: EffectIntentState) -> WaitingReconciliationProjection:
    """The parked node's own effect, standing where the durable store says it does."""
    intent = EffectIntent(
        EffectBinding(
            logical_effect_key_for(node_execution(PARKED_NODE)),
            RUN,
            REVISION,
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
    ("run_state", "intent_state", "node_id", "awaits"),
    (
        (
            RunState.WAITING_RECONCILIATION,
            EffectIntentState.WAITING_RECONCILIATION,
            PARKED_NODE,
            True,
        ),
        (
            RunState.WAITING_RECONCILIATION,
            EffectIntentState.RECONCILING,
            PARKED_NODE,
            True,
        ),
        (
            RunState.WAITING_RECONCILIATION,
            EffectIntentState.ABANDONED,
            PARKED_NODE,
            False,
        ),
        (
            RunState.WAITING_RECONCILIATION,
            EffectIntentState.WAITING_RECONCILIATION,
            ANOTHER_NODE,
            False,
        ),
        (
            RunState.STARTED,
            EffectIntentState.WAITING_RECONCILIATION,
            PARKED_NODE,
            False,
        ),
        (RunState.COMPLETED, EffectIntentState.ABANDONED, PARKED_NODE, False),
    ),
)
def test_only_the_execution_the_waiting_intent_names_awaits_its_effect(
    run_state: RunState, intent_state: EffectIntentState, node_id: str, awaits: bool
) -> None:
    assert (
        execution_awaits_effect_reconciliation(
            run_state, waiting_reconciliation(intent_state), node_execution(node_id)
        )
        is awaits
    )


def test_an_execution_with_no_intent_of_its_own_awaits_no_effect() -> None:
    assert (
        execution_awaits_effect_reconciliation(
            RunState.WAITING_RECONCILIATION, None, node_execution(PARKED_NODE)
        )
        is False
    )


def test_a_defective_run_row_detail_names_the_failure_class_not_its_message() -> None:
    """A store exception's own message never reaches a defective row (#1042).

    `DatabaseError` embeds the failing SQL and its bound parameters; only the
    exception's class -- what family of failure this was -- is curated onto
    the row.
    """
    error = RuntimeError("SELECT * FROM runs WHERE run_id = 'super-secret-id'")

    assert bounded_run_row_defect_detail(error) == "RuntimeError"


def test_a_defective_run_row_detail_is_bounded_even_for_an_overlong_class_name() -> (
    None
):
    overlong = type(
        "X" * (MAXIMUM_RUN_ROW_DEFECT_DETAIL_CHARACTERS + 100), (Exception,), {}
    )

    detail = bounded_run_row_defect_detail(overlong())

    assert len(detail) == MAXIMUM_RUN_ROW_DEFECT_DETAIL_CHARACTERS


def test_a_defective_run_row_detail_falls_back_when_the_class_name_is_empty() -> None:
    unnamed = type("", (Exception,), {})

    detail = bounded_run_row_defect_detail(unnamed())

    assert detail != ""
