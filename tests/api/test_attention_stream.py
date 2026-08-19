"""A live attention holder accumulates identities already emitted at T."""

from __future__ import annotations

import asyncio
from typing import Any

from atelier2.api.stream import (
    BoundedQueryRunner,
    EventPollBackoff,
    PreparedAttentionStream,
    stream_attention_events,
)
from atelier2.application.read_attention_events import AttentionEventsRead
from atelier2.application.read_runs import RunRead
from atelier2.contracts.executions import NodeExecutionId, RunEvent, RunEventKind
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_events import PersistedRunEvent
from atelier2.contracts.runs import RunId
from atelier2.contracts.when import RecordedAt
from atelier2.ports.run_events import AttentionEvent
from tests.scenarios.api import api_limits, stream_run_projection

INSTANT = RecordedAt("2026-08-19T12:00:00Z")
LATER_SORTING_RUN = RunId("z-wait")
EARLIER_SORTING_RUN = RunId("a-wait")


class StopPolling(Exception):
    """End the never-finishing attention loop after the empty poll."""


def _completed(run_id: RunId) -> PersistedRunEvent:
    projection = stream_run_projection(run_id.value)
    run = projection.run
    return PersistedRunEvent(
        RunEvent(
            run.run_id,
            run.revision_hash,
            1,
            "agent",
            NodeExecutionId.for_node(run.run_id, run.revision_hash, "agent"),
            RunEventKind.AGENT_COMPLETED,
            b"done",
        ),
        None,
    )


class ScriptedAttentionPages:
    def __init__(self, pages: list[AttentionEventsRead]) -> None:
        self.pages = list(pages)
        self.asked: list[tuple[Any, ...]] = []

    def __call__(
        self,
        after_run_id: RunId | None,
        after_sequence: int | None,
        page_size: int,
        excluded_identities: tuple[tuple[RunId, int], ...] = (),
    ) -> AttentionEventsRead:
        self.asked.append(
            (after_run_id, after_sequence, page_size, excluded_identities)
        )
        return self.pages.pop(0)


def test_live_holder_excludes_identities_already_emitted_at_the_same_instant() -> None:
    later = _completed(LATER_SORTING_RUN)
    earlier = _completed(EARLIER_SORTING_RUN)
    pages = ScriptedAttentionPages(
        [
            AttentionEventsRead((AttentionEvent(later, INSTANT),)),
            AttentionEventsRead((AttentionEvent(earlier, INSTANT),)),
            AttentionEventsRead(()),
        ]
    )

    async def sleep(_delay: float) -> None:
        raise StopPolling()

    async def collect() -> None:
        async for _frame in stream_attention_events(
            PreparedAttentionStream(None, None),
            pages,
            lambda run_id: RunRead(stream_run_projection(run_id.value)),
            BoundedQueryRunner(1, admission_timeout_seconds=1),
            page_size=PageLimit(10),
            limits=api_limits(),
            poll_backoff=EventPollBackoff(0.01, 0.04, 2),
            sleep=sleep,
        ):
            pass

    try:
        asyncio.run(collect())
    except StopPolling:
        pass

    assert pages.asked[0] == (None, None, 10, ())
    assert pages.asked[1][:3] == (LATER_SORTING_RUN, 1, 10)
    assert pages.asked[1][3] == ()
    assert pages.asked[2][0] == EARLIER_SORTING_RUN
    assert pages.asked[2][1] == 1
    assert pages.asked[2][3] == ((LATER_SORTING_RUN, 1),)
