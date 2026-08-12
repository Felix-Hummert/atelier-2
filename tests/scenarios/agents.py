from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from atelier2.adapters.dbos.run_store import commit_agent_completed, load_graph
from atelier2.adapters.exact_output_agent import EXACT_OUTPUT_EXECUTOR_BINDING
from atelier2.contracts.agents import (
    AgentExecutionRequest,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    ExactOutputContract,
    ProviderId,
)
from atelier2.contracts.executions import NodeExecutionId, TransitionSnapshot
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflows import AgentNode
from atelier2.ports.agent_executions import AgentExecutorKey


def configured_agent_request(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
) -> AgentExecutionRequest:
    node = load_graph(session, revision_hash).node(node_id)
    assert isinstance(node, AgentNode)
    return AgentExecutionRequest(
        NodeExecutionId.for_node(run_id, revision_hash, node_id),
        run_id,
        revision_hash,
        node_id,
        node.job.encode("utf-8"),
        ExactOutputContract(node.output.encode("utf-8")),
    )


def commit_configured_agent(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
) -> TransitionSnapshot:
    request = configured_agent_request(session, run_id, revision_hash, node_id)
    return commit_agent_completed(
        session,
        request,
        EXACT_OUTPUT_EXECUTOR_BINDING,
        AgentExecutionResult(request.exact_output.output_bytes),
    )


@dataclass
class RecordingAgentExecutorV2:
    output: bytes
    requests: list[AgentExecutionRequestV2]
    lifecycle: list[str]
    name: str
    closes: int = 0

    def execute(self, request: AgentExecutionRequestV2) -> AgentExecutionResult:
        self.requests.append(request)
        self.lifecycle.append(f"execute:{self.name}")
        return AgentExecutionResult(self.output)

    def close(self) -> None:
        self.closes += 1
        self.lifecycle.append(f"close:{self.name}")


@dataclass
class RecordingAgentExecutorFactoryV2:
    provider: str
    revision: str
    operational_identity_value: str
    output: bytes
    lifecycle: list[str] = field(default_factory=list)
    key_reads: int = 0
    identity_reads: int = 0
    opens: int = 0
    opened: RecordingAgentExecutorV2 | None = None

    @property
    def key(self) -> AgentExecutorKey:
        self.key_reads += 1
        return AgentExecutorKey(
            ProviderId(self.provider), AgentExecutorRevision(self.revision)
        )

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        self.identity_reads += 1
        return AgentExecutorOperationalIdentity(self.operational_identity_value)

    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        self.opened = RecordingAgentExecutorV2(
            self.output, [], self.lifecycle, self.provider
        )
        return self.opened
