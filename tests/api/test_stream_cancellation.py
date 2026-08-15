from __future__ import annotations

import asyncio
import threading
from typing import Any, cast

from httpx import ASGITransport, AsyncClient

from atelier2.api.app import create_app
from atelier2.api.stream import (
    BoundedQueryRunner,
    EventPollBackoff,
    PreparedEventStream,
    stream_server_events,
)
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.run_events import (
    PersistedRunEvent,
    PrepareRunEventStreamResult,
    RunEventPage,
    RunEventQueries,
    StreamReady,
)
from atelier2.ports.workflow_revisions import (
    WorkflowRevisionPage,
)
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

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
            receipt_logical_key=(
                LogicalEffectKey("receipt-key")
                if kind
                in {
                    RunEventKind.ACTION_RECONCILIATION_RESOLVED,
                    RunEventKind.ACTION_COMPLETED,
                }
                else None
            ),
            receipt_result_hash=(
                Sha256Hash.of(payload)
                if kind
                in {
                    RunEventKind.ACTION_RECONCILIATION_RESOLVED,
                    RunEventKind.ACTION_COMPLETED,
                }
                else None
            ),
        ),
        None,
    )


def tasks_that_could_still_run() -> set[asyncio.Task[Any]]:
    """Everything besides this test that the loop could still hand a turn to.

    These tests prove that something does *not* happen. A wall-clock sleep buys
    that proof with real time and goes false-green under load. An empty set ends
    the question instead: a single-threaded loop with nothing left to run cannot
    reach the query afterwards, however long anybody waits.
    """

    running = asyncio.current_task()
    return {task for task in asyncio.all_tasks() if task is not running}


def test_cancelled_query_keeps_global_slot_until_real_thread_finishes() -> None:
    async def scenario() -> None:
        runner = BoundedQueryRunner(
            maximum_concurrent_queries=1, admission_timeout_seconds=1
        )
        first_started = threading.Event()
        release_first = threading.Event()

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

        second_task = asyncio.create_task(runner.run(lambda: "second"))
        assert runner.abandoned_queries == 1

        release_first.set()
        assert await second_task == "second"
        assert runner.abandoned_queries == 0
        # Admission raises the active count inside the event loop, before any
        # thread exists. A slot handed over while the abandoned thread was still
        # running would therefore have peaked at two, whatever the machine did.
        assert runner.peak_active_queries == 1

    asyncio.run(scenario())


def test_empty_stream_uses_capped_adaptive_backoff_with_injected_sleep() -> None:
    class EmptyQueries:
        def __init__(self) -> None:
            self.calls = 0

        def read_run_event_page(
            self,
            run_id: RunId,
            after_sequence: int,
            limit: int,
            projection_limit: object | None = None,
        ) -> RunEventPage:
            del run_id, after_sequence, limit
            self.calls += 1
            return RunEventPage((), False)

        def prepare_run_event_stream(
            self, run_id: RunId, after_sequence: int
        ) -> StreamReady:
            del run_id, after_sequence
            return StreamReady(0, False, 0)

    class ProbeComplete(Exception):
        pass

    async def scenario() -> None:
        queries = EmptyQueries()
        delays: list[float] = []

        async def virtual_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 5:
                raise ProbeComplete

        stream = stream_server_events(
            PreparedEventStream(RUN_ID, 0, 0, False),
            queries,
            BoundedQueryRunner(1, admission_timeout_seconds=1),
            page_size=1,
            limits=api_limits(),
            poll_backoff=EventPollBackoff(0.1, 0.4, 2),
            sleep=virtual_sleep,
        )

        try:
            await anext(stream)
        except ProbeComplete:
            pass
        else:
            raise AssertionError("empty stream probe did not reach its sleep bound")

        assert delays == [0.1, 0.2, 0.4, 0.4, 0.4]
        assert queries.calls == 5
        assert queries.calls / sum(delays) < 4

    asyncio.run(scenario())


