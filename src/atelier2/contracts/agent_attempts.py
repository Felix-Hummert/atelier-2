from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.agent_transcripts import (
    AttemptTranscript,
    ProviderTerminalRefusal,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    AgentExecutionRequestHash,
    AgentExecutorOperationalIdentity,
    AgentReceiptHash,
)
from atelier2.contracts.artifacts import ArtifactHash
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.secret_redaction import redact_credentials

AGENT_ATTEMPT_ORDINAL = 1
REPLACEMENT_AGENT_ATTEMPT_ORDINAL = 2
MAXIMUM_RUNNER_STANDARD_ERROR_BYTES = 49_152
STOP_AFTER_DRIVER_LOSS = "atelier2-driver-lost"
"""The command id a restart stops an attempt under when its driver is gone.

One durable id for the whole class, because the id is what the cancellation
event carries: a reader of the run is then told *why* the attempt was stopped
rather than only that it was. It is stable so that a second restart re-issues
the very same command instead of a second one the attempt would refuse.
"""


class AgentAttemptId(Sha256Hash):
    @classmethod
    def for_execution(
        cls,
        node_execution_id: NodeExecutionId,
        request_hash: AgentExecutionRequestHash,
        attempt_ordinal: int = AGENT_ATTEMPT_ORDINAL,
    ) -> AgentAttemptId:
        if type(attempt_ordinal) is not int or attempt_ordinal not in (
            AGENT_ATTEMPT_ORDINAL,
            REPLACEMENT_AGENT_ATTEMPT_ORDINAL,
        ):
            raise ValueError("agent attempt ordinal must be exactly 1 or 2")
        return cls.of(
            frame(
                "agent-attempt-id/v1",
                node_execution_id.value.encode("ascii"),
                request_hash.value.encode("ascii"),
                struct.pack(
                    ">Q", attempt_ordinal
                ),  # minted-id family; see hashing.frame
            )
        )


class AgentAttemptReceiptHash(Sha256Hash):
    """Identity of the immutable evidence one attempt wrote."""


@dataclass(frozen=True)
class OutputSchemaRefusalReceipt:
    """The validator evidence that orders the one bounded repair attempt."""

    attempt_id: AgentAttemptId
    reason: str
    schema_revision: PublishedRevisionHash
    value_hash: Sha256Hash
    artifact_hash: ArtifactHash | None
    receipt_hash: AgentAttemptReceiptHash = field(init=False)

    def __post_init__(self) -> None:
        if self.reason == "":
            raise ValueError("an output-schema refusal receipt names its reason")
        object.__setattr__(
            self,
            "receipt_hash",
            AgentAttemptReceiptHash.of(
                frame(
                    "agent-attempt-output-schema-refusal-receipt/v1",
                    self.attempt_id.value.encode("ascii"),
                    self.reason.encode("utf-8"),
                    self.schema_revision.value.encode("ascii"),
                    self.value_hash.value.encode("ascii"),
                    b""
                    if self.artifact_hash is None
                    else self.artifact_hash.value.encode("ascii"),
                )
            ),
        )


class AgentAttemptState(StrEnum):
    PREPARED = "PREPARED"
    LAUNCH_ARMED = "LAUNCH_ARMED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


TERMINAL_AGENT_ATTEMPT_STATES = frozenset(
    (
        AgentAttemptState.SUCCEEDED,
        AgentAttemptState.FAILED,
        AgentAttemptState.CANCELLED,
        AgentAttemptState.INTERRUPTED,
    )
)
"""Every state a durable attempt can no longer leave."""


