from __future__ import annotations

from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
)
from atelier2.contracts.agents import AgentExecutionRequestV2
from atelier2.ports.agent_attempts import (
    AgentAttemptClaimedByThisCall,
    AgentAttemptExecutionOutcome,
    AgentAttemptStore,
)
from atelier2.ports.agent_executions import AgentExecutionFailure, AgentExecutorV2


def execute_agent_attempt(
    request: AgentExecutionRequestV2,
    executor: AgentExecutorV2,
    store: AgentAttemptStore,
) -> AgentAttemptExecutionOutcome:
    """Invoke only after this live call durably wins the launch boundary."""

    store.prepare(request)
    claim = store.claim(request)
    if not isinstance(claim, AgentAttemptClaimedByThisCall):
        return claim
    result = executor.execute(request)
    if isinstance(result, AgentExecutionFailure):
        if result.code is not AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY:
            raise ValueError("executor returned an unsupported known failure")
        return store.complete_known_failure(request)
    return store.complete_success(request, result)
