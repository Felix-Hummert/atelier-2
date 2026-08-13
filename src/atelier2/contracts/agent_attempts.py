from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import StrEnum

from atelier2.contracts.agents import (
    AgentExecutionRequestHash,
    AgentExecutorOperationalIdentity,
    AgentReceiptHash,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import RunId, WorkflowRevisionHash

AGENT_ATTEMPT_ORDINAL = 1


class AgentAttemptId(Sha256Hash):
    @classmethod
    def for_execution(
        cls,
        node_execution_id: NodeExecutionId,
        request_hash: AgentExecutionRequestHash,
        attempt_ordinal: int = AGENT_ATTEMPT_ORDINAL,
    ) -> AgentAttemptId:
        if type(attempt_ordinal) is not int or attempt_ordinal != AGENT_ATTEMPT_ORDINAL:
            raise ValueError("B0.2 permits exactly one agent attempt")
        return cls.of(
            frame(
                "agent-attempt-id/v1",
                node_execution_id.value.encode("ascii"),
                request_hash.value.encode("ascii"),
                struct.pack(">Q", attempt_ordinal),
            )
        )


class AgentAttemptState(StrEnum):
    PREPARED = "PREPARED"
    LAUNCH_ARMED = "LAUNCH_ARMED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class AgentAttemptFailureCode(StrEnum):
    PROCESS_EXITED_UNSUCCESSFULLY = "PROCESS_EXITED_UNSUCCESSFULLY"


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
        shapes = {
            AgentAttemptState.PREPARED: (0, None, None),
            AgentAttemptState.LAUNCH_ARMED: (1, None, None),
            AgentAttemptState.SUCCEEDED: (2, None, self.receipt_hash),
            AgentAttemptState.FAILED: (
                2,
                AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
                None,
            ),
        }
        if shapes[self.state] != (
            self.state_version,
            self.failure_code,
            self.receipt_hash,
        ) or (self.state is AgentAttemptState.SUCCEEDED and self.receipt_hash is None):
            raise ValueError("agent attempt state has a noncanonical shape")
