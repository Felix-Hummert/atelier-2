from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

import pytest

from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    AgentAttempt,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptState,
)
from atelier2.contracts.agents import (
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
