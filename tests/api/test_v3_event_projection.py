"""A format-3 event answers in the shape that says so.

A V3 run projected its events through the V1 mapping: the resource named no
format and carried no node rail, so a cockpit reading the stream of a run it had
just started could not tell which family it was looking at, nor where the run
stood after the event it just received. That is the wire half of #238 -- the
click into a node needs an event stream that speaks about the run it belongs to.
"""

from __future__ import annotations

import pytest

from atelier2.api.projection.events import run_event_resource
from atelier2.api.wire.events import (
    AgentCompletedEventResource,
    AgentCompletedEventResourceV2,
    AgentCompletedEventResourceV3,
)
from atelier2.api.wire.resources import NodeRailResource
from atelier2.contracts.run_projections import NodeState
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.run_events import PersistedRunEvent
from atelier2.contracts.runs import RunId, WorkflowRevisionHash

RUN = RunId("v3/events")
REVISION = WorkflowRevisionHash("a" * 64)
RAIL = (NodeRailResource(node_id="implement", state=NodeState.SUCCEEDED, attempt=None),)


def agent_completed(format_version: int) -> PersistedRunEvent:
    """One durable agent completion of a run of that family."""
    return PersistedRunEvent(
        event=RunEvent(
            run_id=RUN,
            revision_hash=REVISION,
            event_sequence=1,
            node_id="implement",
            node_execution_id=NodeExecutionId.for_node(RUN, REVISION, "implement"),
            event_kind=RunEventKind.AGENT_COMPLETED,
            payload=b"the exact provider answer",
            agent_attempt_id=Sha256Hash.of(b"attempt").value,
            attempt_ordinal=1,
        ),
        workflow_format_version=format_version,
        receipt=None,
    )


@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
@pytest.mark.parametrize(
    ("format_version", "expected"),
    (
        (1, AgentCompletedEventResource),
        (2, AgentCompletedEventResourceV2),
        (3, AgentCompletedEventResourceV3),
    ),
    ids=("v1", "v2", "v3"),
)
def test_each_family_answers_in_its_own_event_shape(
    format_version: int, expected: type
) -> None:
    """Three families, three shapes -- and format 3 stops borrowing V1's.

    The V1 and V2 answers are asserted beside it on purpose: this head widens the
    mapping and must not move either of the shapes a reader already depends on.
    """
    resource = run_event_resource(agent_completed(format_version), RAIL)

    assert isinstance(resource, expected)


@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
def test_a_v3_event_names_its_format_and_carries_the_rail() -> None:
    """What the V1 fall-through dropped, measured on the fields a reader gets."""
    resource = run_event_resource(agent_completed(3), RAIL)
    fields = resource.model_dump()

    assert fields["workflow_format_version"] == 3
    assert tuple(entry["node_id"] for entry in fields["node_rail"]) == ("implement",)
    assert fields["node_id"] == "implement"
    assert fields["output_hash"] == Sha256Hash.of(b"the exact provider answer").value


@pytest.mark.proves("a-format-three-event-answers-in-the-shape-that-says-so")
def test_a_v3_run_cannot_answer_with_a_kind_its_nodes_never_write() -> None:
    """A V3 node is an Agent, so a subworkflow completion is a store that lies."""
    subworkflow = PersistedRunEvent(
        event=RunEvent(
            run_id=RUN,
            revision_hash=REVISION,
            event_sequence=1,
            node_id="done",
            node_execution_id=NodeExecutionId.for_node(RUN, REVISION, "done"),
            event_kind=RunEventKind.SUBWORKFLOW_COMPLETED,
            payload=b"5",
        ),
        workflow_format_version=3,
        receipt=None,
    )

    with pytest.raises(ValueError, match="no exact attempt binding"):
        run_event_resource(subworkflow, RAIL)
