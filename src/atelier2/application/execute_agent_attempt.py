from __future__ import annotations

import logging

from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
    ProcessExitSignature,
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
from atelier2.ports.project_verification import PinnedProjectSource

_LOG = logging.getLogger("atelier2")


def execute_agent_attempt(
    execution: AgentAttemptExecution,
    executor: AgentExecutorV2,
    store: AgentAttemptStore,
    supervisor: AgentProcessRunner,
    workspaces: AgentAttemptWorkspaceOwner,
    project: PinnedProjectSource | None = None,
) -> AgentAttemptExecutionOutcome:
    """Invoke only after this live call durably wins the launch boundary.

    Preparing the provider command and attesting the scratch root are pure, so
    a call that loses the claim leaves no workspace behind: the directory is
    created only once this call's own compare-and-set has won. It is removed
    only once both facts that make removal safe are in hand -- the completion
    proving no process or descendant of this attempt is left, and the durable
    terminal attempt.

    `project` is absent where this runtime was pointed at no project. Where one
    is pinned, the tree that commit names is unpacked into the leased directory
    between the claim and the provider, so the work happens on exactly the
    material the durable binding pinned rather than on whatever a checkout holds
    now -- and a pin the source can no longer answer for refuses beside the
    scratch root, before any claim.

    A node that pinned a tool grant has it redeemed here, in that same lease
    after the provider produced the work the verification is about. What the
    project declares is read at the pin and attested before the claim too, so a
    project that declares no verification refuses before anything runs.
    """

    store.prepare(execution)
    command = executor.prepare_process(execution.request)
    workspaces.preflight()
    if project is not None:
        project.source.attest(project.pin)
        if project.grant is not None:
            project.verifications.preflight(project.pin)
    try:
        supervisor.prepare(execution)
        claim = store.claim(execution)
        if not isinstance(claim, AgentAttemptClaimedByThisCall):
            if isinstance(claim, (AgentAttemptSucceeded, AgentAttemptFailed)):
                supervisor.finalize(execution)
            return claim
        lease = workspaces.acquire(execution.attempt_id)
        if project is not None:
            project.source.materialize(project.pin, lease)
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
            # The completion, not the executor's verdict, carries how the child
            # ended: an executor answers whether it could use the process, and
            # only supervision saw the exit code and the standard error that
            # says why. Composing the durable naming here keeps that one reading
            # of one process, rather than asking every provider to repeat it.
            outcome = store.complete_known_failure(
                execution,
                ProcessExitSignature(completion.return_code, completion.standard_error),
            )
        else:
            outcome = store.complete_success(
                execution, result, _redeemed(execution, lease, project)
            )
            if isinstance(outcome, AgentAttemptFailed):
                failure = outcome.attempt.failure_code
                match failure:
                    case AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED:
                        event = "agent_attempt_output_refused"
                        detail = (
                            "produced an output its own schema refuses; "
                            "the refusal is durably named."
                        )
                    case AgentAttemptFailureCode.AGENT_REFUSED:
                        event = "agent_attempt_refused"
                        detail = "declared a refusal; the refusal is durably named."
                    case AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED:
                        event = "agent_attempt_project_verification_failed"
                        detail = (
                            "project verification ended unsuccessfully; "
                            "the failure is durably named."
                        )
                    case _:
                        raise ValueError("complete_success returned an unnamed failure")
                _LOG.warning(
                    "Agent attempt %s on node %s of run %s %s",
                    execution.attempt_id.value,
                    execution.request.node_id,
                    execution.request.run_id.value,
                    detail,
                    extra={
                        "event": event,
                        "run_id": execution.request.run_id.value,
                        "node_id": execution.request.node_id,
                        "attempt_id": execution.attempt_id.value,
                    },
                )
        supervisor.finalize(execution)
        workspaces.release(execution.attempt_id)
    finally:
        executor.release_credential_channel(command)
    return outcome


def _redeemed(
    execution: AgentAttemptExecution,
    lease: AgentAttemptWorkspaceLease,
    project: PinnedProjectSource | None,
) -> ToolRedemptionReceipt | None:
    """What redeeming this node's grant ran, or nothing where no grant was pinned.

    The verification runs before the attempt is durably terminal, so its evidence
    reaches the store in the same write that keeps the provider's own receipt: a
    redemption durably missing beside a succeeded attempt would say a grant was
    never redeemed, which is exactly the thing this evidence exists to answer.
    """
    if project is None or project.grant is None:
        return None
    request = execution.request
    outcome = project.verifications.run(project.pin, lease)
    return ToolRedemptionReceipt.of(
        request.node_execution_id,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        execution.attempt_id,
        project.grant,
        outcome.command,
        outcome.exit_code,
        outcome.standard_output_hash,
    )
