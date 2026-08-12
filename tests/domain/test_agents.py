from __future__ import annotations

from dataclasses import fields, replace

import pytest

from atelier2.adapters.exact_output_agent import (
    EXACT_OUTPUT_EXECUTOR_BINDING,
    ExactOutputAgentExecutorFactory,
)
from atelier2.contracts.agents import (
    AgentExecutionRequest,
    AgentExecutionResult,
    AgentExecutorBinding,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentReceipt,
    ExactOutputContract,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
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


def test_request_and_receipt_hashes_pin_every_exact_bound_value() -> None:
    execution_request = request()
    result = AgentExecutionResult(b"candidate bytes")

    receipt = AgentReceipt.for_execution(
        execution_request, EXACT_OUTPUT_EXECUTOR_BINDING, result
    )

    assert execution_request.request_hash.value == (
        "e0a801df89bd1a60d5ef80401212dc7e60ec3456cb9da39377f6cbbaddc47d98"
    )
    assert receipt.output_hash.value == (
        "732d058fadd90c70f22429227ab5d9c74919217099efe737aa46835ce3a60856"
    )
    assert receipt.receipt_hash.value == (
        "f19039b31c55f45c9bd4284d89f43000f6b428a5f8060ca762ebf5c8185c8274"
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


def test_agent_receipt_is_not_an_action_or_provider_auth_record() -> None:
    assert {field.name for field in fields(AgentReceipt)} == {
        "request_hash",
        "node_execution_id",
        "run_id",
        "workflow_revision_hash",
        "node_id",
        "executor_binding",
        "output_bytes",
        "output_hash",
        "receipt_hash",
    }


def test_durable_receipt_rejects_changed_bytes_or_hashes() -> None:
    receipt = AgentReceipt.for_execution(
        request(),
        EXACT_OUTPUT_EXECUTOR_BINDING,
        AgentExecutionResult(b"candidate bytes"),
    )

    with pytest.raises(ValueError, match="output hash"):
        replace(receipt, output_bytes=b"changed bytes")
    with pytest.raises(ValueError, match="receipt hash"):
        replace(receipt, receipt_hash=type(receipt.receipt_hash)("0" * 64))
    with pytest.raises(ValueError, match="receipt hash"):
        replace(
            receipt, request_hash=type(receipt.request_hash)(Sha256Hash.of(b"x").value)
        )


@pytest.mark.parametrize(
    "binding",
    [
        AgentExecutorBinding(
            AgentExecutorRevision("exact-output/v2"),
            AgentExecutorOperationalIdentity("format-version-1"),
        ),
        AgentExecutorBinding(
            AgentExecutorRevision("exact-output/v1"),
            AgentExecutorOperationalIdentity("other-operation"),
        ),
    ],
)
def test_receipt_hash_changes_with_executor_binding(
    binding: AgentExecutorBinding,
) -> None:
    original = AgentReceipt.for_execution(
        request(),
        EXACT_OUTPUT_EXECUTOR_BINDING,
        AgentExecutionResult(b"candidate bytes"),
    )

    changed = AgentReceipt.for_execution(
        request(), binding, AgentExecutionResult(b"candidate bytes")
    )

    assert changed.receipt_hash != original.receipt_hash
