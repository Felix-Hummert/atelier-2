from __future__ import annotations

import asyncio
import threading

from atelier2.api.stream import (
    BoundedQueryRunner,
    PreparedEventStream,
    stream_server_events,
)
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.run_events import (
    PersistedRunEvent,
    PrepareRunEventStreamResult,
    RunEventPage,
    StreamReady,
)

RUN_ID = RunId("bounded-stream")
REVISION_HASH = WorkflowRevisionHash("0" * 64)


def persisted_event(
    sequence: int, kind: RunEventKind, payload: bytes
) -> PersistedRunEvent:
    node_id = "final" if kind is RunEventKind.SUBWORKFLOW_COMPLETED else "agent"
    return PersistedRunEvent(
        RunEvent(
            RUN_ID,
            REVISION_HASH,
            sequence,
            node_id,
            NodeExecutionId.for_node(RUN_ID, REVISION_HASH, node_id),
            kind,
            payload,
        ),
        None,
    )


def test_cancelled_query_keeps_global_slot_until_real_thread_finishes() -> None:
    async def scenario() -> None:
        runner = BoundedQueryRunner(maximum_concurrent_queries=1)
        first_started = threading.Event()
        release_first = threading.Event()
        second_started = threading.Event()

        def first() -> str:
            first_started.set()
            release_first.wait(timeout=5)
            return "first"

        first_task = asyncio.create_task(runner.run(first))
        assert await asyncio.to_thread(first_started.wait, 5)
        first_task.cancel()
        try:
            await first_task
        except asyncio.CancelledError:
            pass

        second_task = asyncio.create_task(
            runner.run(lambda: second_started.set() or "second")
        )
        await asyncio.sleep(0.05)
        assert not second_started.is_set()
        assert runner.abandoned_queries == 1

        release_first.set()
        assert await second_task == "second"
        assert runner.abandoned_queries == 0
        assert runner.peak_active_queries == 1

    asyncio.run(scenario())


def test_cancellation_while_waiting_for_slot_starts_no_query() -> None:
    async def scenario() -> None:
        runner = BoundedQueryRunner(maximum_concurrent_queries=1)
        first_started = threading.Event()
        release_first = threading.Event()
        waiting_started = threading.Event()

        def first() -> None:
            first_started.set()
            release_first.wait(timeout=5)

        first_task = asyncio.create_task(runner.run(first))
        assert await asyncio.to_thread(first_started.wait, 5)
        waiting_task = asyncio.create_task(runner.run(lambda: waiting_started.set()))
        await asyncio.sleep(0.05)
        waiting_task.cancel()
        try:
            await waiting_task
        except asyncio.CancelledError:
            pass
        assert not waiting_started.is_set()

        release_first.set()
        await first_task
        assert not waiting_started.is_set()

    asyncio.run(scenario())


def test_stream_does_not_read_the_next_bounded_page_before_yielding_the_current_one() -> (
    None
):
    class PagedQueries:
        def __init__(self) -> None:
            self.calls: list[int] = []

        def prepare_run_event_stream(
            self, run_id: RunId, after_sequence: int
        ) -> PrepareRunEventStreamResult:
            del run_id, after_sequence
            return StreamReady(3, True, 0)

        def read_run_event_page(
            self, run_id: RunId, after_sequence: int, limit: int
        ) -> RunEventPage:
            assert run_id == RUN_ID
            assert limit == 2
            self.calls.append(after_sequence)
            if after_sequence == 0:
                return RunEventPage(
                    (
                        persisted_event(1, RunEventKind.AGENT_COMPLETED, b"one"),
                        persisted_event(2, RunEventKind.AGENT_COMPLETED, b"two"),
                    ),
                    False,
                )
            return RunEventPage(
                (persisted_event(3, RunEventKind.SUBWORKFLOW_COMPLETED, b"3"),),
                True,
            )

    async def scenario() -> None:
        queries = PagedQueries()
        stream = stream_server_events(
            PreparedEventStream(RUN_ID, 0, 3, True),
            queries,
            BoundedQueryRunner(1),
            page_size=2,
            poll_delay_seconds=0.01,
        )

        first = await anext(stream)
        assert first.id is not None
        assert queries.calls == [0]
        await asyncio.sleep(0.02)
        assert queries.calls == [0]

        second = await anext(stream)
        assert second.id is not None
        assert queries.calls == [0]

        third = await anext(stream)
        assert third.id is not None
        assert queries.calls == [0, 2]
        try:
            await anext(stream)
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError("terminal event did not close its durable stream")

    asyncio.run(scenario())


def test_cancelled_stream_starts_no_next_query_and_blocked_query_closes_once() -> None:
    class BlockingQueries:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.calls = 0
            self.closes = 0

        def prepare_run_event_stream(
            self, run_id: RunId, after_sequence: int
        ) -> PrepareRunEventStreamResult:
            del run_id, after_sequence
            return StreamReady(0, False, 0)

        def read_run_event_page(
            self, run_id: RunId, after_sequence: int, limit: int
        ) -> RunEventPage:
            del run_id, after_sequence, limit
            self.calls += 1
            self.started.set()
            try:
                self.release.wait(timeout=5)
                return RunEventPage((), False)
            finally:
                self.closes += 1

    async def scenario() -> None:
        queries = BlockingQueries()
        runner = BoundedQueryRunner(1)
        stream = stream_server_events(
            PreparedEventStream(RUN_ID, 0, 0, False),
            queries,
            runner,
            page_size=2,
            poll_delay_seconds=0.01,
        )
        pending = asyncio.ensure_future(anext(stream))
        assert await asyncio.to_thread(queries.started.wait, 5)

        pending.cancel()
        try:
            await pending
        except asyncio.CancelledError:
            pass
        assert queries.calls == 1
        assert runner.abandoned_queries == 1

        queries.release.set()
        deadline = asyncio.get_running_loop().time() + 5
        while runner.abandoned_queries and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert runner.abandoned_queries == 0
        assert queries.closes == 1
        await asyncio.sleep(0.03)
        assert queries.calls == 1

    asyncio.run(scenario())
