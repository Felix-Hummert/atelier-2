"""The stored receipt reason reaches the AGENT_FAILED event an operator reads."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.schema import (
    agent_attempt_receipts_v3,
    agent_attempts,
    node_receipts_v3,
)
from atelier2.api.projection.events import run_event_resource
from atelier2.api.projection.runs import node_rail_resources, run_resource
from atelier2.api.wire.events import AgentFailedEventResourceV3
from atelier2.api.wire.resources import RunResourceV3
from atelier2.application.project_node_rail import project_node_rail
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS
from atelier2.contracts.artifacts import ArtifactHash
from atelier2.contracts.node_records_v3 import (
    NodeReceiptReason,
    read_stored_node_receipt_reason,
)
from atelier2.contracts.run_events import RunEventPage
from atelier2.contracts.run_projections import (
    NodeState,
    PublicAgentAttemptState,
    RunPage,
)
from atelier2.ports.agent_attempts import (
    NOTHING_TO_KEEP,
    AgentAttemptFailed,
    KeptEvidence,
    ProjectVerificationFailureEvidence,
)
from atelier2.ports.agent_executions import AgentExecutionResult
from atelier2.ports.run_queries import NodeDetailFound, RunFound
from tests.integration.test_redeemed_proof import redemption_for
from tests.integration.test_v3_output_enforcement import (
    NODE,
    PLAN_SCHEMA,
    RUN,
    SUCCESSOR,
    THE_ANSWER_THE_SCHEMA_ADMITS,
    THE_ANSWER_THE_SCHEMA_REFUSES,
    armed_attempt,
    repair_attempt,
    reviewed_planning_document,
)
from tests.integration.test_v3_output_enforcement import (
    runtime as output_contract_runtime,
)
from tests.scenarios.api import durable_queries, healthy_runs

runtime = output_contract_runtime

FIRST_REFUSED_ANSWER = THE_ANSWER_THE_SCHEMA_REFUSES
FIRST_REFUSAL_REASON = "output-schema-refused: instance-not-json: Expecting value"
SECOND_REFUSED_ANSWER = b'{"steps": "three"}'
SECOND_REFUSAL_REASON = (
    "output-schema-refused: schema-violated: /steps: is not of type 'integer'"
)


@pytest.mark.proves("an-agent-failed-event-carries-the-stored-receipt-reason")
def test_an_agent_failed_event_carries_the_stored_receipt_reason(runtime) -> None:
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    outcome = store.complete_success(
        execution, AgentExecutionResult(FIRST_REFUSED_ANSWER)
    )
    assert isinstance(outcome, AgentAttemptFailed), outcome
    assert outcome.attempt.failure_code is AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED
    repair = repair_attempt(runtime, execution)
    outcome = store.complete_success(
        repair, AgentExecutionResult(SECOND_REFUSED_ANSWER)
    )
    assert isinstance(outcome, AgentAttemptFailed), outcome

    with runtime.engine.connect() as connection:
        durable_reasons = tuple(
            connection.execute(
                sa.select(
                    agent_attempt_receipts_v3.c.attempt_id,
                    agent_attempts.c.attempt_ordinal,
                    agent_attempt_receipts_v3.c.reason,
                )
                .select_from(
                    agent_attempt_receipts_v3.join(
                        agent_attempts,
                        agent_attempt_receipts_v3.c.attempt_id
                        == agent_attempts.c.attempt_id,
                    )
                )
                .order_by(agent_attempts.c.attempt_ordinal)
            )
        )
    assert durable_reasons == (
        (execution.attempt_id.value, 1, FIRST_REFUSAL_REASON),
        (repair.attempt_id.value, 2, SECOND_REFUSAL_REASON),
    )

    queries = durable_queries(runtime.engine)
    page = queries.read_run_event_page(RUN, 0, 5)
    assert isinstance(page, RunEventPage)
    assert len(page.events) == 2
    assert tuple(
        (
            persisted.event.attempt_binding.attempt_id,
            persisted.event.attempt_binding.attempt_ordinal,
            persisted.node_receipt_reason,
        )
        for persisted in page.events
        if persisted.event.attempt_binding is not None
    ) == (
        (
            execution.attempt_id,
            1,
            FIRST_REFUSAL_REASON,
        ),
        (
            repair.attempt_id,
            2,
            SECOND_REFUSAL_REASON,
        ),
    )
    detail = queries.get_node_detail(RUN, NODE)
    assert isinstance(detail, NodeDetailFound)
    assert detail.detail.refusal == SECOND_REFUSAL_REASON

    found = queries.get_run(RUN)
    assert isinstance(found, RunFound)
    for persisted in page.events:
        assert persisted.node_receipt_reason is not None
        assert persisted.node_receipt_reason.startswith(
            f"{NodeReceiptReason.OUTPUT_SCHEMA_REFUSED.value}: "
        )
        assert len(persisted.node_receipt_reason) <= MAXIMUM_AGENT_FIELD_CHARACTERS
        resource = run_event_resource(
            persisted,
            node_rail_resources(project_node_rail(found.projection, page.events)),
        )
        assert isinstance(resource, AgentFailedEventResourceV3)
        assert resource.failure_code == "OUTPUT_SCHEMA_REFUSED"
        assert resource.reason == persisted.node_receipt_reason


@pytest.mark.proves("a-failed-run-list-and-events-name-the-same-node")
def test_list_and_events_name_the_same_failed_node(runtime) -> None:
    """GET /runs and GET /runs/{ref}/events answer one node state for one death."""
    execution = armed_attempt(runtime, reviewed_planning_document(PLAN_SCHEMA))
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    outcome = store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )
    assert isinstance(outcome, AgentAttemptFailed), outcome
    repair = repair_attempt(runtime, execution)
    outcome = store.complete_success(
        repair, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )
    assert isinstance(outcome, AgentAttemptFailed), outcome

    queries = durable_queries(runtime.engine)
    found = queries.get_run(RUN)
    listed = queries.list_runs(None, 10)
    page = queries.read_run_event_page(RUN, 0, 5)
    assert isinstance(found, RunFound)
    assert isinstance(listed, RunPage)
    assert isinstance(page, RunEventPage)
    listed_projection = next(
        projection
        for projection in healthy_runs(listed)
        if projection.run.run_id == RUN
    )

    get_resource = run_resource(found.projection)
    list_resource = run_resource(listed_projection)
    assert isinstance(get_resource, RunResourceV3)
    assert isinstance(list_resource, RunResourceV3)
    get_rail = get_resource.node_rail
    list_rail = list_resource.node_rail
    event_rail = node_rail_resources(project_node_rail(found.projection, page.events))

    assert get_rail == list_rail == event_rail
    assert [(entry.node_id, entry.state, entry.attempt) for entry in get_rail] == [
        (
            NODE,
            NodeState.FAILED,
            get_rail[0].attempt,
        ),
        (SUCCESSOR, NodeState.QUEUED, None),
    ]
    assert get_rail[0].attempt is not None
    assert get_rail[0].attempt.ordinal == 2
    assert get_rail[0].attempt.state == PublicAgentAttemptState.FAILED


def _stored_verification_failure_words(runtime, execution) -> str:
    """The composed `PROJECT_VERIFICATION_FAILED` reason this attempt's node
    receipt carries, unwrapped from its judged JSON envelope."""

    with runtime.engine.connect() as connection:
        stored_reason = connection.scalar(
            sa.select(node_receipts_v3.c.reason).where(
                node_receipts_v3.c.node_execution_id
                == execution.request.node_execution_id.value
            )
        )
    words, _schema_revision, _value_hash = read_stored_node_receipt_reason(
        str(stored_reason)
    )
    return words


@pytest.mark.proves("a-red-verifications-output-is-kept-as-a-readable-artifact")
def test_a_redacted_verification_tails_words_say_so_in_the_stored_reason(
    runtime,
) -> None:
    """The durable reason names a redaction, not just the fields that caused it.

    `test_project_verification.py` already proves `execute_agent_attempt`
    computes `ProjectVerificationFailureEvidence(redacted=True)` for a tail
    carrying a credential shape; this proves what the store composes from
    that evidence into the sentence an operator actually reads.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    evidence = ProjectVerificationFailureEvidence(
        "1 failed, 3 passed in 0.01s",
        0.5,
        KeptEvidence(ArtifactHash.of(b"the redacted tail"), redacted=True),
        NOTHING_TO_KEEP,
    )

    outcome = store.complete_success(
        execution,
        AgentExecutionResult(THE_ANSWER_THE_SCHEMA_ADMITS),
        redemption_for(execution, 1),
        evidence,
    )

    assert isinstance(outcome, AgentAttemptFailed)
    assert (
        outcome.attempt.failure_code
        is AgentAttemptFailureCode.PROJECT_VERIFICATION_FAILED
    )
    assert "output redacted" in _stored_verification_failure_words(runtime, execution)


@pytest.mark.proves("a-red-verifications-output-is-kept-as-a-readable-artifact")
def test_a_verification_tail_that_could_not_be_kept_names_the_reason_in_the_stored_reason(
    runtime,
) -> None:
    """The degrade path's own words, not just that `retention_failure` was set.

    `test_project_verification.py` already proves `execute_agent_attempt`
    degrades the words instead of abandoning the attempt when a wired
    publisher answers but cannot write; this proves the durable sentence the
    store composes from that degrade names the reason, not only the exit code.
    """
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    evidence = ProjectVerificationFailureEvidence(
        "1 failed, 3 passed in 0.01s",
        0.5,
        KeptEvidence(None, retention_failure="artifact write unavailable"),
        NOTHING_TO_KEEP,
    )

    outcome = store.complete_success(
        execution,
        AgentExecutionResult(THE_ANSWER_THE_SCHEMA_ADMITS),
        redemption_for(execution, 1),
        evidence,
    )

    assert isinstance(outcome, AgentAttemptFailed)
    words = _stored_verification_failure_words(runtime, execution)
    assert "output could not be kept: artifact write unavailable" in words
