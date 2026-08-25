from __future__ import annotations

import logging

from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
    ProcessExitSignature,
)
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.tool_grants_v3 import (
    ToolGrantCapability,
    ToolGrantCapabilityNotRedeemed,
    ToolRedemptionReceipt,
)
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
from atelier2.ports.project_verification import (
    PinnedProjectSource,
    ProjectVerificationOutcome,
    ProjectVerificationUnavailable,
)

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
    project that declares no verification refuses before anything runs. A
    verification that does not answer within its declared deadline after the
    claim ends the attempt `FAILED` under `PROJECT_VERIFICATION_FAILED` rather
    than leaving it `LAUNCH_ARMED`.
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
            # What the process itself wrote is the other half, and only the
            # executor can read it, so it travels on the failure it returned.
            outcome = store.complete_known_failure(
                execution,
                ProcessExitSignature(completion.return_code, completion.standard_error),
                result.transcript,
            )
        else:
            try:
                redemption = _redeemed(execution, lease, project)
            except ProjectVerificationUnavailable as error:
                # The claim already won; letting this escape leaves the attempt
                # LAUNCH_ARMED, and a replay would report AgentAttemptPossiblyRan.
                outcome = store.complete_project_verification_failure(
                    execution, _verification_unavailable_verdict(error)
                )
            else:
                outcome = store.complete_success(execution, result, redemption)
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
                        raise ValueError("attempt ended under an unnamed failure")
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


def _verification_unavailable_verdict(error: ProjectVerificationUnavailable) -> str:
    """How the granted check failed to complete, without inventing an exit code."""

    if error.timeout_seconds is None:
        return str(error)
    return f"timeout {error.timeout_seconds} seconds"


def _redeemed(
    execution: AgentAttemptExecution,
    lease: AgentAttemptWorkspaceLease,
    project: PinnedProjectSource | None,
) -> ToolRedemptionReceipt | None:
    """What redeeming this node's grant ran, or nothing where no grant was pinned.

    Which redeemer answers is read from the capability the pinned grant
    actually names, not assumed: a grant is redeemed by the one redeemer its
    own capability reaches, never by whichever redeemer this runtime happens
    to carry. The verification runs before the attempt is durably terminal, so
    its evidence reaches the store in the same write that keeps the provider's
    own receipt: a redemption durably missing beside a succeeded attempt would
    say a grant was never redeemed, which is exactly the thing this evidence
    exists to answer.
    """
    if project is None or project.grant is None:
        return None
    grant = project.grant
    outcome = _redeemed_via_capability(grant.capability, project, lease)
    request = execution.request
    return ToolRedemptionReceipt.of(
        request.node_execution_id,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        execution.attempt_id,
        grant,
        outcome.command,
        outcome.exit_code,
        outcome.standard_output_hash,
    )


def _redeemed_via_capability(
    capability: ToolGrantCapability,
    project: PinnedProjectSource,
    lease: AgentAttemptWorkspaceLease,
) -> ProjectVerificationOutcome:
    """The one redeemer this exact capability reaches, run against this lease.

    `ToolGrantCapability` holds one EXEC-shaped member, so this dispatch has one
    case -- but the case is still asked for, rather than assumed, because the
    bug this dispatch exists to keep impossible is exactly a capability that
    used to reach this point unread and got redeemed as a project verification
    regardless of what it actually named. A capability this match does not
    recognize is refused by name instead: only an exec-shaped grant is bound
    into the lease this redeemer runs against -- the effect-shaped `open-pr` is
    redeemed as a platform effect elsewhere -- so a capability reaching this
    branch that this redeemer does not perform is a defect in the runtime that
    bound it, not a condition an attempt can recover from.
    """
    match capability:
        case ToolGrantCapability.RUN_PROJECT_VERIFICATION:
            return project.verifications.run(project.pin, lease)
        case _:
            raise ToolGrantCapabilityNotRedeemed(capability)
