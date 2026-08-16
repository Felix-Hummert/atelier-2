from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash

if TYPE_CHECKING:
    from atelier2.contracts.agent_attempts import AgentAttemptId
    from atelier2.contracts.agents import AgentExecutionRequestV2

_CANONICAL_INTEGER_BYTES = re.compile(rb"(?:0|-?[1-9][0-9]*)")


def is_canonical_integer_bytes(value: bytes) -> bool:
    return _CANONICAL_INTEGER_BYTES.fullmatch(value) is not None


class NodeExecutionId(Sha256Hash):
    @classmethod
    def for_node(
        cls, run_id: RunId, revision_hash: WorkflowRevisionHash, node_id: str
    ) -> NodeExecutionId:
        return cls.of(
            frame(
                "node-execution-id/v1",
                run_id.value.encode("utf-8"),
                revision_hash.value.encode("ascii"),
                node_id.encode("utf-8"),
            )
        )


@dataclass(frozen=True)
class AgentAttemptExecution:
    request: AgentExecutionRequestV2
    attempt_id: AgentAttemptId
    ordinal: int

    def __post_init__(self) -> None:
        from atelier2.contracts.agent_attempts import AgentAttemptId
        from atelier2.contracts.agents import AgentExecutionRequestV2

        if not isinstance(self.request, AgentExecutionRequestV2):
            raise TypeError("agent attempt execution request must be typed")
        expected = AgentAttemptId.for_execution(
            self.request.node_execution_id, self.request.request_hash, self.ordinal
        )
        if self.attempt_id != expected:
            raise ValueError("agent attempt execution differs from its exact identity")


class RunEventKind(StrEnum):
    AGENT_COMPLETED = "AGENT_COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    AGENT_CANCEL_REQUESTED = "AGENT_CANCEL_REQUESTED"
    AGENT_CANCELLED = "AGENT_CANCELLED"
    AGENT_INTERRUPTED = "AGENT_INTERRUPTED"
    ACTION_RECONCILIATION_REQUIRED = "ACTION_RECONCILIATION_REQUIRED"
    ACTION_RECONCILIATION_RESOLVED = "ACTION_RECONCILIATION_RESOLVED"
    ACTION_COMPLETED = "ACTION_COMPLETED"
    WAITING_INPUT = "WAITING_INPUT"
    WAIT_ANSWERED = "WAIT_ANSWERED"
    SUBWORKFLOW_COMPLETED = "SUBWORKFLOW_COMPLETED"


class WaitAnswerState(StrEnum):
    PENDING = "PENDING"
    APPLIED = "APPLIED"


@dataclass(frozen=True)
class RunEvent:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    event_sequence: int
    node_id: str
    node_execution_id: NodeExecutionId
    event_kind: RunEventKind
    payload: bytes
    payload_hash: Sha256Hash = field(init=False)
    receipt_logical_key: LogicalEffectKey | None = None
    receipt_result_hash: Sha256Hash | None = None
    agent_attempt_id: str | None = None
    attempt_ordinal: int | None = None
    cancellation_command_id: str | None = None
    replacement: str | None = None
    cancellation_disposition: str | None = None
    replacement_attempt_id: str | None = None
    event_hash: Sha256Hash = field(init=False)

    def __post_init__(self) -> None:
        if type(self.event_sequence) is not int or self.event_sequence <= 0:
            raise ValueError("event sequence must be an exact positive integer")
        if self.node_id == "":
            raise ValueError("event node id must be nonempty")
        expected_execution = NodeExecutionId.for_node(
            self.run_id, self.revision_hash, self.node_id
        )
        if self.node_execution_id != expected_execution:
            raise ValueError("event execution identity differs from its node binding")
        payload_hash = Sha256Hash.of(self.payload)
        object.__setattr__(self, "payload_hash", payload_hash)
        receipt_event = self.event_kind in {
            RunEventKind.ACTION_RECONCILIATION_RESOLVED,
            RunEventKind.ACTION_COMPLETED,
        }
        if receipt_event:
            if self.receipt_logical_key is None or self.receipt_result_hash is None:
                raise ValueError("receipt event requires both exact receipt fields")
            if self.receipt_result_hash != payload_hash:
                raise ValueError(
                    "receipt event payload must match its receipt result hash"
                )
        elif (
            self.receipt_logical_key is not None or self.receipt_result_hash is not None
        ):
            raise ValueError("nonreceipt event may not carry receipt fields")
        attempt_bound = self.agent_attempt_id is not None
        if attempt_bound != (self.attempt_ordinal is not None):
            raise ValueError("attempt event requires both exact attempt fields")
        if self.attempt_ordinal not in (None, 1, 2):
            raise ValueError("attempt event ordinal must be exactly 1 or 2")
        cancellation_kind = self.event_kind in {
            RunEventKind.AGENT_CANCEL_REQUESTED,
            RunEventKind.AGENT_CANCELLED,
            RunEventKind.AGENT_INTERRUPTED,
        }
        if cancellation_kind:
            if (
                not attempt_bound
                or self.cancellation_command_id is None
                or self.replacement not in {"NONE", "ONE"}
            ):
                raise ValueError(
                    "cancellation event requires its exact command binding"
                )
            terminal_cancellation = self.event_kind in {
                RunEventKind.AGENT_CANCELLED,
                RunEventKind.AGENT_INTERRUPTED,
            }
            if terminal_cancellation != (self.cancellation_disposition is not None):
                raise ValueError("cancellation event disposition shape disagrees")
        elif any(
            value is not None
            for value in (
                self.cancellation_command_id,
                self.replacement,
                self.cancellation_disposition,
                self.replacement_attempt_id,
            )
        ):
            raise ValueError("noncancellation event may not carry cancellation fields")
        use_v2_hash = cancellation_kind or self.attempt_ordinal == 2
        hash_domain = "node-event-hash/v2" if use_v2_hash else "node-event-hash/v1"
        extra_fields = (
            (
                (self.agent_attempt_id or "").encode("ascii"),
                str(self.attempt_ordinal or "").encode(
                    "ascii"
                ),  # persisted event-hash family
                (self.cancellation_command_id or "").encode("utf-8"),
                (self.replacement or "").encode("ascii"),
                (self.cancellation_disposition or "").encode("ascii"),
                (self.replacement_attempt_id or "").encode("ascii"),
            )
            if use_v2_hash
            else ()
        )
        object.__setattr__(
            self,
            "event_hash",
            Sha256Hash.of(
                frame(
                    hash_domain,
                    self.run_id.value.encode("utf-8"),
                    self.revision_hash.value.encode("ascii"),
                    str(self.event_sequence).encode(
                        "ascii"
                    ),  # persisted event-hash family
                    self.node_execution_id.value.encode("ascii"),
                    self.event_kind.value.encode("ascii"),
                    self.payload,
                    payload_hash.value.encode("ascii"),
                    (
                        b""
                        if self.receipt_logical_key is None
                        else self.receipt_logical_key.value.encode("utf-8")
                    ),
                    (
                        b""
                        if self.receipt_result_hash is None
                        else self.receipt_result_hash.value.encode("ascii")
                    ),
                    *extra_fields,
                )
            ),
        )