def test_poll_backoff_resets_to_initial_delay_immediately_after_progress() -> None:
    class ProgressQueries:
        def __init__(self) -> None:
            self.calls = 0

        def read_run_event_page(
            self,
            run_id: RunId,
            after_sequence: int,
            limit: int,
            projection_limit: object | None = None,
        ) -> RunEventPage:
            del run_id, limit
            self.calls += 1
            if self.calls == 3:
                return RunEventPage(
                    (persisted_event(1, RunEventKind.AGENT_COMPLETED, b"one"),),
                    False,
                )
            assert after_sequence in {0, 1}
            return RunEventPage((), False)

        def prepare_run_event_stream(
            self, run_id: RunId, after_sequence: int
        ) -> StreamReady:
            del run_id, after_sequence
            return StreamReady(0, False, 0)

    class ProbeComplete(Exception):
        pass

    async def scenario() -> None:
        queries = ProgressQueries()
        delays: list[float] = []

        async def virtual_sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 3:
                raise ProbeComplete

        stream = stream_server_events(
            PreparedEventStream(RUN_ID, 0, 0, False),
            queries,
            BoundedQueryRunner(1, admission_timeout_seconds=1),
            page_size=1,
            limits=api_limits(),
            poll_backoff=EventPollBackoff(0.1, 0.8, 2),
            sleep=virtual_sleep,
        )

        event = await anext(stream)
        assert event.id is not None
        try:
            await anext(stream)
        except ProbeComplete:
            pass
        else:
            raise AssertionError("progress reset probe did not stop")

        assert delays == [0.1, 0.2, 0.1]

    asyncio.run(scenario())


def test_saturated_event_poll_does_not_starve_an_app_control_route() -> None:
    class AppQueries:
        def __init__(self) -> None:
            self.poll_started = threading.Event()
            self.release_poll = threading.Event()

        def prepare_run_event_stream(
            self, run_id: RunId, after_sequence: int
        ) -> StreamReady:
            assert run_id == RUN_ID
            assert after_sequence == 0
            return StreamReady(0, False, 0)

        def read_run_event_page(
            self,
            run_id: RunId,
            after_sequence: int,
            limit: int,
            projection_limit: object | None = None,
        ) -> RunEventPage:
            assert run_id == RUN_ID
            assert after_sequence == 0
            assert limit == 50
            self.poll_started.set()
            self.release_poll.wait(timeout=5)
            return RunEventPage((), True)

        def list_workflow_revisions(
            self, after: object, limit: int
        ) -> WorkflowRevisionPage:
            assert after is None
            assert limit == 50
            return WorkflowRevisionPage((), None)

    async def scenario() -> None:
        queries = AppQueries()
        app = create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(
                workflow_revision_queries=queries,
                run_queries=queries,
                run_event_queries=queries,
            ),
            limits=api_limits(
                maximum_control_queries=1,
                maximum_event_poll_queries=1,
            ),
            event_poll_backoff=event_poll_backoff(),
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            event_request = asyncio.create_task(
                client.get(
                    "/atelier/api/v1/runs/run1.Ym91bmRlZC1zdHJlYW0/events",
                    headers={"accept": "text/event-stream"},
                )
            )
            assert await asyncio.to_thread(queries.poll_started.wait, 5)
            control = await asyncio.wait_for(
                client.get("/atelier/api/v1/workflow-revisions"), timeout=0.5
            )
            assert control.status_code == 200
            assert control.json() == {
                "items": [],
                "next_after_revision_hash": None,
            }
            queries.release_poll.set()
            events = await asyncio.wait_for(event_request, timeout=1)
            assert events.status_code == 200
            assert events.content == b""

    asyncio.run(scenario())


def test_saturated_control_admission_returns_503_without_starting_another_query() -> (
    None
):
    class BlockingQueries:
        def __init__(self) -> None:
            self.first_started = threading.Event()
            self.release_first = threading.Event()
            self.calls = 0

        def list_workflow_revisions(
            self, after: object, limit: int
        ) -> WorkflowRevisionPage:
            assert after is None
            assert limit == 50
            self.calls += 1
            self.first_started.set()
            self.release_first.wait(timeout=5)
            return WorkflowRevisionPage((), None)

    async def scenario() -> None:
        queries = BlockingQueries()
        app = create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(workflow_revision_queries=queries),
            limits=api_limits(
                maximum_control_queries=1,
                maximum_query_admission_wait_milliseconds=10,
            ),
            event_poll_backoff=event_poll_backoff(),
        )
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(
                client.get("/atelier/api/v1/workflow-revisions")
            )
            assert await asyncio.to_thread(queries.first_started.wait, 5)
            refused = await client.get("/atelier/api/v1/workflow-revisions")
            assert refused.status_code == 503
            assert refused.json()["type"].endswith(":temporarily-unavailable")
            assert queries.calls == 1
            queries.release_first.set()
            completed = await asyncio.wait_for(first, timeout=1)
            assert completed.status_code == 200

    asyncio.run(scenario())


