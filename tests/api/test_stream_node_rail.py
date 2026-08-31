from __future__ import annotations

import asyncio
from typing import Never

from fastapi.sse import ServerSentEvent

from atelier2.api.stream import (
    BoundedQueryRunner,
    PreparedEventStream,
    stream_server_events,
)
from atelier2.api.wire.events import (
    AgentCompletedEventResourceV3,
    WaitCancelledEventResourceV3,
)
from atelier2.api.wire.resources import NodeRailAttemptResource, NodeRailResource
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventKind,
)
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_cancellations import RunCancelCommandId
from atelier2.contracts.run_events import (
    PersistedRunEvent,
    RunEventPage,
)
from atelier2.contracts.run_projections import (
    NodeState,
    PublicAgentAttemptState,
    RunProjection,
)
from atelier2.contracts.runs import RunId, RunState
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.run_events import (
    PrepareRunEventStreamResult,
)
from tests.api.test_agent_attempts import run_projection
from tests.api.test_run_cancellation import on_the_wait_node
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
        excluded_identities: tuple[tuple[RunId, int], ...] = (),
    ) -> Never:
        return unused_attention_event_page(
            after_run_id, after_sequence, limit, excluded_identities
        )


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
            attempt_binding=RunEventAgentAttemptBinding(
                AgentAttemptId(
                    NodeExecutionId.for_node(
                        run.run_id, run.revision_hash, "build"
                    ).value
                ),
                1,
            ),
        ),
        None,
        WorkflowFormatVersion.V3,
    )


def wait_cancelled(projection: RunProjection) -> PersistedRunEvent:
    """The attestation that ends a run resting at its pause, as the stream carries it.

    Sequence 2 against a snapshot that stops at 1, because the case worth pinning
    is the frame that *overtakes* the run a reader already holds: that is the one
    a browser folds in, and the one whose rail used to say `working`.
    """
    run = projection.run
    return PersistedRunEvent(
        RunEvent(
            run.run_id,
            run.revision_hash,
            2,
            run.current_node_id,
            NodeExecutionId.for_node(
                run.run_id, run.revision_hash, run.current_node_id
            ),
            RunEventKind.WAIT_CANCELLED,
            RunCancelCommandId.for_key("operator-stops-the-wait").value.encode("utf-8"),
        ),
        None,
        WorkflowFormatVersion.V3,
    )


def streamed(
    projection: RunProjection, event: PersistedRunEvent, head_sequence: int = 1
) -> ServerSentEvent:
    async def collect() -> list[ServerSentEvent]:
        return [
            frame
            async for frame in stream_server_events(
                PreparedEventStream(
                    projection.run.run_id, 0, head_sequence, False, projection
                ),
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


def test_a_streamed_event_carries_the_rail_the_run_stands_at_after_it() -> None:
    projection = run_projection(PublicAgentAttemptState.POSSIBLY_RAN)

    frame = streamed(projection, agent_completed(projection))

    assert isinstance(frame.data, AgentCompletedEventResourceV3)
    assert frame.data.node_rail == (
        NodeRailResource(
            node_id="build",
            state=NodeState.SUCCEEDED,
            attempt=NodeRailAttemptResource(ordinal=1, state=None),
        ),
        NodeRailResource(node_id="done", state=NodeState.WORKING, attempt=None),
    )


def test_a_streamed_wait_cancellation_carries_a_rail_that_says_the_pause_ended() -> (
    None
):
    """#668: the frame that ends a pause must not leave its node reading `working`.

    This is the event-driven half of the rail -- the streamed event overtakes the
    snapshot, so the run's own word is not consulted and the kind alone has to
    say the node ended. A `WAIT_CANCELLED` the mapping did not know would fall
    through to "the leading event is on this node, so it is working", which is
    exactly the wrong sentence for a run that has stopped.
    """
    projection = on_the_wait_node(RunState.WAITING_INPUT, last_event_sequence=1)

    frame = streamed(projection, wait_cancelled(projection), head_sequence=2)

    assert isinstance(frame.data, WaitCancelledEventResourceV3)
    assert frame.data.node_rail == (
        NodeRailResource(
            node_id=projection.run.current_node_id,
            state=NodeState.CANCELLED,
            attempt=None,
        ),
    )
