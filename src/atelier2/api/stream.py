from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal, TypeVar, assert_never, get_args

from fastapi.sse import ServerSentEvent

from atelier2.api.limits import ApiLimitExceeded, ApiLimits
from atelier2.api.problems import PROJECTION_LIMIT_DETAIL, problem_resource
from atelier2.api.projection.events import run_event_resource
from atelier2.api.projection.runs import node_rail_resources
from atelier2.api.wire.resources import StreamFailureResource
from atelier2.application.project_node_rail import (
    NodeRailUnprojectable,
    project_node_rail,
)
from atelier2.application.read_attention_events import (
    AttentionCursorUnknown,
    AttentionEventPageOversized,
    AttentionEventsRead,
    ReadAttentionEventsResult,
)
from atelier2.application.read_run_events import (
    ReadRunEventsResult,
    RunEventPageOversized,
    RunEventsRead,
)
from atelier2.application.read_runs import GetRunResult, RunNotFound, RunRead
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
)
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_events import (
    PersistedRunEvent,
)
from atelier2.contracts.run_projections import (
    RunProjection,
)
from atelier2.contracts.runs import RunId
from atelier2.contracts.when import RecordedAt

Result = TypeVar("Result")

StreamFailureCode = Literal[
    "durable-state-corrupt", "temporarily-unavailable", "internal-error"
]
STREAM_FAILURE_CODES: Final[tuple[StreamFailureCode, ...]] = get_args(StreamFailureCode)
"""The problem vocabulary a failed stream may speak, owned by the only emitter.

The published document narrows the failure frame to exactly these problems, so
a consumer that accepts them accepts every frame this stream can send.
"""


class QueryAdmissionTimeout(TimeoutError):
    """The bounded API query runner could not admit work before its deadline."""


@dataclass(frozen=True)
class PreparedEventStream:
    run_id: RunId
    after_sequence: int
    head_sequence: int
    terminal: bool
    projection: RunProjection
    """The run this stream reads, read once, so every frame can say where it stands.

    A reader given only events has to rebuild the state machine to learn what one
    event means for the rest of the run. Reading the run here is the edge that
    spares every reader that rebuild: one durable read per stream, onto which the
    events streamed afterwards are folded.
    """


@dataclass(frozen=True)
class PreparedAttentionStream:
    after_run_id: RunId | None
    after_sequence: int | None
    """Resume by same-instant identity exclusion from this event1, or the start of the feed."""


@dataclass(frozen=True)
class EventPollBackoff:
    initial_delay_seconds: float
    maximum_delay_seconds: float
    multiplier: float

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.initial_delay_seconds)
            or self.initial_delay_seconds <= 0
        ):
            raise ValueError("initial poll delay must be positive")
        if (
            not math.isfinite(self.maximum_delay_seconds)
            or self.maximum_delay_seconds < self.initial_delay_seconds
        ):
            raise ValueError("maximum poll delay must not be below the initial delay")
        if not math.isfinite(self.multiplier) or self.multiplier <= 1:
            raise ValueError("poll delay multiplier must be greater than one")


class BoundedQueryRunner:
    """Run blocking durable calls under one global bound despite task cancellation."""

    def __init__(
        self,
        maximum_concurrent_queries: int,
        *,
        admission_timeout_seconds: float,
    ) -> None:
        if (
            type(maximum_concurrent_queries) is not int
            or maximum_concurrent_queries <= 0
        ):
            raise ValueError("maximum concurrent queries must be a positive integer")
        if (
            not math.isfinite(admission_timeout_seconds)
            or admission_timeout_seconds <= 0
        ):
            raise ValueError("query admission timeout must be positive")
        self._semaphore = asyncio.Semaphore(maximum_concurrent_queries)
        self._admission_timeout_seconds = admission_timeout_seconds
        self._active_queries = 0
        self._peak_active_queries = 0
        self._abandoned_tasks: set[asyncio.Task[object]] = set()

    @property
    def peak_active_queries(self) -> int:
        return self._peak_active_queries

    @property
    def abandoned_queries(self) -> int:
        return len(self._abandoned_tasks)

    async def run(self, query: Callable[[], Result]) -> Result:
        try:
            await asyncio.wait_for(
                self._semaphore.acquire(), self._admission_timeout_seconds
            )
        except TimeoutError as error:
            raise QueryAdmissionTimeout(
                "query admission exceeded its configured wait bound"
            ) from error
        self._active_queries += 1
        self._peak_active_queries = max(self._peak_active_queries, self._active_queries)
        task = asyncio.create_task(asyncio.to_thread(query))

        def finished(completed: asyncio.Task[Result]) -> None:
            self._active_queries -= 1
            self._abandoned_tasks.discard(completed)
            self._semaphore.release()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                self._abandoned_tasks.add(task)
            raise


