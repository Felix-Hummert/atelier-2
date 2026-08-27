from __future__ import annotations

from dataclasses import dataclass, field

from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.node_records_v3 import (
    DeclaredContextPackageHash,
    NodeReceiptHash,
)
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.runs import RunId, WorkflowRevisionHash


class RunForkCommandId(Sha256Hash):
    """The command identity owned by the origin run and the caller's retry key."""

    @classmethod
    def for_request(cls, origin_run_id: RunId, idempotency_key: str) -> RunForkCommandId:
        if idempotency_key == "":
            raise ValueError("a run-fork idempotency key must be nonempty")
        return cls.of(
            frame(
                "run-fork-command-id/v1",
                origin_run_id.value.encode("utf-8"),
                idempotency_key.encode("utf-8"),
            )
        )


class RunForkHash(Sha256Hash):
    """The immutable identity of one `run-fork/v1` record."""


def successor_run_id_for(command_id: RunForkCommandId) -> RunId:
    """Mint the server-owned successor identity from the durable command."""

    digest = Sha256Hash.of(
        frame("run-fork-successor-run-id/v1", command_id.value.encode("ascii"))
    )
    return RunId(digest.value)


@dataclass(frozen=True)
class RunForkReusedNode:
    """One strict-prefix node resolved to its ultimate immutable execution."""

    node_id: str
    round_ordinal: int
    source_run_id: RunId
    source_workflow_revision_hash: WorkflowRevisionHash
    source_node_execution_id: NodeExecutionId
    source_event_hash: Sha256Hash
    source_receipt_hash: NodeReceiptHash
    source_declared_context_package_hash: DeclaredContextPackageHash
    source_agent_receipt_hash: Sha256Hash | None = None

    def __post_init__(self) -> None:
        if self.node_id == "" or self.round_ordinal < 1:
            raise ValueError("a reused node names a node and positive round")
        expected = NodeExecutionId.for_node(
            self.source_run_id,
            self.source_workflow_revision_hash,
            self.node_id,
            self.round_ordinal,
        )
        if self.source_node_execution_id != expected:
            raise ValueError("a reused node execution differs from its source binding")

    def framed(self) -> bytes:
        return frame(
            "run-fork-reused-node/v1",
            self.node_id.encode("utf-8"),
            str(self.round_ordinal).encode("ascii"),
            self.source_run_id.value.encode("utf-8"),
            self.source_workflow_revision_hash.value.encode("ascii"),
            self.source_node_execution_id.value.encode("ascii"),
            self.source_event_hash.value.encode("ascii"),
            self.source_receipt_hash.value.encode("ascii"),
            self.source_declared_context_package_hash.value.encode("ascii"),
            (
                b""
                if self.source_agent_receipt_hash is None
                else self.source_agent_receipt_hash.value.encode("ascii")
            ),
        )


@dataclass(frozen=True)
class RunForkEffectFence:
    """One confirmed origin effect the successor must never replay blindly."""

    node_id: str
    round_ordinal: int
    source_logical_key: LogicalEffectKey
    source_run_id: RunId
    source_workflow_revision_hash: WorkflowRevisionHash
    source_result_hash: Sha256Hash

    def __post_init__(self) -> None:
        if self.node_id == "" or self.round_ordinal < 1:
            raise ValueError("an effect fence names a node and positive round")

    def framed(self) -> bytes:
        return frame(
            "run-fork-effect-fence/v1",
            self.node_id.encode("utf-8"),
            str(self.round_ordinal).encode("ascii"),
            self.source_logical_key.value.encode("utf-8"),
            self.source_run_id.value.encode("utf-8"),
            self.source_workflow_revision_hash.value.encode("ascii"),
            self.source_result_hash.value.encode("ascii"),
        )


@dataclass(frozen=True)
class RunFork:
    """The immutable lineage header and its ordered reuse/fence evidence."""

    command_id: RunForkCommandId
    origin_run_id: RunId
    origin_terminal_hash: Sha256Hash
    successor_run_id: RunId
    workflow_revision_hash: WorkflowRevisionHash
    run_configuration_revision_hash: RunConfigurationRevisionHash
    restart_from_node_id: str
    reused_nodes: tuple[RunForkReusedNode, ...]
    effect_fences: tuple[RunForkEffectFence, ...]
    fork_hash: RunForkHash = field(init=False)

    def __post_init__(self) -> None:
        if self.restart_from_node_id == "":
            raise ValueError("a run fork names its restart node")
        if self.successor_run_id != successor_run_id_for(self.command_id):
            raise ValueError("a run fork successor differs from its command identity")
        reused_coordinates = tuple(
            (entry.node_id, entry.round_ordinal) for entry in self.reused_nodes
        )
        if len(set(reused_coordinates)) != len(reused_coordinates):
            raise ValueError("a run fork reuses each node execution once")
        fence_coordinates = tuple(
            (entry.node_id, entry.round_ordinal) for entry in self.effect_fences
        )
        if len(set(fence_coordinates)) != len(fence_coordinates):
            raise ValueError("a run fork fences each effect execution once")
        object.__setattr__(
            self,
            "fork_hash",
            RunForkHash.of(
                frame(
                    "run-fork/v1",
                    self.command_id.value.encode("ascii"),
                    self.origin_run_id.value.encode("utf-8"),
                    self.origin_terminal_hash.value.encode("ascii"),
                    self.successor_run_id.value.encode("utf-8"),
                    self.workflow_revision_hash.value.encode("ascii"),
                    self.run_configuration_revision_hash.value.encode("ascii"),
                    self.restart_from_node_id.encode("utf-8"),
                    frame(
                        "run-fork-reused-nodes/v1",
                        *(entry.framed() for entry in self.reused_nodes),
                    ),
                    frame(
                        "run-fork-effect-fences/v1",
                        *(entry.framed() for entry in self.effect_fences),
                    ),
                )
            ),
        )
