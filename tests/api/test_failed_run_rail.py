"""List and event doors name the same rail for one FAILED run."""

from __future__ import annotations

import pytest

from atelier2.api.projection.runs import node_rail_resources, run_resource
from atelier2.api.wire.resources import RunResourceV3
from atelier2.application.project_node_rail import project_node_rail
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.run_projections import NodeState, PublicAgentAttemptState
from tests.domain.test_node_rail import (
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