class AgentAttemptFailureCode(StrEnum):
    PROCESS_EXITED_UNSUCCESSFULLY = "PROCESS_EXITED_UNSUCCESSFULLY"
    PROCESS_OUTPUT_LIMIT_EXCEEDED = "PROCESS_OUTPUT_LIMIT_EXCEEDED"
    PROCESS_SUPERVISION_FAILED = "PROCESS_SUPERVISION_FAILED"
    # The provider process ended fine; what it produced is what the schema its
    # own author pinned refuses. A distinct member because folding it into the
    # process code would write a durable statement about an exit that never
    # happened; the schema owner's words travel in the node receipt's reason.
    OUTPUT_SCHEMA_REFUSED = "OUTPUT_SCHEMA_REFUSED"
    # The process ended fine and the bytes are a declared refusal form, not a
    # success and not a schema miss. Folding this into either existing code
    # would write that a schema refused what no schema saw, or that a process
    # died that exited cleanly.
    AGENT_REFUSED = "AGENT_REFUSED"
    # The process ended fine and the bytes are a success the schema admits;
    # the project's own granted check then exited nonzero, or did not answer
    # within its declared deadline. Folding this into the process code would
    # write that the provider died; folding it into a schema or agent refusal
    # would write that a form refused what no form saw. A timeout has no exit
    # code, so it must not invent one.
    PROJECT_VERIFICATION_FAILED = "PROJECT_VERIFICATION_FAILED"
    # Everything went right up to the end: the process answered, the schema
    # admitted the bytes, any granted check passed -- and the work itself could
    # not be kept past the directory it was made in. Every other code here would
    # be a durable lie about that: nothing died, no form refused anything, and no
    # verification failed. What is lost is the work, not the answer, and only a
    # word of its own can say so to whoever reads this attempt later.
    CANDIDATE_CAPTURE_FAILED = "CANDIDATE_CAPTURE_FAILED"
    # The process answered and left the leased directory holding exactly the
    # tree its pin named: this attempt changed nothing at all. Every other code
    # here would be a lie about that -- nothing died, no form refused anything,
    # no check ever ran, and there was no work to keep in the first place.
    # Naming it is what lets such an attempt end in seconds instead of paying a
    # whole project verification to discover that it verified the pin (#1156).
    CANDIDATE_UNCHANGED = "CANDIDATE_UNCHANGED"


MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES = 2_048
"""How much of a dead process's standard error one durable receipt keeps.

Supervision already bounds how much a child may say at all; this is the far
smaller bound of how much of it becomes a *receipt*, because a receipt is a
sentence an operator reads at a glance, not a log. One owner for the number, so
no writer of a receipt picks its own.
"""


MAXIMUM_RECEIPTED_AGENT_ANSWER_BYTES = 2_048
"""How much of what a provider answered one durable receipt keeps as words.

Sibling to `MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES`, and the same width for the
same reason: a receipt is a sentence an operator reads at a glance. Kept from
the *start* of the answer rather than its end, because an answer is a document
whose opening fields are the ones a reader came for -- a tail would cut off the
beginning of the very summary it exists to show.
"""


def _readable(said: bytes) -> str:
    """Provider bytes as text a receipt may hold and a terminal may print.

    A tail is cut at a byte boundary, so a character the cut split is replaced
    rather than raised over. Control characters are replaced for a second
    reason: a provider that writes terminal escape sequences to standard error
    would otherwise move the cursor of every operator who prints this reason.
    """
    return "".join(
        character if character.isprintable() or character in "\n\t" else "\ufffd"
        for character in said.decode("utf-8", "replace")
    )


@dataclass(frozen=True)
class ProcessExitSignature:
    """How one provider process ended, in the words its receipt keeps.

    `return_code` follows the convention the process runner reports it under: a
    negative value is the signal that killed the child. Zero is not a
    contradiction beside `PROCESS_EXITED_UNSUCCESSFULLY` -- an executor reaches
    that code just as well for a child that exited cleanly and left an answer
    the provider's own wire format cannot carry -- so the named verdict says
    which of the three happened instead of implying an exit that never did.

    Standard error travels here and nowhere else: it is the only place the
    reason a process died is written down, and it is deliberately not on the
    event stream, which stays the bounded, secret-free surface it is.
    """

    return_code: int
    standard_error: bytes

    def __post_init__(self) -> None:
        if type(self.return_code) is not int:
            raise TypeError("a process exit signature names an integer return code")
        if type(self.standard_error) is not bytes:
            raise TypeError("a process exit signature carries standard error bytes")

    def named(self) -> str:
        """The one sentence a receipt keeps about this exit."""
        return f"{self._ended}; {self._said}"

    @property
    def _ended(self) -> str:
        if self.return_code < 0:
            return f"killed by signal {-self.return_code}"
        if self.return_code > 0:
            return f"exited with code {self.return_code}"
        return "exited with code 0 leaving an answer its executor could not read"

    @property
    def _said(self) -> str:
        if not self.standard_error:
            return "it wrote nothing to standard error"
        tail = _readable(self.standard_error[-MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES:])
        if len(self.standard_error) <= MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES:
            return f"standard error: {tail}"
        return (
            f"last {MAXIMUM_RECEIPTED_STANDARD_ERROR_BYTES} of "
            f"{len(self.standard_error)} standard error bytes: {tail}"
        )