def _stream_failure(
    code: StreamFailureCode, detail: str | None = None
) -> ServerSentEvent:
    """The last frame of a failed stream.

    It carries no id: a resume cursor on a refusal would invite the browser to
    reconnect into the same refusal forever.
    """

    return ServerSentEvent(
        data=StreamFailureResource(problem=problem_resource(code, detail))
    )


async def stream_server_events(
    prepared: PreparedEventStream,
    read_page: Callable[[RunId, int, int], ReadRunEventsResult],
    runner: BoundedQueryRunner,
    *,
    page_size: PageLimit,
    limits: ApiLimits,
    poll_backoff: EventPollBackoff,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[ServerSentEvent]:
    """Forward one run's events until it ends, is refused, or the client leaves.

    What one page *means* is decided by `read_page`; this loop owns only what is
    the API's to own — the query budget it is admitted through, how long it waits
    before asking again, the limits the served projection must fit, and the frame
    each outcome becomes. Backpressure and transient unavailability end the stream
    regularly rather than as a failure, because the client's own reconnect is the
    answer to both.
    """
    after_sequence = prepared.after_sequence
    next_poll_delay = poll_backoff.initial_delay_seconds
    streamed: list[PersistedRunEvent] = []
    if prepared.terminal and after_sequence == prepared.head_sequence:
        return
    while True:
        try:
            result = await runner.run(
                lambda current_after_sequence=after_sequence: read_page(
                    prepared.run_id, current_after_sequence, page_size.value
                )
            )
        except QueryAdmissionTimeout:
            # Backpressure is not a failure: end regularly and let the client reconnect.
            return
        match result:
            case RunEventsRead() as page:
                pass
            case ReadUnavailable():
                # Transient unavailability is answered by the client's own reconnect.
                return
            case ProjectionTooLarge():
                yield _stream_failure(
                    "temporarily-unavailable", PROJECTION_LIMIT_DETAIL
                )
                return
            case RunEventPageOversized():
                yield _stream_failure("internal-error")
                return
            case DurableStateCorrupt():
                yield _stream_failure("durable-state-corrupt")
                return
            case _ as unreachable:
                assert_never(unreachable)
        try:
            for persisted in page.events:
                limits.require_event_projection(persisted)
        except ApiLimitExceeded:
            yield _stream_failure("temporarily-unavailable", PROJECTION_LIMIT_DETAIL)
            return
        except ValueError:
            yield _stream_failure("durable-state-corrupt")
            return
        if page.events:
            next_poll_delay = poll_backoff.initial_delay_seconds
        for persisted in page.events:
            streamed.append(persisted)
            try:
                resource = run_event_resource(
                    persisted,
                    node_rail_resources(
                        project_node_rail(prepared.projection, streamed)
                    ),
                )
            except (ValueError, NodeRailUnprojectable):
                yield _stream_failure("durable-state-corrupt")
                return
            except AssertionError:
                yield _stream_failure("internal-error")
                return
            yield ServerSentEvent(
                id=resource.cursor,
                data=resource,
            )
            after_sequence = resource.sequence
        if page.terminal_seen:
            return
        if not page.events:
            await sleep(next_poll_delay)
            next_poll_delay = min(
                poll_backoff.maximum_delay_seconds,
                next_poll_delay * poll_backoff.multiplier,
            )


async def stream_attention_events(
    prepared: PreparedAttentionStream,
    read_page: Callable[
        [RunId | None, int | None, int, tuple[tuple[RunId, int], ...]],
        ReadAttentionEventsResult,
    ],
    get_run: Callable[[RunId], GetRunResult],
    runner: BoundedQueryRunner,
    *,
    page_size: PageLimit,
    limits: ApiLimits,
    poll_backoff: EventPollBackoff,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[ServerSentEvent]:
    """Forward the ATTENTION_EVENT_KINDS across runs until the client leaves.

    The feed does not end: a terminal run is one event, not the end of the
    subscription. Backpressure and transient unavailability end regularly so
    the client's own reconnect is the answer to both.

    Resume is same-instant identity exclusion: from the cursor event's instant
    T, later instants, or other identities still at T. Last-Event-ID seeds the
    set with that event1; this loop adds each identity it emits and resets the
    set when the second advances.
    """
    after_run_id = prepared.after_run_id
    after_sequence = prepared.after_sequence
    emitted_at_instant: set[tuple[RunId, int]] = set()
    if after_run_id is not None and after_sequence is not None:
        emitted_at_instant.add((after_run_id, after_sequence))
    current_instant: RecordedAt | None = None
    next_poll_delay = poll_backoff.initial_delay_seconds
    while True:
        excluded = tuple(
            identity
            for identity in emitted_at_instant
            if identity != (after_run_id, after_sequence)
        )
        try:
            result = await runner.run(
                lambda current_run_id=after_run_id, current_sequence=after_sequence, current_excluded=excluded: (
                    read_page(
                        current_run_id,
                        current_sequence,
                        page_size.value,
                        current_excluded,
                    )
                )
            )
        except QueryAdmissionTimeout:
            return
        match result:
            case AttentionEventsRead() as page:
                pass
            case AttentionCursorUnknown() | DurableStateCorrupt():
                yield _stream_failure("durable-state-corrupt")
                return
            case ReadUnavailable():
                return
            case ProjectionTooLarge():
                yield _stream_failure(
                    "temporarily-unavailable", PROJECTION_LIMIT_DETAIL
                )
                return
            case AttentionEventPageOversized():
                yield _stream_failure("internal-error")
                return
            case _ as unreachable:
                assert_never(unreachable)
        try:
            for item in page.events:
                limits.require_event_projection(item.event)
        except ApiLimitExceeded:
            yield _stream_failure("temporarily-unavailable", PROJECTION_LIMIT_DETAIL)
            return
        except ValueError:
            yield _stream_failure("durable-state-corrupt")
            return
        if page.events:
            next_poll_delay = poll_backoff.initial_delay_seconds
        for item in page.events:
            persisted = item.event
            try:
                run_result = await runner.run(
                    lambda current_run_id=persisted.event.run_id: get_run(
                        current_run_id
                    )
                )
            except QueryAdmissionTimeout:
                return
            match run_result:
                case RunRead(projection):
                    pass
                case RunNotFound() | DurableStateCorrupt():
                    yield _stream_failure("durable-state-corrupt")
                    return
                case ReadUnavailable():
                    return
                case ProjectionTooLarge():
                    yield _stream_failure(
                        "temporarily-unavailable", PROJECTION_LIMIT_DETAIL
                    )
                    return
                case _ as unreachable:
                    assert_never(unreachable)
            try:
                limits.require_run_projection(projection)
                resource = run_event_resource(
                    persisted,
                    node_rail_resources(project_node_rail(projection, (persisted,))),
                )
            except ApiLimitExceeded:
                yield _stream_failure(
                    "temporarily-unavailable", PROJECTION_LIMIT_DETAIL
                )
                return
            except (ValueError, NodeRailUnprojectable):
                yield _stream_failure("durable-state-corrupt")
                return
            except AssertionError:
                yield _stream_failure("internal-error")
                return
            yield ServerSentEvent(
                id=resource.cursor,
                data=resource,
            )
            if current_instant is not None and item.recorded_at != current_instant:
                emitted_at_instant = set()
            current_instant = item.recorded_at
            after_run_id = persisted.event.run_id
            after_sequence = persisted.event.event_sequence
            emitted_at_instant.add((after_run_id, after_sequence))
        if not page.events:
            await sleep(next_poll_delay)
            next_poll_delay = min(
                poll_backoff.maximum_delay_seconds,
                next_poll_delay * poll_backoff.multiplier,
            )