@dataclass(frozen=True)
class TransitionSnapshot:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    current_node_id: str
    state: RunState
    state_version: int
    last_event_sequence: int
    event: RunEvent


@dataclass(frozen=True)
class WaitAnswer:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    node_id: str
    node_execution_id: NodeExecutionId
    answer_bytes: bytes
    answer_hash: Sha256Hash = field(init=False)
    answer_workflow_id: str = field(init=False)

    def __post_init__(self) -> None:
        if self.node_id == "":
            raise ValueError("answer node id must be nonempty")
        if self.node_execution_id != NodeExecutionId.for_node(
            self.run_id, self.revision_hash, self.node_id
        ):
            raise ValueError("answer execution identity differs from its node binding")
        object.__setattr__(self, "answer_hash", Sha256Hash.of(self.answer_bytes))
        object.__setattr__(
            self, "answer_workflow_id", answer_workflow_id_for(self.node_execution_id)
        )


@dataclass(frozen=True)
class WaitAnswerSnapshot:
    answer: WaitAnswer
    state: WaitAnswerState
    state_version: int


@dataclass(frozen=True)
class SubmitWaitAnswerRequest:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    node_id: str
    answer_bytes: bytes

    def __post_init__(self) -> None:
        if self.node_id == "":
            raise ValueError("answer node id must be nonempty")


def node_workflow_id_for(execution_id: NodeExecutionId) -> str:
    digest = Sha256Hash.of(
        frame("node-workflow-id/v1", execution_id.value.encode("ascii"))
    )
    return f"atelier2-node-{digest.value}"


def logical_effect_key_for(execution_id: NodeExecutionId) -> LogicalEffectKey:
    digest = Sha256Hash.of(
        frame("logical-effect-key/v1", execution_id.value.encode("ascii"))
    )
    return LogicalEffectKey(f"atelier2-node-effect-{digest.value}")


def answer_workflow_id_for(execution_id: NodeExecutionId) -> str:
    digest = Sha256Hash.of(
        frame("answer-workflow-id/v1", execution_id.value.encode("ascii"))
    )
    return f"atelier2-answer-{digest.value}"


def subworkflow_workflow_id_for(execution_id: NodeExecutionId) -> str:
    digest = Sha256Hash.of(
        frame("subworkflow-workflow-id/v1", execution_id.value.encode("ascii"))
    )
    return f"atelier2-subworkflow-{digest.value}"


def terminal_hash_for(
    revision_hash: WorkflowRevisionHash, event_hashes: tuple[Sha256Hash, ...]
) -> Sha256Hash:
    return Sha256Hash.of(
        frame(
            "run-terminal-hash/v1",
            revision_hash.value.encode("ascii"),
            *(event_hash.value.encode("ascii") for event_hash in event_hashes),
        )
    )
