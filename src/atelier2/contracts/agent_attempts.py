from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import StrEnum

from atelier2.contracts.agents import (
    MAXIMUM_AGENT_FIELD_CHARACTERS,
    AgentExecutionRequestHash,
    AgentExecutorOperationalIdentity,
    AgentReceiptHash,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import RunId, WorkflowRevisionHash

AGENT_ATTEMPT_ORDINAL = 1
REPLACEMENT_AGENT_ATTEMPT_ORDINAL = 2


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


class AgentAttemptReplacement(StrEnum):
    NONE = "NONE"
    ONE = "ONE"


class AgentAttemptRedriveState(StrEnum):
    PENDING = "PENDING"
    OWNER_NOT_LOCAL = "OWNER_NOT_LOCAL"
    CLEANUP_ATTESTED = "CLEANUP_ATTESTED"


class AgentAttemptCancellationDisposition(StrEnum):
    NEVER_LAUNCHED = "NEVER_LAUNCHED"
    EXITED_BEFORE_SIGNAL = "EXITED_BEFORE_SIGNAL"
    REAPED_AFTER_TERM = "REAPED_AFTER_TERM"
    REAPED_AFTER_KILL = "REAPED_AFTER_KILL"
    OWNER_LOST_AFTER_PARENT_DEATH = "OWNER_LOST_AFTER_PARENT_DEATH"


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

    @property
    def workflow_id(self) -> str:
        digest = Sha256Hash.of(
            frame(
                "agent-cancellation-workflow-id/v1",
                self.attempt_id.value.encode("ascii"),
                self.command_id.encode("utf-8"),
            )
        )
        return f"atelier2-agent-cancel-{digest.value}"


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
                    )
                    or (
                        self.process_phase is AgentAttemptProcessPhase.WATCHDOG_READY
                        and self.state_version == 1
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
                and self.process_phase is AgentAttemptProcessPhase.CLEANUP_ATTESTED
            )
        elif self.state is AgentAttemptState.SUCCEEDED:
            valid = (
                self.failure_code is None
                and self.receipt_hash is not None
                and self.cancellation is None
            )
        else:
            valid = (
                self.failure_code
                is AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
                and self.receipt_hash is None
                and self.cancellation is None
            )
        if not valid:
            raise ValueError(
                "agent attempt cancellation/state has a noncanonical shape"
            )
