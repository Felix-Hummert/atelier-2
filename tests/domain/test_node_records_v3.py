from __future__ import annotations

from typing import cast

import pytest

from atelier2.contracts.agents import AgentExecutionCapability
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.node_records_v3 import (
    AvailableContextGrant,
    BoundNodeRevisions,
    ContextPackage,
    DeclaredOutput,
    InputEnvelope,
    NodeArtifact,
    NodeExecutionRequest,
    NodeKindV3,
    NodeReceipt,
    PersistedReceiptDisposition,
    ProjectedDeliveryStatus,
    ReceiptOutput,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_configuration_v3 import RunConfigurationRevisionHash
from atelier2.contracts.runs import RunId, WorkflowRevisionHash

WORKFLOW = WorkflowRevisionHash("aa" * 32)
RUN_CONFIGURATION = RunConfigurationRevisionHash("bb" * 32)
SCHEMA = PublishedRevisionHash("cc" * 32)
SOURCE = PublishedRevisionHash("dd" * 32)
READ_OPERATION = PublishedRevisionHash("ee" * 32)
VALUE = Sha256Hash("ff" * 32)
RUN = RunId("run-v3")
NODE = "build"
MANIFEST = b"context-manifest\x00"
PACKAGE_HASH = "6876523ded4e24ff8cd5a8b6a7adc591aab480fdb1e2ea231369bc56e24d9c31"
REQUEST_HASH = "9aa2193b57878473d03e616430a8b70da18063db4d51f25fc7ef034b0eb3a701"
ARTIFACT_HASH = "6dfc8742c2ea8d216481dafb31294010c0586c4cbcb87a6641ee4f73e816cd10"
RECEIPT_HASH = "083f44422e23ceb43188464c4327e580dd77454716a21636972de556e7f760ec"
RESULT_BYTES = b"result-bytes"
RESULT_VALUE_HASH = "4796ef914c847f1124994597d49d513b96882c4176bf1ccb0bc4f4c5b18ee95a"


def _package() -> ContextPackage:
    return ContextPackage(MANIFEST)


def _request(
    *,
    context_package_hash: str | None = None,
    kind: NodeKindV3 = NodeKindV3.AGENT,
    mode: AgentExecutionCapability | None = AgentExecutionCapability.HEADLESS,
    node_id: str = NODE,
) -> NodeExecutionRequest:
    package_hash = _package().package_hash
    if context_package_hash is not None:
        from atelier2.contracts.node_records_v3 import ContextPackageHash

        package_hash = ContextPackageHash(context_package_hash)
    return NodeExecutionRequest(
        WORKFLOW,
        RUN_CONFIGURATION,
        RUN,
        node_id,
        package_hash,
        (AvailableContextGrant("notes", SOURCE, (READ_OPERATION,)),),
        kind,
        mode,
        (InputEnvelope(ProjectedDeliveryStatus.SUCCEEDED, "draft", SCHEMA, VALUE),),
        BoundNodeRevisions(
            agent_configuration=PublishedRevisionHash("11" * 32),
            profile=PublishedRevisionHash("22" * 32),
            budget=PublishedRevisionHash("33" * 32),
        ),
        (DeclaredOutput("result", SCHEMA),),
    )


def _execution() -> NodeExecutionId:
    return NodeExecutionId.for_node(RUN, WORKFLOW, NODE)


def test_context_package_hash_is_the_literal_v3_vector() -> None:
    assert _package().package_hash.value == PACKAGE_HASH


def test_changing_context_package_bytes_changes_the_hash() -> None:
    assert ContextPackage(b"context-manifest\x01").package_hash.value != PACKAGE_HASH


def test_node_execution_request_hash_is_the_literal_v3_vector() -> None:
    assert _request().request_hash.value == REQUEST_HASH


@pytest.mark.parametrize(
    "builder",
    (
        lambda: _request(context_package_hash=("00" * 32)),
        lambda: _request(node_id="other"),
        lambda: _request(
            kind=NodeKindV3.ACTION,
            mode=None,
        ),
    ),
    ids=("context", "node", "kind"),
)
def test_changing_one_request_preimage_field_changes_the_hash(builder) -> None:
    assert builder().request_hash.value != REQUEST_HASH


def test_identical_request_retry_keeps_the_literal_hash() -> None:
    assert _request().request_hash == _request().request_hash


def test_agent_request_requires_a_mode_and_other_kinds_refuse_one() -> None:
    with pytest.raises(ValueError, match="mode"):
        _request(kind=NodeKindV3.AGENT, mode=None)
    with pytest.raises(ValueError, match="mode"):
        _request(kind=NodeKindV3.WAIT, mode=AgentExecutionCapability.HEADLESS)


def test_stale_is_a_delivery_status_and_not_a_persisted_disposition() -> None:
    envelope = InputEnvelope(ProjectedDeliveryStatus.STALE, "draft", SCHEMA, None)
    assert envelope.status is ProjectedDeliveryStatus.STALE
    assert "stale" not in {item.value for item in PersistedReceiptDisposition}
    with pytest.raises(TypeError):
        NodeReceipt(
            _execution(),
            cast(PersistedReceiptDisposition, "stale"),
            "projected",
            _request().request_hash,
            _package().package_hash,
            (),
        )


def test_node_artifact_hash_is_the_literal_v3_vector() -> None:
    artifact = NodeArtifact(RUN, NODE, _execution(), "result", SCHEMA, RESULT_BYTES)
    assert artifact.value_hash.value == RESULT_VALUE_HASH
    assert artifact.artifact_hash.value == ARTIFACT_HASH


def test_changing_artifact_value_bytes_changes_the_hash() -> None:
    changed = NodeArtifact(RUN, NODE, _execution(), "result", SCHEMA, b"other-bytes")
    assert changed.artifact_hash.value != ARTIFACT_HASH
    assert changed.value_hash.value != RESULT_VALUE_HASH


def test_succeeded_receipt_hash_is_the_literal_v3_vector() -> None:
    artifact = NodeArtifact(RUN, NODE, _execution(), "result", SCHEMA, RESULT_BYTES)
    receipt = NodeReceipt(
        _execution(),
        PersistedReceiptDisposition.SUCCEEDED,
        "completed",
        _request().request_hash,
        _package().package_hash,
        (ReceiptOutput("result", SCHEMA, artifact.value_hash),),
    )
    assert receipt.receipt_hash.value == RECEIPT_HASH


def test_failed_receipt_carries_no_output_values_and_differs() -> None:
    failed = NodeReceipt(
        _execution(),
        PersistedReceiptDisposition.FAILED,
        "schema_violation",
        _request().request_hash,
        _package().package_hash,
        (),
    )
    assert failed.receipt_hash.value != RECEIPT_HASH
    with pytest.raises(ValueError, match="output"):
        NodeReceipt(
            _execution(),
            PersistedReceiptDisposition.FAILED,
            "schema_violation",
            _request().request_hash,
            _package().package_hash,
            (ReceiptOutput("result", SCHEMA, Sha256Hash(RESULT_VALUE_HASH)),),
        )


def test_receipt_refuses_a_wrong_request_hash_type() -> None:
    with pytest.raises(TypeError):
        NodeReceipt(
            _execution(),
            PersistedReceiptDisposition.BLOCKED,
            "dependency_failed",
            cast(object, PACKAGE_HASH),  # type: ignore[arg-type]
            _package().package_hash,
            (),
        )
