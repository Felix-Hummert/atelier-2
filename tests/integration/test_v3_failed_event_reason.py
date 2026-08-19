"""The stored receipt reason reaches the AGENT_FAILED event an operator reads."""

from __future__ import annotations

import pytest

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.api.projection.events import run_event_resource
from atelier2.api.projection.runs import node_rail_resources, run_resource
from atelier2.api.wire.events import AgentFailedEventResourceV3
from atelier2.api.wire.resources import RunResourceV3
from atelier2.application.project_node_rail import project_node_rail
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.node_records_v3 import NodeReceiptReason
from atelier2.contracts.run_events import RunEventPage
from atelier2.contracts.run_projections import (
    NodeState,
    PublicAgentAttemptState,
    RunPage,
)
from atelier2.ports.agent_attempts import AgentAttemptFailed
from atelier2.ports.agent_executions import AgentExecutionResult
from atelier2.ports.run_queries import RunFound
from tests.integration.test_v3_output_enforcement import (
    NODE,
    PLAN_SCHEMA,
    RUN,
    SUCCESSOR,
    THE_ANSWER_THE_SCHEMA_REFUSES,
    armed_attempt,
    reviewed_planning_document,
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


@pytest.mark.proves("a-failed-run-list-and-events-name-the-same-node")
def test_list_and_events_name_the_same_failed_node(runtime) -> None:
    """GET /runs and GET /runs/{ref}/events answer one node state for one death."""
    execution = armed_attempt(runtime, reviewed_planning_document(PLAN_SCHEMA))
    store = DbosAgentAttemptStore(runtime.engine, runtime.settings.application_version)
    outcome = store.complete_success(
        execution, AgentExecutionResult(THE_ANSWER_THE_SCHEMA_REFUSES)
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
        projection for projection in listed.runs if projection.run.run_id == RUN
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
    assert get_rail[0].attempt.ordinal == 1
    assert get_rail[0].attempt.state == PublicAgentAttemptState.FAILED