def receipted_agent_answer(answer: bytes) -> str:
    """What the provider answered, in the bounded words a receipt may keep.

    Redacted before it is kept, unlike the transcript this answer travels
    beside: a receipt reason is durable material an operator reads and the run
    page shows, so a credential a provider echoed out of its own tooling must
    not survive into it.
    """

    if not answer:
        return "nothing"
    head = redact_credentials(
        _readable(answer[:MAXIMUM_RECEIPTED_AGENT_ANSWER_BYTES])
    ).text
    if len(answer) <= MAXIMUM_RECEIPTED_AGENT_ANSWER_BYTES:
        return head
    return (
        f"first {MAXIMUM_RECEIPTED_AGENT_ANSWER_BYTES} of {len(answer)} "
        f"answer bytes: {head}"
    )


def process_exit_verdict(
    exit_signature: ProcessExitSignature, transcript: AttemptTranscript | None
) -> str:
    """The one sentence a receipt keeps about a failed process, honestly sourced.

    An exit code and an empty standard error explain nothing about a call the
    provider itself read and refused before it did anything (`#1029`, `#942`):
    the process behaved exactly as designed, and the shell around it is not
    where that refusal was said. Where the transcript carries the provider's
    own named refusal, the receipt keeps that instead of the exit signature's
    silence; every other ending -- a crash, a timeout, a supervision failure --
    still gets the exit signature's own words, unchanged. Any provider's
    transcript may carry the refusal step, not only Claude's: the vocabulary is
    neutral even though one adapter is the only writer of it today.
    """

    refusal = _terminal_refusal(transcript)
    if refusal is None:
        return exit_signature.named()
    return f"provider-reported: {refusal.terminal_reason}: {refusal.text}"


def _terminal_refusal(
    transcript: AttemptTranscript | None,
) -> ProviderTerminalRefusal | None:
    if transcript is None:
        return None
    for event in transcript.events:
        if isinstance(event, ProviderTerminalRefusal):
            return event
    return None


class AgentAttemptReplacement(StrEnum):
    NONE = "NONE"
    ONE = "ONE"


class AgentAttemptRedriveState(StrEnum):
    PENDING = "PENDING"
    OWNER_NOT_LOCAL = "OWNER_NOT_LOCAL"
    CLEANUP_ATTESTED = "CLEANUP_ATTESTED"


class AgentAttemptCancellationDisposition(StrEnum):
    """How cleanup of a cancelled attempt settled.

    One closed set, written once. The query resource and the SSE event
    resources name these members rather than restating the tokens.
    """

    NEVER_LAUNCHED = "NEVER_LAUNCHED"
    EXITED_BEFORE_SIGNAL = "EXITED_BEFORE_SIGNAL"
    REAPED_AFTER_TERM = "REAPED_AFTER_TERM"
    REAPED_AFTER_KILL = "REAPED_AFTER_KILL"
    OWNER_LOST_AFTER_PARENT_DEATH = "OWNER_LOST_AFTER_PARENT_DEATH"


class RunnerManifestId(Sha256Hash):
    """SHA-256 content identity of the exact Runner offer Core selected."""


def _require_runner_identity(value: str, owner: str) -> None:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= MAXIMUM_AGENT_FIELD_CHARACTERS
    ):
        raise ValueError(
            f"{owner} must contain 1..{MAXIMUM_AGENT_FIELD_CHARACTERS} exact characters"
        )
    _require_runner_text(value, owner)


def _require_runner_text(value: str, owner: str) -> None:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{owner} must be encodable as UTF-8") from error
    if encoded.decode("utf-8") != value:
        raise ValueError(f"{owner} must have one canonical UTF-8 encoding")


@dataclass(frozen=True)
class RunnerGenerationId:
    """Core-owned identity of one placement, minted before an external effect."""

    value: str

    def __post_init__(self) -> None:
        _require_runner_identity(self.value, "runner generation id")


@dataclass(frozen=True)
class RunnerInvocationId:
    """Runner-owned identity of the one execution accepted for a generation."""

    value: str

    def __post_init__(self) -> None:
        _require_runner_identity(self.value, "runner invocation id")


