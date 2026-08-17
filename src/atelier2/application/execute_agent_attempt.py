from __future__ import annotations

import logging

from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.tool_grants_v3 import ToolRedemptionReceipt
from atelier2.ports.agent_attempts import (
    AgentAttemptClaimedByThisCall,
    AgentAttemptExecutionOutcome,
    AgentAttemptFailed,
    AgentAttemptStore,
    AgentAttemptSucceeded,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentAttemptWorkspaceOwner,
    AgentExecutionFailure,
    AgentExecutorV2,
    AgentProcessInvocation,
    AgentProcessRunner,
)
from atelier2.ports.project_verification import ToolGrantRedemption

_LOG = logging.getLogger("atelier2")


def execute_agent_attempt(
    execution: AgentAttemptExecution,
    executor: AgentExecutorV2,
    store: AgentAttemptStore,
    supervisor: AgentProcessRunner,
    workspaces: AgentAttemptWorkspaceOwner,
    redemption: ToolGrantRedemption | None = None,
) -> AgentAttemptExecutionOutcome:
    """Invoke only after this live call durably wins the launch boundary.

    Preparing the provider command and attesting the scratch root are pure, so
    a call that loses the claim leaves no workspace behind: the directory is
    created only once this call's own compare-and-set has won. It is removed
    only once both facts that make removal safe are in hand -- the completion
    proving no process or descendant of this attempt is left, and the durable
    terminal attempt.

    `redemption` is absent for a node that pinned no tool grant. A node that
    pinned one has its grant redeemed here, in the directory this attempt owns,
    after the provider produced the work the verification is about -- and the
    verification the project declares is attested before the claim, beside the
    scratch root, so a project that declares none refuses before anything runs.
    """

    store.prepare(execution)
    command = executor.prepare_process(execution.request)
    workspaces.preflight()
    if redemption is not None:
        redemption.verifications.preflight()
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
            _LOG.warning(
                "Agent attempt %s on node %s of run %s failed.",
                execution.attempt_id.value,
                execution.request.node_id,
                execution.request.run_id.value,
                extra={
                    "event": "agent_attempt_failed",
                    "run_id": execution.request.run_id.value,
                    "node_id": execution.request.node_id,
                    "attempt_id": execution.attempt_id.value,
                },
            )
            outcome = store.complete_known_failure(execution)
        else:
            outcome = store.complete_success(
                execution, result, _redeemed(execution, lease, redemption)
            )
        supervisor.finalize(execution)
        workspaces.release(execution.attempt_id)
    finally:
        executor.release_credential_channel(command)
    return outcome


def _redeemed(
    execution: AgentAttemptExecution,
    lease: AgentAttemptWorkspaceLease,
    redemption: ToolGrantRedemption | None,
) -> ToolRedemptionReceipt | None:
    """What redeeming this node's grant ran, or nothing where no grant was pinned.

    The verification runs before the attempt is durably terminal, so its evidence
    reaches the store in the same write that keeps the provider's own receipt: a
    redemption durably missing beside a succeeded attempt would say a grant was
    never redeemed, which is exactly the thing this evidence exists to answer.
    """
    if redemption is None:
        return None
    request = execution.request
    outcome = redemption.verifications.run(lease)
    return ToolRedemptionReceipt.of(
        request.node_execution_id,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        execution.attempt_id,
        redemption.grant,
        outcome.command,
        outcome.exit_code,
        outcome.standard_output_hash,
    )
