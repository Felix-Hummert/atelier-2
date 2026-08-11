from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TypeVar

from fastapi.sse import ServerSentEvent

from atelier2.api.models import run_event_resource
from atelier2.contracts.runs import RunId
from atelier2.ports.run_events import RunEventPage, RunEventQueries

Result = TypeVar("Result")


@dataclass(frozen=True)
class PreparedEventStream:
    run_id: RunId
    after_sequence: int
    head_sequence: int
    terminal: bool


class BoundedQueryRunner:
    """Run blocking durable calls under one global bound despite task cancellation."""

    def __init__(self, maximum_concurrent_queries: int) -> None:
        if (
            type(maximum_concurrent_queries) is not int
            or maximum_concurrent_queries <= 0
        ):
            raise ValueError("maximum concurrent queries must be a positive integer")
        self._semaphore = asyncio.Semaphore(maximum_concurrent_queries)
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
        await self._semaphore.acquire()
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


async def stream_server_events(
    prepared: PreparedEventStream,
    queries: RunEventQueries,
    runner: BoundedQueryRunner,
    *,
    page_size: int,
    poll_delay_seconds: float,
) -> AsyncIterator[ServerSentEvent]:
    after_sequence = prepared.after_sequence
    if prepared.terminal and after_sequence == prepared.head_sequence:
        return
    while True:
        result = await runner.run(
            lambda current_after_sequence=after_sequence: queries.read_run_event_page(
                prepared.run_id, current_after_sequence, page_size
            )
        )
        if not isinstance(result, RunEventPage):
            return
        for persisted in result.events:
            resource = run_event_resource(persisted)
            yield ServerSentEvent(
                id=resource.cursor,
                event=resource.event,
                data=resource,
            )
            after_sequence = resource.sequence
        if result.terminal_seen:
            return
        if not result.events:
            await asyncio.sleep(poll_delay_seconds)
