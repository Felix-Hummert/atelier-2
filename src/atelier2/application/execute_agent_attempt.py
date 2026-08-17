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
    AgentAttemptWorkspaceOwner,
    AgentExecutionFailure,
    AgentExecutorV2,
    AgentProcessInvocation,
    AgentProcessRunner,
)


def execute_agent_attempt(
    execution: AgentAttemptExecution,
    executor: AgentExecutorV2,
    store: AgentAttemptStore,
    supervisor: AgentProcessRunner,
    workspaces: AgentAttemptWorkspaceOwner,
) -> AgentAttemptExecutionOutcome:
    """Invoke only after this live call durably wins the launch boundary.

    Preparing the provider command and attesting the scratch root are pure, so
    a call that loses the claim leaves no workspace behind: the directory is
    created only once this call's own compare-and-set has won. It is removed
    only once both facts that make removal safe are in hand -- the completion
    proving no process or descendant of this attempt is left, and the durable
    terminal attempt.
    """

    store.prepare(execution)
    command = executor.prepare_process(execution.request)
    workspaces.preflight()
    try:
        supervisor.prepare(execution)
        claim = store.claim(execution)
        if not isinstance(claim, AgentAttemptClaimedByThisCall):
            if isinstance(claim, (AgentAttemptSucceeded, AgentAttemptFailed)):
                supervisor.finalize(execution)
            return claim
        lease = workspaces.acquire(execution.attempt_id)
        invocation = AgentProcessInvocation(command, lease)
        completion = supervisor.launch_and_wait(execution, invocation)
        result = executor.decode_process_completion(invocation, completion)
        if isinstance(result, AgentExecutionFailure):
            if result.code is not AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY:
                raise ValueError("executor returned an unsupported known failure")
            outcome = store.complete_known_failure(execution)
        else:
            outcome = store.complete_success(execution, result)
        supervisor.finalize(execution)
        workspaces.release(execution.attempt_id)
    finally:
        executor.release_credential_channel(command)
    return outcome
