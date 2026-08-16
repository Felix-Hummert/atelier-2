from __future__ import annotations

from typing import cast

import pytest

from atelier2.adapters.dbos.continuation import action_continuation_workflow_id_for
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventKind,
    WaitAnswer,
    answer_workflow_id_for,
    logical_effect_key_for,
    node_workflow_id_for,
    subworkflow_workflow_id_for,
    terminal_hash_for,
)
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.runs import RunId, WorkflowRevision


def test_exact_document_and_new_identity_preimages_are_frozen() -> None:
    document = b"format_version: 1\nstart: agent\nnodes: []\n"
    revision = WorkflowRevision(document)
    execution = NodeExecutionId.for_node(
        RunId("r\N{LATIN SMALL LETTER U WITH ACUTE}n-1"),
        revision.revision_hash,
        "n\N{LATIN SMALL LETTER O WITH ACUTE}de",
    )

    assert revision.document is document
    assert frame("test/v1", b"a", b"\x00\xff").hex() == (
        "4154454c494552320000000007746573742f7631000000000000000161000000000000000200ff"
    )
    assert (
        execution.value
        == "95774a97abe1effd7586c315d1a47edeef448d38581db617a5d67be7a57d226f"
    )
    assert node_workflow_id_for(execution) == (
        "atelier2-node-d5d705efd74694d64115cc72b7dcb22aea553e95a8b437d6fe533b6d590cc857"
    )
    assert logical_effect_key_for(execution).value == (
        "atelier2-node-effect-224a41804d7e4f4399b9a0ceb84e7b32332236759379d4b1f55f00f54c689aa4"
    )
    assert action_continuation_workflow_id_for(logical_effect_key_for(execution)) == (
        "atelier2-action-continuation-"
        "1e692016f71fa055dbefbd78f961ab9c764b63ece462ccb68aebeb8d275f51c4"
    )
    assert answer_workflow_id_for(execution) == (
        "atelier2-answer-1cf339c00cc0026bb17df09896739a7e48e675fe16523a430a687438d62a8ad6"
    )
    assert subworkflow_workflow_id_for(execution) == (
        "atelier2-subworkflow-"
        "30d5c6f76a0ec0b44275c438f8a269f75112dd69417852b938deef469cbee25b"
    )


def test_event_terminal_and_answer_hash_vectors_are_frozen() -> None:
    revision = WorkflowRevision(b"format_version: 1\nstart: agent\nnodes: []\n")
    run_id = RunId("r\N{LATIN SMALL LETTER U WITH ACUTE}n-1")
    node_id = "n\N{LATIN SMALL LETTER O WITH ACUTE}de"
    execution = NodeExecutionId.for_node(run_id, revision.revision_hash, node_id)
    event = RunEvent(
        run_id,
        revision.revision_hash,
        7,
        node_id,
        execution,
        RunEventKind.WAIT_ANSWERED,
        b"5",
    )
    answer = WaitAnswer(run_id, revision.revision_hash, node_id, execution, b"5")

    assert event.payload_hash.value == (
        "ef2d127de37b942baad06145e54b0c619a1f22327b2ebbcfbec78f5564afe39d"
    )
    assert event.event_hash.value == (
        "b5c2174a727207c18a0cd48475ab2a25cb9aa68908ca1900fe1cc057061beca8"
    )
    assert terminal_hash_for(revision.revision_hash, (event.event_hash,)).value == (
        "a940f984e32461923c96412fd5c041b222c1034c80c3f942665330bdb2cd1385"
    )
    assert answer.answer_hash.value == (
        "ef2d127de37b942baad06145e54b0c619a1f22327b2ebbcfbec78f5564afe39d"
    )
    assert answer.answer_workflow_id == (
        "atelier2-answer-1cf339c00cc0026bb17df09896739a7e48e675fe16523a430a687438d62a8ad6"
    )


def test_v2_cancellation_event_hash_vector_pins_attempt_ordinal() -> None:
    revision = WorkflowRevision(b"format_version: 1\nstart: agent\nnodes: []\n")
    run_id = RunId("r\N{LATIN SMALL LETTER U WITH ACUTE}n-1")
    node_id = "n\N{LATIN SMALL LETTER O WITH ACUTE}de"
    execution = NodeExecutionId.for_node(run_id, revision.revision_hash, node_id)
    event = RunEvent(
        run_id,
        revision.revision_hash,
        7,
        node_id,
        execution,
        RunEventKind.AGENT_CANCELLED,
        b"5",
        agent_attempt_id="b" * 64,
        attempt_ordinal=1,
        cancellation_command_id="cancel",
        replacement="NONE",
        cancellation_disposition="REAPED_AFTER_TERM",
    )

    assert event.event_hash.value == (
        "3114df5be5f3b9b80558d0f37b6faa64a96e47eb1fc827fae8c6eeddf8c53943"
    )


def test_event_hash_vector_with_receipt_fields_is_frozen() -> None:
    revision = WorkflowRevision(b"format_version: 1\nstart: agent\nnodes: []\n")
    run_id = RunId("r\N{LATIN SMALL LETTER U WITH ACUTE}n-1")
    node_id = "n\N{LATIN SMALL LETTER O WITH ACUTE}de"
    execution = NodeExecutionId.for_node(run_id, revision.revision_hash, node_id)

    event = RunEvent(
        run_id,
        revision.revision_hash,
        7,
        node_id,
        execution,
        RunEventKind.ACTION_COMPLETED,
        b"5",
        receipt_logical_key=LogicalEffectKey("receipt-key"),
        receipt_result_hash=Sha256Hash(
            "ef2d127de37b942baad06145e54b0c619a1f22327b2ebbcfbec78f5564afe39d"
        ),
    )

    assert event.event_hash.value == (
        "4b4bccd5c7537a9144cc8425762c8e1de702b65f6c2815c8e015076d2f2a4138"
    )


