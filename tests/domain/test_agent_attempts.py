from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
    AgentAttempt,
    AgentAttemptCancellation,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
    AgentProcessOwnerId,
    WatchdogGenerationId,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    AgentExecutionRequestHash,
    AgentExecutorOperationalIdentity,
    AgentReceiptHash,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import RunId, WorkflowRevisionHash


def _attempt(state: AgentAttemptState = AgentAttemptState.PREPARED) -> AgentAttempt:
    execution_id = NodeExecutionId("0" * 64)
    request_hash = AgentExecutionRequestHash("1" * 64)
    return AgentAttempt(
        AgentAttemptId.for_execution(execution_id, request_hash),
        execution_id,
        request_hash,
        AgentExecutorOperationalIdentity("controlled-executor"),
        RunId("run-17"),
        WorkflowRevisionHash("2" * 64),
        "builder",
        AGENT_ATTEMPT_ORDINAL,
        state,
        0 if state is AgentAttemptState.PREPARED else 1,
    )


def test_attempt_id_has_fixed_canonical_vector() -> None:
    attempt_id = AgentAttemptId.for_execution(
        NodeExecutionId("0" * 64),
        AgentExecutionRequestHash("1" * 64),
    )
    assert (
        attempt_id.value
        == "6ea67ad4ac9b01be7a7eddef32e44a8b3bd7391fe69f89eb8407bd05a7dc1129"
    )


def test_attempt_identity_accepts_exactly_two_ordinals() -> None:
    execution_id = NodeExecutionId("0" * 64)
    request_hash = AgentExecutionRequestHash("1" * 64)

    first = AgentAttemptId.for_execution(execution_id, request_hash, 1)
    replacement = AgentAttemptId.for_execution(execution_id, request_hash, 2)

    assert first != replacement
    assert AGENT_ATTEMPT_ORDINAL == 1
    assert REPLACEMENT_AGENT_ATTEMPT_ORDINAL == 2
    for invalid in (0, 3, True):
        with pytest.raises(ValueError, match="ordinal"):
            AgentAttemptId.for_execution(execution_id, request_hash, invalid)


@pytest.mark.parametrize(
    "build",
    (
        AgentProcessOwnerId,
        WatchdogGenerationId,
        lambda value: AgentAttemptCancellation(value, 1, AgentAttemptReplacement.NONE),
    ),
    ids=("process owner id", "watchdog generation id", "cancellation command id"),
)
def test_attempt_text_fields_end_at_the_agent_field_bound(
    build: Callable[[str], object],
) -> None:
    build("x" * MAXIMUM_AGENT_FIELD_CHARACTERS)

    with pytest.raises(ValueError, match=str(MAXIMUM_AGENT_FIELD_CHARACTERS)):
        build("x" * (MAXIMUM_AGENT_FIELD_CHARACTERS + 1))


def test_cancellation_contract_has_one_closed_canonical_terminal_shape() -> None:
    cancellation = AgentAttemptCancellation(
        command_id="cancel-17",
        expected_attempt_state_version=1,
        replacement=AgentAttemptReplacement.ONE,
        redrive_state=AgentAttemptRedriveState.CLEANUP_ATTESTED,
        disposition=AgentAttemptCancellationDisposition.REAPED_AFTER_TERM,
    )
    armed = replace(
        _attempt(AgentAttemptState.LAUNCH_ARMED),
        process_phase=AgentAttemptProcessPhase.LAUNCH_AUTHORIZED,
        process_owner_id=AgentProcessOwnerId("owner-17"),
        watchdog_generation_id=WatchdogGenerationId("generation-17"),
    )
    cancelled = replace(
        armed,
        state=AgentAttemptState.CANCELLED,
        state_version=3,
        process_phase=AgentAttemptProcessPhase.CLEANUP_ATTESTED,
        cancellation=cancellation,
    )

    assert cancelled.cancellation == cancellation
    assert cancelled.state is AgentAttemptState.CANCELLED

    with pytest.raises(ValueError, match="cancellation"):
        replace(cancelled, cancellation=None)


def test_attempt_contract_accepts_only_the_four_exact_state_shapes() -> None:
    prepared = _attempt()
    armed = replace(prepared, state=AgentAttemptState.LAUNCH_ARMED, state_version=1)
    succeeded = replace(
        armed,
        state=AgentAttemptState.SUCCEEDED,
        state_version=2,
        receipt_hash=AgentReceiptHash("3" * 64),
    )
    failed = replace(
        armed,
        state=AgentAttemptState.FAILED,
        state_version=2,
        failure_code=AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
    )

    assert (prepared.state, armed.state, succeeded.state, failed.state) == (
        AgentAttemptState.PREPARED,
        AgentAttemptState.LAUNCH_ARMED,
        AgentAttemptState.SUCCEEDED,
        AgentAttemptState.FAILED,
    )


@pytest.mark.parametrize(
    "mutation",
    (
        lambda value: replace(value, attempt_ordinal=2),
        lambda value: replace(value, state_version=1),
        lambda value: replace(
            value,
            failure_code=AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        ),
        lambda value: replace(value, receipt_hash=AgentReceiptHash("3" * 64)),
        lambda value: replace(
            value,
            state=AgentAttemptState.LAUNCH_ARMED,
            state_version=2,
        ),
        lambda value: replace(
            value,
            state=AgentAttemptState.SUCCEEDED,
            state_version=2,
        ),
        lambda value: replace(
            value,
            state=AgentAttemptState.FAILED,
            state_version=2,
        ),
    ),
)
def test_attempt_contract_rejects_every_noncanonical_state_shape(
    mutation: Callable[[AgentAttempt], AgentAttempt],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        mutation(_attempt())
