from __future__ import annotations

from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
    AgentAttemptState,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.ports.agent_attempts import (
    AgentAttemptClaimedByThisCall,
    AgentAttemptExecutionOutcome,
    AgentAttemptFailed,
    AgentAttemptPossiblyRan,
    AgentAttemptStore,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentExecutorV2,
    AgentProcessExited,
    AgentProcessOutputLimitExceeded,
    AgentProcessRunner,
    AgentProcessStopped,
    AgentProcessSupervisionFailed,
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
    supervisor.prepare(execution, invocation)
    claim = store.claim(execution)
    should_join_local_attempt = isinstance(claim, AgentAttemptPossiblyRan) and (
        claim.attempt.state
        in {AgentAttemptState.LAUNCH_ARMED, AgentAttemptState.CANCEL_REQUESTED}
    )
    if not isinstance(claim, AgentAttemptClaimedByThisCall) and not (
        should_join_local_attempt
    ):
        if isinstance(claim, (AgentAttemptSucceeded, AgentAttemptFailed)):
            supervisor.finalize(execution)
        return claim
    process_outcome = supervisor.launch_and_wait(execution)
    if isinstance(
        process_outcome,
        (AgentAttemptSucceeded, AgentAttemptFailed, AgentAttemptPossiblyRan),
    ):
        return process_outcome
    if isinstance(process_outcome, AgentProcessOutputLimitExceeded):
        outcome = store.complete_known_failure(
            execution, AgentAttemptFailureCode.PROCESS_OUTPUT_LIMIT_EXCEEDED
        )
    elif isinstance(process_outcome, AgentProcessSupervisionFailed):
        outcome = store.complete_known_failure(
            execution, AgentAttemptFailureCode.PROCESS_SUPERVISION_FAILED
        )
    elif isinstance(process_outcome, AgentProcessStopped):
        return store.observe_process_stopped(execution)
    else:
        assert isinstance(process_outcome, AgentProcessExited)
        result = executor.decode_process_completion(process_outcome)
        if isinstance(result, AgentExecutionFailure):
            if result.code is not AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY:
                raise ValueError("executor returned an unsupported known failure")
            outcome = store.complete_known_failure(execution, result.code)
        else:
            outcome = store.complete_success(execution, result)
    if isinstance(outcome, (AgentAttemptSucceeded, AgentAttemptFailed)):
        supervisor.finalize(execution)
    return outcome
