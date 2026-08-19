from __future__ import annotations

import asyncio
from typing import Never

import pytest
from fastapi.sse import ServerSentEvent

from atelier2.api.stream import (
    BoundedQueryRunner,
    PreparedEventStream,
    stream_server_events,
)
from atelier2.api.wire.events import AgentCompletedEventResourceV2
from atelier2.api.wire.resources import NodeRailAttemptResource, NodeRailResource
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_events import (
    PersistedRunEvent,
    RunEventPage,
)
from atelier2.contracts.run_projections import (
    NodeState,
    PublicAgentAttemptState,
    RunProjection,
)
from atelier2.contracts.runs import RunId
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.run_events import (
    PrepareRunEventStreamResult,
)
from tests.api.test_agent_attempts import v2_run_projection
from tests.scenarios.api import (
    api_limits,
    event_poll_backoff,
    stream_page_reader,
    unused_attention_event_page,
)


class OnePageOfEvents:
    """A stream source that answers with one page and then says it ended."""

    def __init__(self, events: tuple[PersistedRunEvent, ...]) -> None:
        self._events = events

    def prepare_run_event_stream(
        self, run_id: RunId, after_sequence: int
    ) -> PrepareRunEventStreamResult:
        del run_id, after_sequence
        raise AssertionError("this scenario prepares its stream by hand")

    def read_run_event_page(
        self,
        run_id: object,
        after_sequence: int,
        limit: int,
        projection_limit: object | None = None,
    ) -> RunEventPage:
        del run_id, after_sequence, limit, projection_limit
        return RunEventPage(self._events, True)

    def read_attention_event_page(
        self,
        after_run_id: RunId | None,
        after_sequence: int | None,
        limit: int,
    ) -> Never:
        return unused_attention_event_page(after_run_id, after_sequence, limit)


def agent_completed(projection: RunProjection) -> PersistedRunEvent:
    run = projection.run
    execution_id = NodeExecutionId.for_node(run.run_id, run.revision_hash, "build")
    return PersistedRunEvent(
        RunEvent(
            run.run_id,
            run.revision_hash,
            1,
            "build",
            execution_id,
            RunEventKind.AGENT_COMPLETED,
            b"output",
            agent_attempt_id=NodeExecutionId.for_node(
                run.run_id, run.revision_hash, "build"
            ).value,
            attempt_ordinal=1,
        ),
        None,
        WorkflowFormatVersion.V2,
    )


def streamed(projection: RunProjection, event: PersistedRunEvent) -> ServerSentEvent:
    async def collect() -> list[ServerSentEvent]:
        return [
            frame
            async for frame in stream_server_events(
                PreparedEventStream(projection.run.run_id, 0, 1, False, projection),
                stream_page_reader(OnePageOfEvents((event,))),
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=PageLimit(10),
                limits=api_limits(),
                poll_backoff=event_poll_backoff(),
            )
        ]

    frames = asyncio.run(collect())
    assert len(frames) == 1
    return frames[0]


@pytest.mark.proves("the-browser-derives-no-durable-state-for-a-v2-run")
def test_a_streamed_v2_event_carries_the_rail_the_run_stands_at_after_it() -> None:
    projection = v2_run_projection(PublicAgentAttemptState.POSSIBLY_RAN)

    frame = streamed(projection, agent_completed(projection))

    assert isinstance(frame.data, AgentCompletedEventResourceV2)
    assert frame.data.node_rail == (
        NodeRailResource(
            node_id="build",
            state=NodeState.SUCCEEDED,
            attempt=NodeRailAttemptResource(ordinal=1, state=None),
        ),
        NodeRailResource(node_id="done", state=NodeState.WORKING, attempt=None),
    )
