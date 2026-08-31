from __future__ import annotations

from dataclasses import replace

import pytest

from atelier2.adapters.exact_output_agent import (
    EXACT_OUTPUT_EXECUTOR_BINDING,
    ExactOutputAgentExecutorFactory,
)
from atelier2.contracts.agents import (
    AgentExecutionRequest,
    AgentExecutionResult,
    ExactOutputContract,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import RunId, WorkflowRevision


def request() -> AgentExecutionRequest:
    run_id = RunId("run-17")
    revision_hash = WorkflowRevision(b"workflow-v1").revision_hash
    node_id = "draft"
    return AgentExecutionRequest(
        NodeExecutionId.for_node(run_id, revision_hash, node_id),
        run_id,
        revision_hash,
        node_id,
        b"write the change",
        ExactOutputContract(b"candidate bytes"),
    )


def test_request_hash_pins_every_exact_bound_value() -> None:
    assert request().request_hash.value == (
        "e0a801df89bd1a60d5ef80401212dc7e60ec3456cb9da39377f6cbbaddc47d98"
    )


@pytest.mark.parametrize(
    "changed",
    [
        lambda value: replace(value, job_bytes=b"review the change"),
        lambda value: replace(
            value, exact_output=ExactOutputContract(b"different bytes")
        ),
    ],
    ids=["job", "exact-output"],
)
def test_request_hash_changes_with_each_exact_contract_value(changed) -> None:
    original = request()

    assert changed(original).request_hash != original.request_hash


def test_request_rejects_a_node_that_contradicts_its_execution_identity() -> None:
    with pytest.raises(ValueError, match="execution identity"):
        replace(request(), node_id="other")


def test_exact_output_executor_returns_only_the_bound_contract() -> None:
    factory = ExactOutputAgentExecutorFactory()
    executor = factory.open()
    try:
        assert factory.binding == EXACT_OUTPUT_EXECUTOR_BINDING
        assert executor.execute(request()) == AgentExecutionResult(b"candidate bytes")
    finally:
        executor.close()