class RunnerCancellationObservation(StrEnum):
    NEVER_LAUNCHED = "NEVER_LAUNCHED"
    EXITED_BEFORE_SIGNAL = "EXITED_BEFORE_SIGNAL"
    REAPED_AFTER_TERM = "REAPED_AFTER_TERM"
    REAPED_AFTER_KILL = "REAPED_AFTER_KILL"


class RunnerEvidenceAcceptancePhase(StrEnum):
    """How far Core durably accepted one semantic evidence object."""

    NONE = "NONE"
    CORE_COMMITTED = "CORE_COMMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"


class RunnerTerminalEvidenceHash(Sha256Hash):
    """Semantic identity of one Runner evidence object, kept for the durable
    `runner_terminal_evidence_hash` column an attempt may already carry."""


class AgentAttemptProcessPhase(StrEnum):
    NONE = "NONE"
    WATCHDOG_READY = "WATCHDOG_READY"
    LAUNCH_AUTHORIZED = "LAUNCH_AUTHORIZED"
    PROCESS_OBSERVED = "PROCESS_OBSERVED"
    CLEANUP_ATTESTED = "CLEANUP_ATTESTED"


@dataclass(frozen=True)
class AgentProcessOwnerId:
    value: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.value) <= MAXIMUM_AGENT_FIELD_CHARACTERS:
            raise ValueError(
                "agent process owner id must contain "
                f"1..{MAXIMUM_AGENT_FIELD_CHARACTERS} characters"
            )


@dataclass(frozen=True)
class WatchdogGenerationId:
    value: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.value) <= MAXIMUM_AGENT_FIELD_CHARACTERS:
            raise ValueError(
                "watchdog generation id must contain "
                f"1..{MAXIMUM_AGENT_FIELD_CHARACTERS} characters"
            )


@dataclass(frozen=True)
class AgentAttemptCancellation:
    command_id: str
    expected_attempt_state_version: int
    replacement: AgentAttemptReplacement
    redrive_state: AgentAttemptRedriveState = AgentAttemptRedriveState.PENDING
    disposition: AgentAttemptCancellationDisposition | None = None

    def __post_init__(self) -> None:
        if not 1 <= len(self.command_id) <= MAXIMUM_AGENT_FIELD_CHARACTERS:
            raise ValueError(
                "cancellation command id must contain "
                f"1..{MAXIMUM_AGENT_FIELD_CHARACTERS} characters"
            )
        if (
            type(self.expected_attempt_state_version) is not int
            or self.expected_attempt_state_version < 0
        ):
            raise ValueError(
                "expected agent attempt state version must be a nonnegative integer"
            )
        if not isinstance(self.replacement, AgentAttemptReplacement):
            raise TypeError("cancellation replacement policy must be typed")
        if not isinstance(self.redrive_state, AgentAttemptRedriveState):
            raise TypeError("cancellation redrive state must be typed")
        if (self.redrive_state is AgentAttemptRedriveState.CLEANUP_ATTESTED) != (
            self.disposition is not None
        ):
            raise ValueError(
                "cancellation cleanup attestation and disposition must agree"
            )

    def matches(self, request: CancelAgentAttemptRequest) -> bool:
        """Answer whether this cancellation is the one the request commands.

        Redrive progress and disposition are outcomes the request never carries,
        so they never take part in the identity.
        """

        return (
            self.command_id == request.command_id
            and self.expected_attempt_state_version
            == request.expected_attempt_state_version
            and self.replacement is request.replacement
        )


@dataclass(frozen=True)
class CancelAgentAttemptRequest:
    run_id: RunId
    attempt_id: AgentAttemptId
    command_id: str
    expected_attempt_state_version: int
    replacement: AgentAttemptReplacement

    def __post_init__(self) -> None:
        AgentAttemptCancellation(
            self.command_id,
            self.expected_attempt_state_version,
            self.replacement,
        )