def test_event_poll_admission_timeout_mid_stream_closes_without_starting_query() -> (
    None
):
    class UnreachedQueries:
        def __init__(self) -> None:
            self.calls = 0

        def read_run_event_page(
            self,
            run_id: RunId,
            after_sequence: int,
            limit: int,
            projection_limit: object | None = None,
        ) -> RunEventPage:
            del run_id, after_sequence, limit
            self.calls += 1
            return RunEventPage((), False)

    async def scenario() -> None:
        runner = BoundedQueryRunner(1, admission_timeout_seconds=0.01)
        occupied = threading.Event()
        release = threading.Event()

        def hold_slot() -> None:
            occupied.set()
            release.wait(timeout=5)

        occupying = asyncio.create_task(runner.run(hold_slot))
        assert await asyncio.to_thread(occupied.wait, 5)
        queries = UnreachedQueries()
        received = [
            item
            async for item in stream_server_events(
                PreparedEventStream(RUN_ID, 1, 2, False),
                cast(RunEventQueries, queries),
                runner,
                page_size=1,
                limits=api_limits(),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
            )
        ]
        release.set()
        await occupying

        assert received == []
        assert queries.calls == 0

    asyncio.run(scenario())


def test_cancellation_while_waiting_for_slot_starts_no_query() -> None:
    async def scenario() -> None:
        runner = BoundedQueryRunner(
            maximum_concurrent_queries=1, admission_timeout_seconds=1
        )
        first_started = threading.Event()
        release_first = threading.Event()
        waiting_started = threading.Event()
        asking_for_a_slot = asyncio.Event()

        def first() -> None:
            first_started.set()
            release_first.wait(timeout=5)

        async def ask_for_a_slot() -> None:
            asking_for_a_slot.set()
            await runner.run(lambda: waiting_started.set())

        first_task = asyncio.create_task(runner.run(first))
        assert await asyncio.to_thread(first_started.wait, 5)
        waiting_task = asyncio.create_task(ask_for_a_slot())
        # Setting the event does not suspend the asking task, so it runs on into
        # `run` and parks on the admission it cannot get. This test resumes only
        # afterwards, with the waiter provably at the point being cancelled.
        await asking_for_a_slot.wait()
        assert runner.peak_active_queries == 1

        waiting_task.cancel()
        try:
            await waiting_task
        except asyncio.CancelledError:
            pass
        assert not waiting_started.is_set()

        release_first.set()
        await first_task
        assert not waiting_started.is_set()
        assert runner.peak_active_queries == 1
        assert runner.abandoned_queries == 0
        assert tasks_that_could_still_run() == set()

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
            self,
            run_id: RunId,
            after_sequence: int,
            limit: int,
            projection_limit: object | None = None,
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
            BoundedQueryRunner(1, admission_timeout_seconds=1),
            page_size=2,
            limits=api_limits(),
            poll_backoff=EventPollBackoff(0.01, 0.04, 2),
        )

        first = await anext(stream)
        assert first.id is not None
        assert queries.calls == [0]
        # Nothing is left that could fetch the next page behind the consumer's
        # back, so the second page stays unread until it is asked for.
        assert tasks_that_could_still_run() == set()

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
            self,
            run_id: RunId,
            after_sequence: int,
            limit: int,
            projection_limit: object | None = None,
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
        runner = BoundedQueryRunner(1, admission_timeout_seconds=1)
        stream = stream_server_events(
            PreparedEventStream(RUN_ID, 0, 0, False),
            queries,
            runner,
            page_size=2,
            limits=api_limits(),
            poll_backoff=EventPollBackoff(0.01, 0.04, 2),
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
        await asyncio.gather(*tasks_that_could_still_run())
        assert runner.abandoned_queries == 0
        assert queries.closes == 1
        # The abandoned query has finished and left nothing behind that could
        # ask for a second page on the cancelled stream's behalf.
        assert queries.calls == 1
        assert tasks_that_could_still_run() == set()

    asyncio.run(scenario())