@pytest.mark.parametrize(
    "event_kind",
    [
        RunEventKind.ACTION_RECONCILIATION_RESOLVED,
        RunEventKind.ACTION_COMPLETED,
    ],
)
def test_receipt_events_accept_the_exact_payload_receipt_binding(
    event_kind: RunEventKind,
) -> None:
    revision = WorkflowRevision(b"workflow")
    run_id = RunId("run")
    execution = NodeExecutionId.for_node(run_id, revision.revision_hash, "action")
    payload = b"result"

    event = RunEvent(
        run_id,
        revision.revision_hash,
        1,
        "action",
        execution,
        event_kind,
        payload,
        receipt_logical_key=LogicalEffectKey("receipt-key"),
        receipt_result_hash=Sha256Hash.of(payload),
    )

    assert event.receipt_result_hash == event.payload_hash


@pytest.mark.parametrize(
    ("receipt_logical_key", "receipt_result_hash"),
    [
        pytest.param(None, None, id="both-missing"),
        pytest.param(LogicalEffectKey("receipt-key"), None, id="hash-missing"),
        pytest.param(
            None,
            Sha256Hash.of(b"result"),
            id="logical-key-missing",
        ),
        pytest.param(
            LogicalEffectKey("receipt-key"),
            Sha256Hash.of(b"different-result"),
            id="payload-hash-differs",
        ),
    ],
)
@pytest.mark.parametrize(
    "event_kind",
    [
        RunEventKind.ACTION_RECONCILIATION_RESOLVED,
        RunEventKind.ACTION_COMPLETED,
    ],
)
def test_receipt_events_reject_missing_or_different_receipt_bindings(
    event_kind: RunEventKind,
    receipt_logical_key: LogicalEffectKey | None,
    receipt_result_hash: Sha256Hash | None,
) -> None:
    revision = WorkflowRevision(b"workflow")
    run_id = RunId("run")
    execution = NodeExecutionId.for_node(run_id, revision.revision_hash, "action")

    with pytest.raises(ValueError, match="receipt"):
        RunEvent(
            run_id,
            revision.revision_hash,
            1,
            "action",
            execution,
            event_kind,
            b"result",
            receipt_logical_key=receipt_logical_key,
            receipt_result_hash=receipt_result_hash,
        )


@pytest.mark.parametrize(
    "event_kind",
    [
        RunEventKind.AGENT_COMPLETED,
        RunEventKind.ACTION_RECONCILIATION_REQUIRED,
        RunEventKind.WAITING_INPUT,
        RunEventKind.WAIT_ANSWERED,
        RunEventKind.SUBWORKFLOW_COMPLETED,
    ],
)
@pytest.mark.parametrize(
    ("receipt_logical_key", "receipt_result_hash"),
    [
        pytest.param(LogicalEffectKey("receipt-key"), None, id="key-only"),
        pytest.param(None, Sha256Hash.of(b"result"), id="hash-only"),
        pytest.param(
            LogicalEffectKey("receipt-key"),
            Sha256Hash.of(b"result"),
            id="both",
        ),
    ],
)
def test_nonreceipt_events_reject_receipt_bindings(
    event_kind: RunEventKind,
    receipt_logical_key: LogicalEffectKey | None,
    receipt_result_hash: Sha256Hash | None,
) -> None:
    revision = WorkflowRevision(b"workflow")
    run_id = RunId("run")
    execution = NodeExecutionId.for_node(run_id, revision.revision_hash, "node")

    with pytest.raises(ValueError, match="receipt"):
        RunEvent(
            run_id,
            revision.revision_hash,
            1,
            "node",
            execution,
            event_kind,
            b"result",
            receipt_logical_key=receipt_logical_key,
            receipt_result_hash=receipt_result_hash,
        )


@pytest.mark.parametrize("sequence", [True, 1.5, 0, -1])
def test_event_sequence_is_an_exact_positive_integer(sequence: object) -> None:
    revision = WorkflowRevision(b"workflow")
    run_id = RunId("run")
    execution = NodeExecutionId.for_node(run_id, revision.revision_hash, "node")

    with pytest.raises((TypeError, ValueError), match="sequence"):
        RunEvent(
            run_id,
            revision.revision_hash,
            cast(int, sequence),
            "node",
            execution,
            RunEventKind.WAITING_INPUT,
            b"",
        )


def test_event_rejects_an_execution_identity_for_another_node() -> None:
    revision = WorkflowRevision(b"workflow")
    run_id = RunId("run")
    wrong_execution = NodeExecutionId.for_node(
        run_id, revision.revision_hash, "another-node"
    )

    with pytest.raises(ValueError, match="execution"):
        RunEvent(
            run_id,
            revision.revision_hash,
            1,
            "node",
            wrong_execution,
            RunEventKind.WAITING_INPUT,
            b"",
        )


def test_answer_rejects_an_execution_identity_for_another_node() -> None:
    revision = WorkflowRevision(b"workflow")
    run_id = RunId("run")
    wrong_execution = NodeExecutionId.for_node(
        run_id, revision.revision_hash, "another-node"
    )

    with pytest.raises(ValueError, match="execution"):
        WaitAnswer(
            run_id,
            revision.revision_hash,
            "node",
            wrong_execution,
            b"5",
        )
