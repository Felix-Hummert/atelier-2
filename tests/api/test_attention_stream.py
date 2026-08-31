"""A live attention holder accumulates identities already emitted at T."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from typing import Any

from fastapi.sse import ServerSentEvent

from atelier2.api.references import encode_event_cursor, encode_public_run_reference
from atelier2.api.stream import (
    BoundedQueryRunner,
    EventPollBackoff,
    PreparedAttentionStream,
    stream_attention_events,
)
from atelier2.application.read_attention_events import AttentionEventsRead
from atelier2.application.read_runs import GetRunResult, RunRead
from atelier2.application.refusals import DurableStateCorrupt
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventKind,
)
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_events import PersistedRunEvent
from atelier2.contracts.runs import RunId
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.run_events import AttentionEvent, AttentionEventCorrupt
from tests.scenarios.api import api_limits, stream_run_projection

INSTANT = RecordedAt("2026-08-19T12:00:00Z")
LATER_SORTING_RUN = RunId("z-wait")
EARLIER_SORTING_RUN = RunId("a-wait")


class StopPolling(Exception):
    """End the never-finishing attention loop after the empty poll."""


ATTEMPT_BINDING = RunEventAgentAttemptBinding(AgentAttemptId("a" * 64), 1)


def _completed(run_id: RunId, output: bytes = b"done") -> PersistedRunEvent:
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
            output,
            attempt_binding=ATTEMPT_BINDING,
        ),
        None,
        WorkflowFormatVersion.V3,
    )


def _failed_with_overlong_receipt_reason(run_id: RunId) -> PersistedRunEvent:
    projection = stream_run_projection(run_id.value)
    run = projection.run
    return PersistedRunEvent(
        RunEvent(
            run.run_id,
            run.revision_hash,
            1,
            "agent",
            NodeExecutionId.for_node(run.run_id, run.revision_hash, "agent"),
            RunEventKind.AGENT_FAILED,
            b"OUTPUT_SCHEMA_REFUSED",
            attempt_binding=ATTEMPT_BINDING,
        ),
        None,
        WorkflowFormatVersion.V3,
        "x" * (MAXIMUM_AGENT_FIELD_CHARACTERS + 1),
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


def test_the_workbench_stream_names_an_omitted_overlong_receipt_reason() -> None:
    failed = _failed_with_overlong_receipt_reason(EARLIER_SORTING_RUN)
    pages = ScriptedAttentionPages(
        [
            AttentionEventsRead((AttentionEvent(failed, INSTANT),)),
            AttentionEventsRead(()),
        ]
    )

    async def sleep(_delay: float) -> None:
        raise StopPolling()

    async def collect() -> list[ServerSentEvent]:
        frames: list[ServerSentEvent] = []
        try:
            async for frame in stream_attention_events(
                PreparedAttentionStream(None, None),
                pages,
                lambda run_id: RunRead(stream_run_projection(run_id.value)),
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=PageLimit(10),
                limits=api_limits(),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
                sleep=sleep,
            ):
                frames.append(frame)
        except StopPolling:
            pass
        return frames

    frames = asyncio.run(collect())

    assert len(frames) == 1
    event: dict[str, Any] = json.loads(frames[0].data.model_dump_json())
    assert event["event"] == "AGENT_FAILED"
    reason = event["reason"]
    assert reason is not None
    assert not ("x" * (MAXIMUM_AGENT_FIELD_CHARACTERS + 1)).startswith(reason)
    assert "omitted" in reason
    assert (
        "GET /atelier/api/v1/runs/"
        f"{encode_public_run_reference(EARLIER_SORTING_RUN)}/nodes/agent"
    ) in reason
    assert len(reason) <= api_limits().maximum_field_characters


def test_attention_feed_names_an_unrepresentable_event_field() -> None:
    oversized = _completed(EARLIER_SORTING_RUN, b"oversized")
    pages = ScriptedAttentionPages(
        [AttentionEventsRead((AttentionEvent(oversized, INSTANT),))]
    )

    async def collect() -> list[ServerSentEvent]:
        return [
            frame
            async for frame in stream_attention_events(
                PreparedAttentionStream(None, None),
                pages,
                lambda run_id: RunRead(stream_run_projection(run_id.value)),
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=PageLimit(10),
                limits=api_limits(maximum_decoded_payload_bytes=4),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
            )
        ]

    frames = asyncio.run(collect())

    assert len(frames) == 1
    problem = json.loads(frames[0].data.model_dump_json())["problem"]
    assert problem["type"].endswith(":durable-projection-unrepresentable")
    assert problem["status"] == 500
    assert "event_payload" in problem["detail"]
    assert "4 bytes" in problem["detail"]
    assert "/runs/run1.YS13YWl0/nodes/agent" in problem["detail"]


CORRUPT_RUN = RunId("corrupt-run")
HEALTHY_RUN = RunId("healthy-run")


def test_attention_feed_names_one_corrupt_run_and_keeps_the_healthy_event() -> None:
    """A get_run projection failure belongs to that run, not to the feed."""

    corrupt = _completed(CORRUPT_RUN)
    healthy = _completed(HEALTHY_RUN)
    pages = ScriptedAttentionPages(
        [
            AttentionEventsRead(
                (AttentionEvent(corrupt, INSTANT), AttentionEvent(healthy, INSTANT))
            ),
            AttentionEventsRead(()),
        ]
    )

    async def sleep(_delay: float) -> None:
        raise StopPolling()

    def get_run(run_id: RunId) -> GetRunResult:
        if run_id == CORRUPT_RUN:
            return DurableStateCorrupt()
        return RunRead(stream_run_projection(run_id.value))

    async def collect() -> list[ServerSentEvent]:
        frames: list[ServerSentEvent] = []
        try:
            async for frame in stream_attention_events(
                PreparedAttentionStream(None, None),
                pages,
                get_run,
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=PageLimit(10),
                limits=api_limits(),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
                sleep=sleep,
            ):
                frames.append(frame)
        except StopPolling:
            pass
        return frames

    frames = asyncio.run(collect())
    payloads = [json.loads(frame.data.model_dump_json()) for frame in frames]
    kinds = [payload["event"] for payload in payloads]

    assert kinds == ["RUN_PROJECTION_CORRUPT", "AGENT_COMPLETED"]
    corrupt_payload = payloads[0]
    assert frames[0].id == encode_event_cursor(CORRUPT_RUN, 1)
    assert corrupt_payload["public_run_reference"] == encode_public_run_reference(
        CORRUPT_RUN
    )
    assert corrupt_payload["problem"]["type"].endswith(":durable-state-corrupt")
    assert payloads[1]["public_run_reference"] == encode_public_run_reference(
        HEALTHY_RUN
    )
    assert pages.asked[1][0] == HEALTHY_RUN
    assert pages.asked[1][1] == 1
    assert pages.asked[1][3] == ((CORRUPT_RUN, 1),)


def test_page_size_one_emits_the_corrupt_row_then_the_healthy_event() -> None:
    """limit=1 must emit the first corrupt identity so the next poll can leave it."""

    healthy = _completed(HEALTHY_RUN)
    pages = ScriptedAttentionPages(
        [
            AttentionEventsRead((AttentionEventCorrupt(CORRUPT_RUN, 1, INSTANT),)),
            AttentionEventsRead((AttentionEvent(healthy, INSTANT),)),
            AttentionEventsRead(()),
        ]
    )

    async def sleep(_delay: float) -> None:
        raise StopPolling()

    def get_run(run_id: RunId) -> GetRunResult:
        return RunRead(stream_run_projection(run_id.value))

    async def collect() -> list[ServerSentEvent]:
        frames: list[ServerSentEvent] = []
        try:
            async for frame in stream_attention_events(
                PreparedAttentionStream(None, None),
                pages,
                get_run,
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=PageLimit(1),
                limits=api_limits(),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
                sleep=sleep,
            ):
                frames.append(frame)
        except StopPolling:
            pass
        return frames

    frames = asyncio.run(collect())
    payloads = [json.loads(frame.data.model_dump_json()) for frame in frames]
    kinds = [payload["event"] for payload in payloads]

    assert kinds == ["RUN_PROJECTION_CORRUPT", "AGENT_COMPLETED"]
    assert frames[0].id == encode_event_cursor(CORRUPT_RUN, 1)
    assert payloads[0]["public_run_reference"] == encode_public_run_reference(
        CORRUPT_RUN
    )
    assert payloads[0]["problem"]["type"].endswith(":durable-state-corrupt")
    assert payloads[1]["public_run_reference"] == encode_public_run_reference(
        HEALTHY_RUN
    )
    assert pages.asked[0] == (None, None, 1, ())
    assert pages.asked[1][:3] == (CORRUPT_RUN, 1, 1)
    assert pages.asked[1][3] == ()
    assert pages.asked[2][0] == HEALTHY_RUN
    assert pages.asked[2][1] == 1
    assert pages.asked[2][3] == ((CORRUPT_RUN, 1),)


def test_attention_feed_ends_loudly_on_an_untyped_run_projection_failure() -> None:
    """A ValueError or unprojectable rail is a stream failure, not per-run isolation."""

    healthy = _completed(HEALTHY_RUN)
    pages = ScriptedAttentionPages(
        [AttentionEventsRead((AttentionEvent(healthy, INSTANT),))]
    )

    def get_run(run_id: RunId) -> GetRunResult:
        projection = stream_run_projection(run_id.value)
        return RunRead(
            replace(projection, run=replace(projection.run, current_node_id="missing"))
        )

    async def collect() -> list[ServerSentEvent]:
        return [
            frame
            async for frame in stream_attention_events(
                PreparedAttentionStream(None, None),
                pages,
                get_run,
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=PageLimit(10),
                limits=api_limits(),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
            )
        ]

    frames = asyncio.run(collect())
    assert len(frames) == 1
    payload = json.loads(frames[0].data.model_dump_json())
    assert payload["event"] == "STREAM_FAILED"
    assert payload["problem"]["type"].endswith(":internal-error")


def test_attention_feed_ends_loudly_on_an_untyped_event_resource_failure() -> None:
    """A V3 failure without its attempt binding is durable state nothing can type."""

    revision_hash = stream_run_projection(HEALTHY_RUN.value).run.revision_hash
    failed = PersistedRunEvent(
        RunEvent(
            HEALTHY_RUN,
            revision_hash,
            1,
            "agent",
            NodeExecutionId.for_node(HEALTHY_RUN, revision_hash, "agent"),
            RunEventKind.AGENT_FAILED,
            b"OUTPUT_SCHEMA_REFUSED",
        ),
        None,
        WorkflowFormatVersion.V3,
    )
    pages = ScriptedAttentionPages(
        [AttentionEventsRead((AttentionEvent(failed, INSTANT),))]
    )

    async def collect() -> list[ServerSentEvent]:
        return [
            frame
            async for frame in stream_attention_events(
                PreparedAttentionStream(None, None),
                pages,
                lambda run_id: RunRead(stream_run_projection(run_id.value)),
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=PageLimit(10),
                limits=api_limits(),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
            )
        ]

    frames = asyncio.run(collect())
    assert len(frames) == 1
    payload = json.loads(frames[0].data.model_dump_json())
    assert payload["event"] == "STREAM_FAILED"
    assert payload["problem"]["type"].endswith(":internal-error")
