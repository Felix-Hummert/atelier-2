"""The stored receipt reason reaches the AGENT_FAILED event an operator reads."""

from __future__ import annotations

import pytest

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.api.projection.events import run_event_resource
from atelier2.api.projection.runs import node_rail_resources
from atelier2.api.wire.events import AgentFailedEventResourceV3
from atelier2.application.project_node_rail import project_node_rail
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.node_records_v3 import NodeReceiptReason
from atelier2.contracts.run_events import RunEventPage
from atelier2.ports.agent_attempts import AgentAttemptFailed
from atelier2.ports.agent_executions import AgentExecutionResult
from atelier2.ports.run_queries import RunFound
from tests.integration.test_v3_output_enforcement import (
    RUN,
    THE_ANSWER_THE_SCHEMA_REFUSES,
    armed_attempt,
)
from tests.integration.test_v3_output_enforcement import (
    runtime as output_contract_runtime,
)
from tests.scenarios.api import durable_queries

runtime = output_contract_runtime


@pytest.mark.proves("an-agent-failed-event-carries-the-stored-receipt-reason")
def test_an_agent_failed_event_carries_the_stored_receipt_reason(runtime) -> None:
    execution = armed_attempt(runtime)
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    outcome = store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
    )
    assert isinstance(outcome, AgentAttemptFailed), outcome
    assert outcome.attempt.failure_code is AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED

    queries = durable_queries(runtime.engine)
    page = queries.read_run_event_page(RUN, 0, 5)
    assert isinstance(page, RunEventPage)
    assert len(page.events) == 1
    persisted = page.events[0]
    assert persisted.node_receipt_reason is not None
    assert persisted.node_receipt_reason.startswith(
        f"{NodeReceiptReason.OUTPUT_SCHEMA_REFUSED.value}: "
    )

    found = queries.get_run(RUN)
    assert isinstance(found, RunFound)
    resource = run_event_resource(
        persisted,
        node_rail_resources(project_node_rail(found.projection, page.events)),
    )
    assert isinstance(resource, AgentFailedEventResourceV3)
    assert resource.failure_code == "OUTPUT_SCHEMA_REFUSED"
    assert resource.reason == persisted.node_receipt_reason
