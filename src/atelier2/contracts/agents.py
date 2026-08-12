from __future__ import annotations

from dataclasses import dataclass, field

from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import RunId, WorkflowRevisionHash


class AgentExecutionRequestHash(Sha256Hash):
    """The immutable fingerprint of one exact logical agent invocation."""


class AgentOutputHash(Sha256Hash):
    """The immutable fingerprint of the exact bytes one agent returned."""


class AgentReceiptHash(Sha256Hash):
    """The immutable fingerprint of one successful agent execution receipt."""


@dataclass(frozen=True)
class AgentExecutorIdentifier:
    value: str

    def __post_init__(self) -> None:
        if self.value == "":
            raise ValueError(f"{type(self).__name__} must be a nonempty string")


class AgentExecutorRevision(AgentExecutorIdentifier):
    """The immutable revision of the executor adapter."""


class AgentExecutorOperationalIdentity(AgentExecutorIdentifier):
    """The stable, non-secret identity of one executor operation."""


@dataclass(frozen=True)
class AgentExecutorBinding:
    adapter_revision: AgentExecutorRevision
    operational_identity: AgentExecutorOperationalIdentity


@dataclass(frozen=True)
class ExactOutputContract:
    output_bytes: bytes


@dataclass(frozen=True)
class AgentExecutionRequest:
    node_execution_id: NodeExecutionId
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    node_id: str
    job_bytes: bytes
    exact_output: ExactOutputContract
    request_hash: AgentExecutionRequestHash = field(init=False)

    def __post_init__(self) -> None:
        if self.node_id == "":
            raise ValueError("agent request node id must be nonempty")
        if self.job_bytes == b"":
            raise ValueError("agent request job bytes must be nonempty")
        expected_execution = NodeExecutionId.for_node(
            self.run_id, self.workflow_revision_hash, self.node_id
        )
        if self.node_execution_id != expected_execution:
            raise ValueError(
                "agent request execution identity differs from its binding"
            )
        object.__setattr__(
            self,
            "request_hash",
            AgentExecutionRequestHash.of(
                frame(
                    "agent-execution-request/v1",
                    self.node_execution_id.value.encode("ascii"),
                    self.run_id.value.encode("utf-8"),
                    self.workflow_revision_hash.value.encode("ascii"),
                    self.node_id.encode("utf-8"),
                    self.job_bytes,
                    self.exact_output.output_bytes,
                )
            ),
        )


@dataclass(frozen=True)
class AgentExecutionResult:
    output_bytes: bytes


@dataclass(frozen=True)
class AgentReceipt:
    request_hash: AgentExecutionRequestHash
    node_execution_id: NodeExecutionId
    run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    node_id: str
    executor_binding: AgentExecutorBinding
    output_bytes: bytes
    output_hash: AgentOutputHash
    receipt_hash: AgentReceiptHash

    def __post_init__(self) -> None:
        if self.node_id == "":
            raise ValueError("agent receipt node id must be nonempty")
        if self.node_execution_id != NodeExecutionId.for_node(
            self.run_id, self.workflow_revision_hash, self.node_id
        ):
            raise ValueError(
                "agent receipt execution identity differs from its binding"
            )
        expected_output_hash = AgentOutputHash.of(self.output_bytes)
        if self.output_hash != expected_output_hash:
            raise ValueError("agent receipt output hash differs from its bytes")
        expected_receipt_hash = self.hash_for(
            self.request_hash,
            self.node_execution_id,
            self.run_id,
            self.workflow_revision_hash,
            self.node_id,
            self.executor_binding,
            self.output_bytes,
            self.output_hash,
        )
        if self.receipt_hash != expected_receipt_hash:
            raise ValueError("agent receipt hash differs from its exact binding")

    @staticmethod
    def hash_for(
        request_hash: AgentExecutionRequestHash,
        node_execution_id: NodeExecutionId,
        run_id: RunId,
        workflow_revision_hash: WorkflowRevisionHash,
        node_id: str,
        executor_binding: AgentExecutorBinding,
        output_bytes: bytes,
        output_hash: AgentOutputHash,
    ) -> AgentReceiptHash:
        return AgentReceiptHash.of(
            frame(
                "agent-receipt/v1",
                request_hash.value.encode("ascii"),
                node_execution_id.value.encode("ascii"),
                run_id.value.encode("utf-8"),
                workflow_revision_hash.value.encode("ascii"),
                node_id.encode("utf-8"),
                executor_binding.adapter_revision.value.encode("utf-8"),
                executor_binding.operational_identity.value.encode("utf-8"),
                output_bytes,
                output_hash.value.encode("ascii"),
            )
        )

    @classmethod
    def for_execution(
        cls,
        request: AgentExecutionRequest,
        executor_binding: AgentExecutorBinding,
        result: AgentExecutionResult,
    ) -> AgentReceipt:
        output_hash = AgentOutputHash.of(result.output_bytes)
        receipt_hash = cls.hash_for(
            request.request_hash,
            request.node_execution_id,
            request.run_id,
            request.workflow_revision_hash,
            request.node_id,
            executor_binding,
            result.output_bytes,
            output_hash,
        )
        return cls(
            request.request_hash,
            request.node_execution_id,
            request.run_id,
            request.workflow_revision_hash,
            request.node_id,
            executor_binding,
            result.output_bytes,
            output_hash,
            receipt_hash,
        )
