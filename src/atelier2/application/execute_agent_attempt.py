from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

from atelier2.application.publish_artifact import (
    ArtifactPublicationCreated,
    ArtifactPublicationExisting,
    ArtifactPublicationInvalid,
    publish_artifact,
)
from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.agent_attempts import (
    AgentAttemptFailureCode,
    ProcessExitSignature,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.secret_redaction import redact_credentials
from atelier2.contracts.tool_grants_v3 import (
    ToolGrantCapability,
    ToolGrantCapabilityNotRedeemed,
    ToolRedemptionReceipt,
)
from atelier2.contracts.when import RecordedAt, recorded_instant
from atelier2.ports.agent_attempts import (
    AgentAttemptClaimedByThisCall,
    AgentAttemptExecutionOutcome,
    AgentAttemptFailed,
    AgentAttemptStore,
    AgentAttemptSucceeded,
    ProjectVerificationFailureEvidence,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentAttemptWorkspaceOwner,
    AgentExecutionFailure,
    AgentExecutionPreflightRefusal,
    AgentExecutorV2,
    AgentProcessInvocation,
    AgentProcessRunner,
)
from atelier2.ports.artifacts import ArtifactPublisher
from atelier2.ports.candidate_store import CandidateNotKept
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
    artifacts: ArtifactPublisher | None = None,
    clock: Callable[[], RecordedAt] = recorded_instant,
) -> AgentAttemptExecutionOutcome:
    """Invoke only after this live call durably wins the launch boundary.

    Preparing the provider command and attesting the scratch root happen before
    the claim, so a call that loses it leaves no workspace behind: the directory
    is created only once this call's own compare-and-set has won. A prepared
    command's private channel is released on every path, including a failed
    preflight. The workspace is removed only once both facts that make removal
    safe are in hand -- the completion proving no process or descendant of this
    attempt is left, and the durable terminal attempt.

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

    `artifacts` is where a verification that exits nonzero publishes the tail of
    what it printed, so the words the attempt ends with can name where that
    proof lives rather than just the exit code (#1137). It is required exactly
    where a project is pinned with a redeemable grant; every other attempt never
    reads it. A runtime pinned with a grant but wired with no publisher refuses
    beside the verification's own preflight, before any provider process, so a
    check that later exits nonzero never discovers the missing door while
    trying to keep what it already ran.

    What the attempt made is kept last of all, after any granted check has run
    and before the attempt is completed. Last, because the candidate must be the
    state the verification passed on rather than the state before it; before,
    because the lease is released once this attempt is terminal and nothing can
    recover the work afterwards. The two writes cannot share a transaction --
    the candidate is a git object and the attempt a durable row -- so the order
    is what carries the invariant: no attempt is ever `SUCCEEDED` without its
    work kept, and a capture that fails ends it `FAILED` under
    `CANDIDATE_CAPTURE_FAILED` instead.
    """

    store.prepare(execution)
    try:
        command = executor.prepare_process(execution.request)
    except AgentExecutionPreflightRefusal as refusal:
        claim = store.claim(execution)
        if isinstance(claim, AgentAttemptClaimedByThisCall):
            return store.complete_agent_refusal(execution, refusal.reason)
        return claim
    try:
        workspaces.preflight()
        if project is not None:
            project.source.attest(project.pin)
            if project.grant is not None:
                project.verifications.preflight(project.pin)
                if (
                    project.grant.capability
                    is ToolGrantCapability.RUN_PROJECT_VERIFICATION
                    and artifacts is None
                ):
                    raise RuntimeError(
                        "a project pinned a redeemable run-project-verification "
                        "grant, but this attempt was given no artifact "
                        "publisher to keep a failed verification's output with"
                    )
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
        result = _with_recorded_transcript(
            executor.decode_process_completion(invocation, completion), clock
        )
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
                redeemed = _redeemed(execution, lease, project)
            except ProjectVerificationUnavailable as error:
                # The claim already won; letting this escape leaves the attempt
                # LAUNCH_ARMED, and a replay would report AgentAttemptPossiblyRan.
                # The provider had already answered when the check went silent,
                # so its steps travel into this ending too rather than this
                # being the one path that drops them.
                outcome = store.complete_project_verification_failure(
                    execution,
                    _verification_unavailable_verdict(error),
                    result.transcript,
                )
            else:
                redemption = redeemed.receipt if redeemed is not None else None
                # A check that said no has already decided this attempt, so
                # nothing is captured and nothing else may rename the ending.
                # Capturing first would keep work the project rejected and, worse,
                # let the loss of that work overwrite the verdict: the attempt
                # would read CANDIDATE_CAPTURE_FAILED and carry a redemption
                # saying the check failed. The store owns what a nonzero
                # redemption means; this only refuses to reach past it.
                #
                # Past that, `redemption` here is a value this branch is *known*
                # to have and known to be a pass, so its evidence travels into
                # whichever ending follows.
                if redeemed is not None and not redeemed.receipt.satisfied_the_project:
                    outcome = store.complete_success(
                        execution,
                        result,
                        redeemed.receipt,
                        _published_verification_failure_evidence(
                            redeemed.outcome, artifacts
                        ),
                    )
                else:
                    try:
                        _keep_what_the_attempt_made(lease, project)
                    except CandidateNotKept as refusal:
                        # Same reason as the branch above, for the other loss:
                        # letting this escape would leave the attempt
                        # LAUNCH_ARMED. The work is gone either way, but a named
                        # failure is a fact an operator can act on, while an
                        # armed attempt is one nobody can resolve. What the
                        # granted check proved before the loss is kept with it --
                        # the check passed, and that stays true however the
                        # keeping ended.
                        outcome = store.complete_candidate_capture_failure(
                            execution, str(refusal), result.transcript, redemption
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
                    case AgentAttemptFailureCode.CANDIDATE_CAPTURE_FAILED:
                        event = "agent_attempt_candidate_capture_failed"
                        detail = (
                            "did the work and none of it could be kept; "
                            "the loss is durably named."
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


def _with_recorded_transcript(
    result: AgentExecutionResult | AgentExecutionFailure,
    clock: Callable[[], RecordedAt],
) -> AgentExecutionResult | AgentExecutionFailure:
    """Stamp decoded events at the one boundary that records their transcript."""

    transcript = result.transcript
    if transcript is None:
        return result
    return replace(result, transcript=transcript.with_recorded_moment(clock()))


def _keep_what_the_attempt_made(
    lease: AgentAttemptWorkspaceLease, project: PinnedProjectSource | None
) -> None:
    """Anchor what this attempt made, where a reader can still find it later.

    Nothing comes back. The candidate is anchored under the attempt's own
    identity, so whoever holds the attempt can ask the store for it; carrying
    the tree's address into the durable row as well would be a second record of
    one fact, and two records of one fact can disagree.

    A runtime pointed at no project keeps nothing, because there is no store
    that could own the result and no pin the work would be a change to.
    """

    if project is None:
        return
    project.candidates.capture(project.pin, lease)


def _verification_unavailable_verdict(error: ProjectVerificationUnavailable) -> str:
    """How the granted check failed to complete, without inventing an exit code."""

    if error.timeout_seconds is None:
        return str(error)
    return f"timeout {error.timeout_seconds} seconds"


def _published_verification_failure_evidence(
    outcome: ProjectVerificationOutcome, artifacts: ArtifactPublisher | None
) -> ProjectVerificationFailureEvidence:
    """Keep a failed check's own output where its refusal can point to it.

    Nothing is published where the command left nothing to keep -- the artifact
    store refuses empty content by its own rule, and a refusal naming no
    artifact is exactly as honest as one naming an empty one. Publication is
    idempotent by content, so a replay of this same attempt lands on the same
    address rather than growing a second copy. Any credential shape
    `redact_credentials` recognises is replaced before the tail ever reaches
    the publisher, because this artifact is HTTP-readable durable material,
    not a transcript already behind its own boundary.

    A publisher this runtime was never wired with is refused by preflight,
    before any provider work; `artifacts` is `None` here only if that
    invariant broke, which this treats as this function's own defect rather
    than repeating preflight's wiring refusal. A publisher that answers but
    refuses, cannot be reached, or answers with a state no write sequence
    could have produced degrades the attempt's words instead of abandoning
    it: the exit code, the command and the summary line still reach the
    receipt, with a note naming why the tail itself is not kept beside them.
    """

    if not outcome.output_tail:
        return ProjectVerificationFailureEvidence(
            outcome.summary_line, None, outcome.duration_seconds, redacted=False
        )
    if artifacts is None:
        raise AssertionError(
            "preflight refuses a redeemable grant with no artifact publisher "
            "before any provider work; reaching publication without one is a "
            "defect in this runtime, not a condition this attempt can name"
        )
    redacted_tail = redact_credentials(outcome.output_tail.decode("utf-8", "replace"))
    published = publish_artifact(redacted_tail.text.encode("utf-8"), artifacts)
    match published:
        case ArtifactPublicationCreated(artifact) | ArtifactPublicationExisting(
            artifact
        ):
            return ProjectVerificationFailureEvidence(
                outcome.summary_line,
                artifact.artifact_hash,
                outcome.duration_seconds,
                redacted=redacted_tail.redacted,
            )
        case ArtifactPublicationInvalid() | WriteUnavailable() | DurableStateCorrupt():
            return ProjectVerificationFailureEvidence(
                outcome.summary_line,
                None,
                outcome.duration_seconds,
                redacted=redacted_tail.redacted,
                retention_failure=_publication_failure_reason(published),
            )


def _publication_failure_reason(
    published: ArtifactPublicationInvalid | WriteUnavailable | DurableStateCorrupt,
) -> str:
    """Why a failed check's output could not be kept, in the reader's own words."""

    match published:
        case ArtifactPublicationInvalid(verdict):
            return str(verdict)
        case WriteUnavailable(detail):
            return detail if detail is not None else "artifact write unavailable"
        case DurableStateCorrupt():
            return "durable artifact state corrupt"


@dataclass(frozen=True)
class _RedeemedGrant:
    """A grant's receipt, beside the raw outcome it was built from.

    The receipt alone answers the store's question -- did the check pass --
    but composing a failed check's words needs what the receipt deliberately
    does not keep: the output itself, not just its hash. Both travel together
    so a caller with one always has the other, rather than reopening a process
    or a released workspace to get back what it already read.
    """

    receipt: ToolRedemptionReceipt
    outcome: ProjectVerificationOutcome


def _redeemed(
    execution: AgentAttemptExecution,
    lease: AgentAttemptWorkspaceLease,
    project: PinnedProjectSource | None,
) -> _RedeemedGrant | None:
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
    receipt = ToolRedemptionReceipt.of(
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
    return _RedeemedGrant(receipt, outcome)


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
