from __future__ import annotations

from typing import cast

import pytest

from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptReplacement,
)
from atelier2.contracts.agents import AgentReceiptHash
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventCancellationBinding,
    RunEventKind,
    WaitAnswer,
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
        attempt_binding=RunEventCancellationBinding(
            AgentAttemptId("b" * 64),
            1,
            AgentAttemptReplacement.NONE,
            "cancel",
            AgentAttemptCancellationDisposition.REAPED_AFTER_TERM,
        ),
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


def _agent_completion(
    revision: WorkflowRevision,
    run_id: RunId,
    node_id: str,
    *,
    ordinal: int = 1,
    agent_receipt_hash: AgentReceiptHash | None = None,
) -> RunEvent:
    return RunEvent(
        run_id,
        revision.revision_hash,
        7,
        node_id,
        NodeExecutionId.for_node(run_id, revision.revision_hash, node_id),
        RunEventKind.AGENT_COMPLETED,
        b"5",
        attempt_binding=RunEventAgentAttemptBinding(AgentAttemptId("b" * 64), ordinal),
        agent_receipt_hash=agent_receipt_hash,
    )


@pytest.mark.proves("an-agent-completion-without-a-receipt-binding-keeps-its-hash")
def test_an_agent_completion_without_a_receipt_binding_keeps_its_frozen_hash() -> None:
    """The freeze vector: what the chain said before v3 exists, it still says.

    Measured against `main` 8d56658 before the domain was added, so the literal
    is the predecessor's own answer rather than this head's.
    """
    revision = WorkflowRevision(b"format_version: 1\nstart: agent\nnodes: []\n")
    run_id = RunId("r\N{LATIN SMALL LETTER U WITH ACUTE}n-1")

    event = _agent_completion(
        revision, run_id, "n\N{LATIN SMALL LETTER O WITH ACUTE}de"
    )

    assert event.agent_receipt_hash is None
    assert event.event_hash.value == (
        "3e2e7a04215f832950fa53430b9b97ba72bf1c3a6b39e92e6228636a11fd5422"
    )
    assert terminal_hash_for(revision.revision_hash, (event.event_hash,)).value == (
        "2d265fadf195e51404dda804b83bfdfaa998f7711625219d353bfb8c65be7568"
    )


def test_a_bound_agent_completion_carries_its_receipt_into_the_terminal_hash() -> None:
    """The binding vector: the receipt hash is a position in the preimage.

    Strike `agent_receipt_hash` out of the preimage and the two events below
    collapse onto one hash, which is exactly the fraud an offline recomputation
    has to see.
    """
    revision = WorkflowRevision(b"format_version: 1\nstart: agent\nnodes: []\n")
    run_id = RunId("r\N{LATIN SMALL LETTER U WITH ACUTE}n-1")
    node_id = "n\N{LATIN SMALL LETTER O WITH ACUTE}de"

    bound = _agent_completion(
        revision, run_id, node_id, agent_receipt_hash=AgentReceiptHash("c" * 64)
    )
    otherwise_bound = _agent_completion(
        revision, run_id, node_id, agent_receipt_hash=AgentReceiptHash("d" * 64)
    )

    assert bound.event_hash.value == (
        "03fd0bb92cc6383fc52d37ea2b1f1a2635b359219fe67bf81594d282372aa202"
    )
    assert terminal_hash_for(revision.revision_hash, (bound.event_hash,)).value == (
        "59d2d2703a2f2288e2ee8daf12067816d904b51e72c88951f4f4cf97be6a1f1b"
    )
    assert bound.event_hash != otherwise_bound.event_hash
    assert terminal_hash_for(
        revision.revision_hash, (bound.event_hash,)
    ) != terminal_hash_for(revision.revision_hash, (otherwise_bound.event_hash,))


def test_a_bound_completion_still_distinguishes_the_attempt_it_ran_as() -> None:
    """v3 nests v2: binding a receipt never drops the attempt dimension.

    A retry carries the same receipt when it produced the same answer under the
    same binding, so the ordinal is what keeps the second run visible.
    """
    revision = WorkflowRevision(b"format_version: 1\nstart: agent\nnodes: []\n")
    run_id = RunId("run")
    receipt_hash = AgentReceiptHash("c" * 64)

    first = _agent_completion(
        revision, run_id, "node", ordinal=1, agent_receipt_hash=receipt_hash
    )
    retry = _agent_completion(
        revision, run_id, "node", ordinal=2, agent_receipt_hash=receipt_hash
    )

    assert first.event_hash != retry.event_hash


@pytest.mark.parametrize(
    "event_kind",
    [
        RunEventKind.AGENT_FAILED,
        RunEventKind.ACTION_RECONCILIATION_REQUIRED,
        RunEventKind.WAITING_INPUT,
        RunEventKind.WAIT_ANSWERED,
        RunEventKind.SUBWORKFLOW_COMPLETED,
    ],
)
def test_only_an_agent_completion_may_carry_a_receipt_hash(
    event_kind: RunEventKind,
) -> None:
    revision = WorkflowRevision(b"workflow")
    run_id = RunId("run")
    execution = NodeExecutionId.for_node(run_id, revision.revision_hash, "node")
    attempt_bound = event_kind is RunEventKind.AGENT_FAILED

    with pytest.raises(ValueError, match="receipt hash"):
        RunEvent(
            run_id,
            revision.revision_hash,
            1,
            "node",
            execution,
            event_kind,
            b"result",
            attempt_binding=(
                RunEventAgentAttemptBinding(AgentAttemptId("b" * 64), 1)
                if attempt_bound
                else None
            ),
            agent_receipt_hash=AgentReceiptHash("c" * 64),
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
