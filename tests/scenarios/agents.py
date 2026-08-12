from __future__ import annotations

from typing import Any

from atelier2.adapters.dbos.run_store import commit_agent_completed, load_graph
from atelier2.adapters.exact_output_agent import EXACT_OUTPUT_EXECUTOR_BINDING
from atelier2.contracts.agents import (
    AgentExecutionRequest,
    AgentExecutionResult,
    ExactOutputContract,
)
from atelier2.contracts.executions import NodeExecutionId, TransitionSnapshot
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflows import AgentNode


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