@dataclass(frozen=True)
class AgentAttempt:
    attempt_id: AgentAttemptId
    node_execution_id: NodeExecutionId
    request_hash: AgentExecutionRequestHash
    executor_operational_identity: AgentExecutorOperationalIdentity
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    node_id: str
    attempt_ordinal: int
    state: AgentAttemptState
    state_version: int
    failure_code: AgentAttemptFailureCode | None = None
    receipt_hash: AgentReceiptHash | None = None
    process_phase: AgentAttemptProcessPhase = AgentAttemptProcessPhase.NONE
    process_owner_id: AgentProcessOwnerId | None = None
    watchdog_generation_id: WatchdogGenerationId | None = None
    cancellation: AgentAttemptCancellation | None = None
    runner_manifest_id: RunnerManifestId | None = None
    runner_generation_id: RunnerGenerationId | None = None
    runner_invocation_id: RunnerInvocationId | None = None
    runner_terminal_evidence_hash: RunnerTerminalEvidenceHash | None = None
    runner_evidence_acceptance_phase: RunnerEvidenceAcceptancePhase = (
        RunnerEvidenceAcceptancePhase.NONE
    )
    transcript_artifact_hash: ArtifactHash | None = None
    """Where this attempt's steps are kept, or nothing where none were decoded.

    A pointer rather than the transcript, because the transcript is bytes read
    whole and the artifact store is this repository's one owner of those. It is
    absent for every attempt whose executor publishes no structured stream and
    for every ending that reached no process at all, and an absent pointer says
    exactly that -- never that the attempt took no steps.
    """

    def __post_init__(self) -> None:
        if not isinstance(self.attempt_id, AgentAttemptId):
            raise TypeError("agent attempt id must be typed")
        if not isinstance(
            self.executor_operational_identity, AgentExecutorOperationalIdentity
        ):
            raise TypeError("agent attempt executor identity must be typed")
        if self.attempt_id != AgentAttemptId.for_execution(
            self.node_execution_id, self.request_hash, self.attempt_ordinal
        ):
            raise ValueError("agent attempt id differs from its exact binding")
        if not self.node_id:
            raise ValueError("agent attempt node id must be nonempty")
        if (
            type(self.state_version) is not int
            or self.state_version < 0
            or not isinstance(self.process_phase, AgentAttemptProcessPhase)
        ):
            raise ValueError("agent attempt state has a noncanonical shape")
        owner_bound = self.process_owner_id is not None
        generation_bound = self.watchdog_generation_id is not None
        if owner_bound != generation_bound:
            raise ValueError(
                "agent process owner and generation must be bound together"
            )
        if owner_bound and (
            not isinstance(self.process_owner_id, AgentProcessOwnerId)
            or not isinstance(self.watchdog_generation_id, WatchdogGenerationId)
        ):
            raise TypeError("agent process owner and generation must be typed")
        runner_manifest_bound = self.runner_manifest_id is not None
        runner_generation_bound = self.runner_generation_id is not None
        if runner_manifest_bound != runner_generation_bound:
            raise ValueError("runner manifest and generation must be bound together")
        if runner_manifest_bound and (
            not isinstance(self.runner_manifest_id, RunnerManifestId)
            or not isinstance(self.runner_generation_id, RunnerGenerationId)
        ):
            raise TypeError("runner manifest and generation must be typed")
        if self.runner_invocation_id is not None and (
            not isinstance(self.runner_invocation_id, RunnerInvocationId)
            or not runner_manifest_bound
        ):
            raise ValueError("runner invocation requires its exact generation binding")
        if not isinstance(
            self.runner_evidence_acceptance_phase, RunnerEvidenceAcceptancePhase
        ):
            raise TypeError("runner evidence acceptance phase must be typed")
        evidence_kept = self.runner_terminal_evidence_hash is not None
        if evidence_kept != (
            self.runner_evidence_acceptance_phase
            is not RunnerEvidenceAcceptancePhase.NONE
        ):
            raise ValueError("runner evidence hash and acceptance phase must agree")
        if evidence_kept and not isinstance(
            self.runner_terminal_evidence_hash, RunnerTerminalEvidenceHash
        ):
            raise TypeError("runner terminal evidence hash must be typed")
        if self.transcript_artifact_hash is not None and not isinstance(
            self.transcript_artifact_hash, ArtifactHash
        ):
            raise TypeError("agent attempt transcript pointer must be typed")
        if (
            owner_bound or self.process_phase is not AgentAttemptProcessPhase.NONE
        ) and (runner_manifest_bound):
            raise ValueError(
                "legacy process ownership and runner binding are exclusive"
            )
        if (self.runner_invocation_id is not None or evidence_kept) and not (
            runner_manifest_bound
        ):
            raise ValueError("runner invocation and evidence require a generation")
        if (
            self.state is AgentAttemptState.PREPARED
            and evidence_kept
            and self.runner_evidence_acceptance_phase
            not in {
                RunnerEvidenceAcceptancePhase.CORE_COMMITTED,
                RunnerEvidenceAcceptancePhase.ACKNOWLEDGED,
            }
        ):
            raise ValueError("prepared runner evidence has a noncanonical phase")
        if self.process_phase is AgentAttemptProcessPhase.NONE and owner_bound:
            raise ValueError("unprepared agent process may not have a live owner")
        ownerless_never_launched = (
            self.process_phase is AgentAttemptProcessPhase.CLEANUP_ATTESTED
            and self.cancellation is not None
            and self.cancellation.disposition
            is AgentAttemptCancellationDisposition.NEVER_LAUNCHED
        )
        if (
            self.process_phase is not AgentAttemptProcessPhase.NONE
            and not owner_bound
            and not ownerless_never_launched
        ):
            raise ValueError(
                "prepared agent process requires its exact owner generation"
            )
        if self.state in TERMINAL_AGENT_ATTEMPT_STATES and self.state_version < 2:
            raise ValueError("terminal agent attempt requires state version at least 2")
        if self.state is AgentAttemptState.PREPARED:
            valid = (
                self.failure_code is None
                and self.receipt_hash is None
                and self.cancellation is None
                and (
                    (
                        self.process_phase is AgentAttemptProcessPhase.NONE
                        and self.state_version == 0
                        and not runner_manifest_bound
                    )
                    or (
                        self.process_phase is AgentAttemptProcessPhase.WATCHDOG_READY
                        and self.state_version == 1
                    )
                    or (
                        self.process_phase is AgentAttemptProcessPhase.NONE
                        and self.state_version >= 1
                        and runner_manifest_bound
                    )
                )
            )
        elif self.state is AgentAttemptState.LAUNCH_ARMED:
            valid = (
                self.state_version >= 1
                and self.failure_code is None
                and self.receipt_hash is None
                and self.cancellation is None
                and self.process_phase
                in {
                    # NONE preserves the landed B0.2 durable vector while V7 launch
                    # paths bind LAUNCH_AUTHORIZED before any real child exists.
                    AgentAttemptProcessPhase.NONE,
                    AgentAttemptProcessPhase.LAUNCH_AUTHORIZED,
                    AgentAttemptProcessPhase.PROCESS_OBSERVED,
                }
                and (
                    self.process_phase is not AgentAttemptProcessPhase.NONE
                    or self.state_version == 1
                    or runner_manifest_bound
                )
            )
        elif self.state is AgentAttemptState.CANCEL_REQUESTED:
            valid = (
                self.state_version >= 1
                and self.failure_code is None
                and self.receipt_hash is None
                and self.cancellation is not None
                and self.cancellation.disposition is None
            )
        elif self.state in {
            AgentAttemptState.CANCELLED,
            AgentAttemptState.INTERRUPTED,
        }:
            valid = (
                self.failure_code is None
                and self.receipt_hash is None
                and self.cancellation is not None
                and self.cancellation.disposition is not None
                and (
                    self.process_phase is AgentAttemptProcessPhase.CLEANUP_ATTESTED
                    or (
                        runner_manifest_bound
                        and self.process_phase is AgentAttemptProcessPhase.NONE
                    )
                )
            )
        elif self.state is AgentAttemptState.SUCCEEDED:
            valid = (
                self.failure_code is None
                and self.receipt_hash is not None
                and self.cancellation is None
            )
        else:
            valid = (
                self.failure_code is not None
                and self.receipt_hash is None
                and self.cancellation is None
            )
        if not valid:
            raise ValueError(
                "agent attempt cancellation/state has a noncanonical shape"
            )


def stop_command_for(attempt: AgentAttempt) -> CancelAgentAttemptRequest:
    """The stop this attempt already stands under, or the one a lost driver earns.

    An attempt that is already being cancelled keeps its exact command: reissuing
    a second one under a new id is what the store refuses as a command conflict,
    and rightly, because two commands would disagree about which cleanup the
    attestation belongs to. An attempt under no command yet is stopped under the
    one durable driver-loss command.
    """

    cancellation = attempt.cancellation
    if cancellation is None:
        return CancelAgentAttemptRequest(
            attempt.run_id,
            attempt.attempt_id,
            STOP_AFTER_DRIVER_LOSS,
            attempt.state_version,
            AgentAttemptReplacement.NONE,
        )
    return CancelAgentAttemptRequest(
        attempt.run_id,
        attempt.attempt_id,
        cancellation.command_id,
        cancellation.expected_attempt_state_version,
        cancellation.replacement,
    )
