"""A declared agent refusal is the second truth, never a verdict or a schema miss."""

from __future__ import annotations

import json

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.schema import agent_attempts, node_receipts_v3, runs
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode, AgentAttemptState
from atelier2.contracts.agent_refusals import AGENT_REFUSAL_FIELD, agent_refusal_reason
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.node_records_v3 import NodeReceiptReason
from atelier2.contracts.runs import RunState
from atelier2.contracts.stored_node_receipt_reasons import (
    read_stored_node_receipt_reason,
)
from atelier2.ports.agent_attempts import AgentAttemptFailed
from tests.integration.test_v3_output_enforcement import (
    PLAN_SCHEMA,
    THE_ANSWER_THE_SCHEMA_ADMITS,
    THE_ANSWER_THE_SCHEMA_REFUSES,
    armed_attempt,
    durable_answer,
    planning_document,
)
from tests.integration.test_v3_output_enforcement import (
    runtime as output_contract_runtime,
)

runtime = output_contract_runtime

REASON = "Auftrag unklar, weil X"
REFUSAL_BYTES = json.dumps({AGENT_REFUSAL_FIELD: REASON}, separators=(",", ":")).encode(
    "ascii"
)


def refusing_document() -> bytes:
    return planning_document(PLAN_SCHEMA).replace(
        f"          revision: {PLAN_SCHEMA.revision_hash.value}\n".encode(),
        (
            f"          revision: {PLAN_SCHEMA.revision_hash.value}\n"
            "        refusal: {reason: required}\n"
        ).encode(),
        1,
    )


@pytest.mark.proves("a-node-may-declare-a-named-agent-refusal")
def test_the_product_owns_the_refusal_reason_sentence() -> None:
    assert agent_refusal_reason(REFUSAL_BYTES) == REASON
    assert agent_refusal_reason(THE_ANSWER_THE_SCHEMA_ADMITS) is None
    assert agent_refusal_reason(THE_ANSWER_THE_SCHEMA_REFUSES) is None


@pytest.mark.proves("a-declared-refusal-ends-the-attempt-as-agent-refused")
def test_a_declared_refusal_ends_the_attempt_and_the_run(runtime) -> None:
    execution = armed_attempt(runtime, refusing_document())
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(execution, AgentExecutionResult(REFUSAL_BYTES))

    assert isinstance(outcome, AgentAttemptFailed), outcome
    assert outcome.attempt.failure_code is AgentAttemptFailureCode.AGENT_REFUSED
    assert durable_answer(runtime) == (
        0,
        1,
        RunState.FAILED.value,
        AgentAttemptState.FAILED.value,
    )
    with runtime.engine.connect() as connection:
        stored = connection.scalar(sa.select(node_receipts_v3.c.reason))
        words, _schema, _value = read_stored_node_receipt_reason(str(stored))
        assert words.startswith(NodeReceiptReason.AGENT_REFUSED.value)
        assert REASON in words
        assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 1
        assert connection.scalar(sa.select(agent_attempts.c.failure_code)) == (
            AgentAttemptFailureCode.AGENT_REFUSED.value
        )


@pytest.mark.proves(
    "success-and-schema-refusal-stay-the-same-when-refusal-is-undeclared"
)
def test_undeclared_refusal_shaped_bytes_are_still_a_schema_miss(runtime) -> None:
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(execution, AgentExecutionResult(REFUSAL_BYTES))

    assert isinstance(outcome, AgentAttemptFailed)
    assert outcome.attempt.failure_code is AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED


@pytest.mark.proves(
    "success-and-schema-refusal-stay-the-same-when-refusal-is-undeclared"
)
def test_a_declared_refusal_still_accepts_the_success_schema(runtime) -> None:
    execution = armed_attempt(runtime, refusing_document())
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_ADMITS)
    )

    assert not isinstance(outcome, AgentAttemptFailed)
    assert durable_answer(runtime)[2] != RunState.FAILED.value


@pytest.mark.proves("a-declared-refusal-ends-the-attempt-as-agent-refused")
def test_the_refusal_door_is_what_names_agent_refused(
    runtime, monkeypatch: pytest.MonkeyPatch
) -> None:
    execution = armed_attempt(runtime, refusing_document())
    monkeypatch.setattr(
        "atelier2.adapters.dbos.agent_attempt_store.agent_refusal_reason",
        lambda _bytes: None,
    )
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)

    outcome = store.complete_success(execution, AgentExecutionResult(REFUSAL_BYTES))

    assert isinstance(outcome, AgentAttemptFailed)
    assert outcome.attempt.failure_code is AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED
