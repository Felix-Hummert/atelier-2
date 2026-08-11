from __future__ import annotations

import asyncio
import threading
from typing import cast

import pytest
from httpx import ASGITransport, AsyncClient

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import ApiPorts, create_app
from atelier2.api.references import MAX_SIGNED_INT64
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
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    TransactionalWaitAnswerer,
)
from atelier2.ports.effects import TransactionalEffectReconcileCommander
from atelier2.ports.run_events import (
    EventHistoryCorrupt,
    PersistedRunEvent,
    PrepareRunEventStreamResult,
    RunEventPage,
    RunEventQueries,
    StreamReady,
)
from atelier2.ports.run_queries import RunQueries
from atelier2.ports.workflow_revisions import (
    WorkflowRevisionPage,
    WorkflowRevisionPublisher,
    WorkflowRevisionQueries,
)
from tests.scenarios.api import api_limits, event_poll_backoff

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


def test_cancelled_query_keeps_global_slot_until_real_thread_finishes() -> None:
    async def scenario() -> None:
        runner = BoundedQueryRunner(
            maximum_concurrent_queries=1, admission_timeout_seconds=1
        )
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
        unused = object()
        app = create_app(
            source_commit="commit",
            source_tree="tree",
            ports=ApiPorts(
                cast(WorkflowRevisionPublisher, unused),
                cast(DurablePublishedRunStarter, unused),
                cast(TransactionalWaitAnswerer, unused),
                cast(TransactionalEffectReconcileCommander, unused),
                cast(WorkflowRevisionQueries, queries),
                cast(RunQueries, queries),
                queries,
                parse_workflow_document,
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
        unused = object()
        app = create_app(
            source_commit="commit",
            source_tree="tree",
            ports=ApiPorts(
                cast(WorkflowRevisionPublisher, unused),
                cast(DurablePublishedRunStarter, unused),
                cast(TransactionalWaitAnswerer, unused),
                cast(TransactionalEffectReconcileCommander, unused),
                cast(WorkflowRevisionQueries, queries),
                cast(RunQueries, unused),
                cast(RunEventQueries, unused),
                parse_workflow_document,
            ),
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
        deadline = asyncio.get_running_loop().time() + 5
        while runner.abandoned_queries and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert runner.abandoned_queries == 0
        assert queries.closes == 1
        await asyncio.sleep(0.03)
        assert queries.calls == 1

    asyncio.run(scenario())


def test_post_header_event_history_corruption_closes_without_synthetic_event() -> None:
    class CorruptQueries:
        def __init__(self) -> None:
            self.calls = 0

        def read_run_event_page(
            self,
            run_id: RunId,
            after_sequence: int,
            limit: int,
            projection_limit: object | None = None,
        ) -> EventHistoryCorrupt:
            del run_id, after_sequence, limit
            self.calls += 1
            return EventHistoryCorrupt()

        def prepare_run_event_stream(
            self, run_id: RunId, after_sequence: int
        ) -> StreamReady:
            del run_id, after_sequence
            return StreamReady(1, False, 0)

    async def scenario() -> None:
        queries = CorruptQueries()
        received = [
            item
            async for item in stream_server_events(
                PreparedEventStream(RUN_ID, 0, 1, False),
                queries,
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=1,
                limits=api_limits(),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
            )
        ]
        assert received == []
        assert queries.calls == 1

    asyncio.run(scenario())


def test_over_page_query_output_closes_before_any_event_is_serialized() -> None:
    class OverPageQueries:
        def read_run_event_page(
            self,
            run_id: RunId,
            after_sequence: int,
            limit: int,
            projection_limit: object | None = None,
        ) -> RunEventPage:
            del run_id, after_sequence
            assert limit == 1
            return RunEventPage(
                (
                    persisted_event(1, RunEventKind.AGENT_COMPLETED, b"one"),
                    persisted_event(2, RunEventKind.AGENT_COMPLETED, b"two"),
                ),
                False,
            )

    async def scenario() -> None:
        received = [
            item
            async for item in stream_server_events(
                PreparedEventStream(RUN_ID, 0, 2, False),
                cast(RunEventQueries, OverPageQueries()),
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=1,
                limits=api_limits(),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
            )
        ]
        assert received == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("sequence", "payload", "limit_changes"),
    [
        (1, b"oversized", {"maximum_decoded_payload_bytes": 4}),
        (1, b"\xff", {}),
        (MAX_SIGNED_INT64 + 1, b"valid", {}),
    ],
    ids=("payload-limit", "invalid-utf8", "cursor-range"),
)
def test_invalid_event_projection_closes_without_serialization(
    sequence: int, payload: bytes, limit_changes: dict[str, int]
) -> None:
    class OversizedEventQueries:
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
            return RunEventPage(
                (persisted_event(sequence, RunEventKind.AGENT_COMPLETED, payload),),
                False,
            )

        def prepare_run_event_stream(
            self, run_id: RunId, after_sequence: int
        ) -> StreamReady:
            del run_id, after_sequence
            return StreamReady(1, False, 0)

    async def scenario() -> None:
        queries = OversizedEventQueries()
        received = [
            item
            async for item in stream_server_events(
                PreparedEventStream(RUN_ID, 0, 1, False),
                queries,
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=1,
                limits=api_limits(**limit_changes),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
            )
        ]

        assert received == []
        assert queries.calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (RunEventKind.WAIT_ANSWERED, b"01"),
        (RunEventKind.SUBWORKFLOW_COMPLETED, b"+1"),
        (RunEventKind.ACTION_RECONCILIATION_RESOLVED, b"result"),
    ],
    ids=("wait-integer", "subworkflow-integer", "missing-receipt"),
)
def test_invalid_typed_event_conversion_closes_the_stream(
    kind: RunEventKind, payload: bytes
) -> None:
    class InvalidTypedQueries:
        def read_run_event_page(
            self,
            run_id: RunId,
            after_sequence: int,
            limit: int,
            projection_limit: object | None = None,
        ) -> RunEventPage:
            del run_id, after_sequence, limit
            return RunEventPage((persisted_event(1, kind, payload),), False)

    async def scenario() -> None:
        received = [
            item
            async for item in stream_server_events(
                PreparedEventStream(RUN_ID, 0, 1, False),
                cast(RunEventQueries, InvalidTypedQueries()),
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=1,
                limits=api_limits(),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
            )
        ]
        assert received == []

    asyncio.run(scenario())
