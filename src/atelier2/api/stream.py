from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal, TypeVar, assert_never, get_args

from fastapi.sse import ServerSentEvent

from atelier2.api.limits import ApiLimitExceeded, ApiLimits
from atelier2.api.models import StreamFailureResource, run_event_resource
from atelier2.api.problems import problem_resource
from atelier2.contracts.runs import RunId
from atelier2.ports.run_events import (
    EventHistoryCorrupt,
    RunEventPage,
    RunEventQueries,
)
from atelier2.ports.workflow_revisions import (
    PROJECTION_LIMIT_DETAIL,
    DurableProjectionLimit,
    QueryDurableStateCorrupt,
    ReadUnavailable,
)

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
    queries: RunEventQueries,
    runner: BoundedQueryRunner,
    *,
    page_size: int,
    limits: ApiLimits,
    poll_backoff: EventPollBackoff,
    projection_limit: DurableProjectionLimit | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> AsyncIterator[ServerSentEvent]:
    after_sequence = prepared.after_sequence
    next_poll_delay = poll_backoff.initial_delay_seconds
    if prepared.terminal and after_sequence == prepared.head_sequence:
        return
    while True:
        try:
            result = await runner.run(
                lambda current_after_sequence=after_sequence: (
                    queries.read_run_event_page(
                        prepared.run_id,
                        current_after_sequence,
                        page_size,
                        projection_limit,
                    )
                )
            )
        except QueryAdmissionTimeout:
            # Backpressure is not a failure: end regularly and let the client reconnect.
            return
        match result:
            case RunEventPage() as page:
                pass
            case ReadUnavailable():
                # Transient unavailability is answered by the client's own reconnect.
                return
            case EventHistoryCorrupt() | QueryDurableStateCorrupt():
                yield _stream_failure("durable-state-corrupt")
                return
            case _ as unreachable:
                assert_never(unreachable)
        if len(page.events) > page_size:
            yield _stream_failure("internal-error")
            return
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
            try:
                resource = run_event_resource(persisted)
            except ValueError:
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
