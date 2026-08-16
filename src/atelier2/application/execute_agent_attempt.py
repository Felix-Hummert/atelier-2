from __future__ import annotations

from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.ports.agent_attempts import (
    AgentAttemptClaimedByThisCall,
    AgentAttemptExecutionOutcome,
    AgentAttemptFailed,
    AgentAttemptStore,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentExecutorV2,
    AgentProcessRunner,
)


def execute_agent_attempt(
    execution: AgentAttemptExecution,
    executor: AgentExecutorV2,
    store: AgentAttemptStore,
    supervisor: AgentProcessRunner,
) -> AgentAttemptExecutionOutcome:
    """Invoke only after this live call durably wins the launch boundary."""

    store.prepare(execution)
    invocation = executor.prepare_process(execution.request)
    supervisor.prepare(execution)
    claim = store.claim(execution)
    if not isinstance(claim, AgentAttemptClaimedByThisCall):
        if isinstance(claim, (AgentAttemptSucceeded, AgentAttemptFailed)):
            supervisor.finalize(execution)
        return claim
    completion = supervisor.launch_and_wait(execution, invocation)
    result = executor.decode_process_completion(invocation, completion)
    if isinstance(result, AgentExecutionFailure):
        if result.code is not AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY:
            raise ValueError("executor returned an unsupported known failure")
        outcome = store.complete_known_failure(execution)
    else:
        outcome = store.complete_success(execution, result)
    supervisor.finalize(execution)
    return outcome
