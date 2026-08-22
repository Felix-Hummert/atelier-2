"""List and event doors name the same rail for one FAILED run."""

from __future__ import annotations

import pytest

from atelier2.api.projection.runs import node_rail_resources, run_resource
from atelier2.api.wire.resources import RunResourceV3
from atelier2.application.project_node_rail import project_node_rail
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
)
from atelier2.contracts.executions import NodeExecutionId, RunEventKind
from atelier2.contracts.run_projections import (
    AgentAttemptCancellationProjection,
    AgentAttemptProjection,
    NodeState,
    PublicAgentAttemptState,
)
from tests.domain.test_node_rail import (
    REQUEST_HASH,
    REVISION_HASH,
    RUN_ID,
    agent_attempt,
    v3_agent_event,
    v3_failed_projection,
)


@pytest.mark.proves("a-failed-run-list-and-events-name-the-same-node")
def test_list_and_events_name_the_same_failed_node_rail() -> None:
    """The measurement on #393: same run, two doors, one node state.

    The list projection used to answer working/attempt null while the event
    stream of that same FAILED run answered failed with attempt 1 FAILED.
    """
    projection = v3_failed_projection(
        (agent_attempt(1, PublicAgentAttemptState.FAILED, "implement"),)
    )
    event = v3_agent_event(RunEventKind.AGENT_FAILED)

    resource = run_resource(projection)
    assert isinstance(resource, RunResourceV3)
    listed = resource.node_rail
    streamed = node_rail_resources(project_node_rail(projection, (event,)))

    assert listed == streamed
    assert [(entry.node_id, entry.state, entry.attempt) for entry in listed] == [
        (
            "implement",
            NodeState.FAILED,
            listed[0].attempt,
        ),
        ("review", NodeState.QUEUED, None),
    ]
    assert listed[0].attempt is not None
    assert listed[0].attempt.ordinal == 1
    assert listed[0].attempt.state == PublicAgentAttemptState.FAILED


@pytest.mark.proves("a-bound-unstarted-run-refuses-when-its-executor-is-unavailable")
def test_attemptless_failure_keeps_the_failed_rail_free_of_an_invented_attempt() -> (
    None
):
    projection = v3_failed_projection()
    event = v3_agent_event(RunEventKind.AGENT_FAILED, attempt_ordinal=None)

    listed = run_resource(projection)
    assert isinstance(listed, RunResourceV3)
    streamed = node_rail_resources(project_node_rail(projection, (event,)))

    assert [
        (entry.node_id, entry.state, entry.attempt) for entry in listed.node_rail
    ] == [
        ("implement", NodeState.FAILED, None),
        ("review", NodeState.QUEUED, None),
    ]
    assert streamed == listed.node_rail


def _never_launched_cleanup_attempt() -> AgentAttemptProjection:
    execution_id = NodeExecutionId.for_node(RUN_ID, REVISION_HASH, "implement")
    return AgentAttemptProjection(
        AgentAttemptId.for_execution(execution_id, REQUEST_HASH, 1),
        execution_id,
        REQUEST_HASH,
        1,
        PublicAgentAttemptState.CANCELLED,
        None,
        AgentAttemptCancellationProjection(
            "agent-executor-binding-unavailable:cleanup",
            AgentAttemptReplacement.NONE,
            AgentAttemptRedriveState.CLEANUP_ATTESTED,
            AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
        ),
    )


@pytest.mark.proves("a-bound-unstarted-run-refuses-when-its-executor-is-unavailable")
def test_never_launched_cleanup_on_a_failed_run_is_attemptless_failure() -> None:
    projection = v3_failed_projection((_never_launched_cleanup_attempt(),))
    event = v3_agent_event(RunEventKind.AGENT_FAILED, attempt_ordinal=None)

    listed = run_resource(projection)
    assert isinstance(listed, RunResourceV3)
    streamed = node_rail_resources(project_node_rail(projection, (event,)))

    assert [
        (entry.node_id, entry.state, entry.attempt) for entry in listed.node_rail
    ] == [
        ("implement", NodeState.FAILED, None),
        ("review", NodeState.QUEUED, None),
    ]
    assert streamed == listed.node_rail
