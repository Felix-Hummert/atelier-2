"""What the event stream says when a durable row cannot be served.

Every case here is a stream that has already answered 200 and sent its
headers, so the only channel left is the stream itself. A failure that ends
the stream without a word is indistinguishable from a run that finished.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Never
from urllib.parse import quote

import pytest
from fastapi.sse import ServerSentEvent

from atelier2.api.openapi import API_PREFIX, EVENT_PATH
from atelier2.api.problems import PROJECTION_LIMIT_DETAIL
from atelier2.api.projection.events import bounded_event_summary
from atelier2.api.references import (
    MAX_SIGNED_INT64,
    encode_public_run_reference,
)
from atelier2.api.stream import (
    BoundedQueryRunner,
    EventPollBackoff,
    PreparedEventStream,
    stream_server_events,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptId,
    AgentAttemptReplacement,
)
from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS
from atelier2.contracts.effects import LogicalEffectKey
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEvent,
    RunEventAgentAttemptBinding,
    RunEventCancellationBinding,
    RunEventKind,
)
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.pages import PageLimit
from atelier2.contracts.run_events import (
    PersistedRunEvent,
    RunEventPage,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.run_events import (
    EventHistoryCorrupt,
    PrepareRunEventStreamResult,
    ReadRunEventPageResult,
    StreamReady,
)
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
    QueryDurableStateCorrupt,
    ReadUnavailable,
)
from tests.scenarios.api import (
    api_limits,
    event_stream_client,
    stream_page_reader,
    stream_run_projection,
    unused_attention_event_page,
)

READY_NONTERMINAL_HEAD = StreamReady(1, False, 0)
RUN_ID = RunId("failing-stream")
REVISION_HASH = WorkflowRevisionHash("0" * 64)
EVENT_TARGET = EVENT_PATH.replace("{public_ref}", encode_public_run_reference(RUN_ID))


class OnePageQueries:
    """Serve one prepared page result and refuse every read after it."""

    def __init__(
        self,
        page: ReadRunEventPageResult,
        prepared: PrepareRunEventStreamResult | None = None,
    ) -> None:
        self.page = page
        self.prepared = READY_NONTERMINAL_HEAD if prepared is None else prepared
        self.page_calls = 0

    def prepare_run_event_stream(
        self, run_id: RunId, after_sequence: int
    ) -> PrepareRunEventStreamResult:
        del run_id, after_sequence
        return self.prepared

    def read_run_event_page(
        self,
        run_id: RunId,
        after_sequence: int,
        limit: int,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ReadRunEventPageResult:
        del run_id, after_sequence, limit, projection_limit
        self.page_calls += 1
        if self.page_calls > 1:
            raise AssertionError("a terminal failure frame did not end the stream")
        return self.page

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


def persisted_event(
    sequence: int, kind: RunEventKind, payload: bytes
) -> PersistedRunEvent:
    receipt_bound = kind in {
        RunEventKind.ACTION_RECONCILIATION_RESOLVED,
        RunEventKind.ACTION_COMPLETED,
    }
    return PersistedRunEvent(
        RunEvent(
            RUN_ID,
            REVISION_HASH,
            sequence,
            "agent",
            NodeExecutionId.for_node(RUN_ID, REVISION_HASH, "agent"),
            kind,
            payload,
            receipt_logical_key=(
                LogicalEffectKey("receipt-key") if receipt_bound else None
            ),
            receipt_result_hash=Sha256Hash.of(payload) if receipt_bound else None,
        ),
        None,
    )


def persisted_v1_cancellation() -> PersistedRunEvent:
    """A V1 projection carrying a kind only a V2 run can produce."""

    return PersistedRunEvent(
        RunEvent(
            RUN_ID,
            REVISION_HASH,
            1,
            "agent",
            NodeExecutionId.for_node(RUN_ID, REVISION_HASH, "agent"),
            RunEventKind.AGENT_CANCELLED,
            b"",
            attempt_binding=RunEventCancellationBinding(
                AgentAttemptId("a" * 64),
                1,
                AgentAttemptReplacement.NONE,
                "command",
                AgentAttemptCancellationDisposition.NEVER_LAUNCHED,
            ),
        ),
        None,
        workflow_format_version=WorkflowFormatVersion.V1,
    )


def persisted_v3_failure_with_overlong_receipt_reason(
    run_id: RunId = RUN_ID, node_id: str = "agent"
) -> PersistedRunEvent:
    return PersistedRunEvent(
        RunEvent(
            run_id,
            REVISION_HASH,
            1,
            node_id,
            NodeExecutionId.for_node(run_id, REVISION_HASH, node_id),
            RunEventKind.AGENT_FAILED,
            b"OUTPUT_SCHEMA_REFUSED",
            attempt_binding=RunEventAgentAttemptBinding(AgentAttemptId("a" * 64), 1),
        ),
        None,
        WorkflowFormatVersion.V3,
        "x" * (MAXIMUM_AGENT_FIELD_CHARACTERS + 1),
    )


def one_event_page(
    kind: RunEventKind, payload: bytes, sequence: int = 1
) -> RunEventPage:
    return RunEventPage((persisted_event(sequence, kind, payload),), False)


def streamed_frames(
    queries: OnePageQueries,
    *,
    page_size: int = 1,
    limit_changes: dict[str, int] | None = None,
) -> list[ServerSentEvent]:
    async def collect() -> list[ServerSentEvent]:
        return [
            frame
            async for frame in stream_server_events(
                PreparedEventStream(
                    RUN_ID, 0, 1, False, stream_run_projection(RUN_ID.value)
                ),
                stream_page_reader(queries),
                BoundedQueryRunner(1, admission_timeout_seconds=1),
                page_size=PageLimit(page_size),
                limits=api_limits(**(limit_changes or {})),
                poll_backoff=EventPollBackoff(0.01, 0.04, 2),
            )
        ]

    return asyncio.run(collect())


def terminal_problem(frames: list[ServerSentEvent]) -> dict[str, Any]:
    assert len(frames) == 1, "a failed stream carries exactly one terminal frame"
    frame = frames[0]
    assert frame.id is None, "a failure frame must not offer itself as a resume cursor"
    payload: dict[str, Any] = json.loads(frame.data.model_dump_json())
    assert payload["event"] == "STREAM_FAILED"
    return payload["problem"]


def failed_stream_problem(
    queries: OnePageQueries,
    *,
    page_size: int = 1,
    limit_changes: dict[str, int] | None = None,
) -> dict[str, Any]:
    return terminal_problem(
        streamed_frames(queries, page_size=page_size, limit_changes=limit_changes)
    )


def problem_identity(problem: dict[str, Any]) -> tuple[str, str, int]:
    return (problem["type"], problem["title"], problem["status"])


def promised_stream_problems(document: dict[str, Any]) -> set[tuple[str, str, int]]:
    """Every problem body the published document allows inside a failure frame."""

    components = document["components"]["schemas"]
    options = components["StreamFailureResource"]["properties"]["problem"]["oneOf"]
    promised = (components[option["$ref"].rsplit("/", 1)[-1]] for option in options)
    return {
        (
            component["properties"]["type"]["const"],
            component["properties"]["title"]["const"],
            component["properties"]["status"]["const"],
        )
        for component in promised
    }


@pytest.mark.parametrize(
    "page",
    [EventHistoryCorrupt(), QueryDurableStateCorrupt()],
    ids=("event-history-corrupt", "durable-state-corrupt"),
)
def test_a_corrupt_durable_read_ends_the_stream_as_durable_corruption(
    page: ReadRunEventPageResult,
) -> None:
    problem = failed_stream_problem(OnePageQueries(page))

    assert problem["type"].endswith(":durable-state-corrupt")
    assert problem["status"] == 500


def test_a_temporarily_unavailable_read_ends_the_stream_for_native_reconnection() -> (
    None
):
    frames = streamed_frames(OnePageQueries(ReadUnavailable("the store is restarting")))

    assert frames == []


def test_an_over_long_port_page_ends_the_stream_as_a_named_server_fault() -> None:
    page = RunEventPage(
        (
            persisted_event(1, RunEventKind.AGENT_COMPLETED, b"one"),
            persisted_event(2, RunEventKind.AGENT_COMPLETED, b"two"),
        ),
        False,
    )

    problem = failed_stream_problem(OnePageQueries(page))

    assert problem["type"].endswith(":internal-error")


def test_an_event_beyond_the_configured_limits_ends_the_stream_as_unavailable() -> None:
    page = one_event_page(RunEventKind.AGENT_COMPLETED, b"oversized")

    problem = failed_stream_problem(
        OnePageQueries(page), limit_changes={"maximum_decoded_payload_bytes": 4}
    )

    assert problem["type"].endswith(":temporarily-unavailable")
    assert problem["detail"] == PROJECTION_LIMIT_DETAIL


def test_a_run_stream_names_an_omitted_overlong_receipt_reason() -> None:
    frames = streamed_frames(
        OnePageQueries(
            RunEventPage((persisted_v3_failure_with_overlong_receipt_reason(),), True)
        )
    )

    assert len(frames) == 1
    event: dict[str, Any] = json.loads(frames[0].data.model_dump_json())
    assert event["event"] == "AGENT_FAILED"
    reason = event["reason"]
    assert reason is not None
    assert not ("x" * (MAXIMUM_AGENT_FIELD_CHARACTERS + 1)).startswith(reason)
    assert "omitted" in reason
    assert (
        "GET /atelier/api/v1/runs/"
        f"{encode_public_run_reference(RUN_ID)}/nodes/agent"
    ) in reason
    assert len(reason) <= api_limits().maximum_field_characters


def test_an_omitted_receipt_reason_percent_encodes_its_real_node_detail_path() -> None:
    projection = persisted_v3_failure_with_overlong_receipt_reason(
        RunId("summary-route"), "agent / review"
    )

    summary = bounded_event_summary(projection).node_receipt_reason
    encoded_node_id = quote(projection.event.node_id, safe="-_.!~*'()")

    assert summary is not None
    assert (
        "GET /atelier/api/v1/runs/"
        f"{encode_public_run_reference(projection.event.run_id)}/nodes/"
        f"{encoded_node_id}"
    ) in summary


def test_an_omitted_receipt_reason_names_event_fields_when_its_detail_path_is_too_long() -> None:
    projection = persisted_v3_failure_with_overlong_receipt_reason(
        RunId("r" * MAXIMUM_AGENT_FIELD_CHARACTERS),
        "n" * MAXIMUM_AGENT_FIELD_CHARACTERS,
    )

    summary = bounded_event_summary(projection).node_receipt_reason
    stored_reason = projection.node_receipt_reason

    assert summary is not None
    assert stored_reason is not None
    assert not stored_reason.startswith(summary)
    assert "omitted" in summary
    assert "node-detail resource" in summary
    assert "public_run_reference" in summary
    assert "node_id" in summary
    assert len(summary) <= MAXIMUM_AGENT_FIELD_CHARACTERS


@pytest.mark.parametrize(
    ("kind", "payload", "sequence"),
    [
        (RunEventKind.AGENT_COMPLETED, b"\xff", 1),
        (RunEventKind.AGENT_COMPLETED, b"valid", MAX_SIGNED_INT64 + 1),
        (RunEventKind.WAIT_ANSWERED, b"01", 1),
        (RunEventKind.SUBWORKFLOW_COMPLETED, b"+1", 1),
        (RunEventKind.ACTION_RECONCILIATION_RESOLVED, b"result", 1),
    ],
    ids=(
        "invalid-utf8",
        "sequence-beyond-int64",
        "wait-integer",
        "subworkflow-integer",
        "missing-receipt",
    ),
)
def test_an_unprojectable_durable_row_ends_the_stream_as_durable_corruption(
    kind: RunEventKind, payload: bytes, sequence: int
) -> None:
    page = one_event_page(kind, payload, sequence)

    problem = failed_stream_problem(OnePageQueries(page))

    assert problem["type"].endswith(":durable-state-corrupt")


def test_a_v1_row_carrying_a_v2_only_kind_ends_the_stream_as_durable_corruption() -> (
    None
):
    """The escape became a named refusal, which is what #88 changed here.

    A V1 row carrying a kind no V1 run produces used to leave the closed union
    through a fall-through assertion, and the stream could only call that an
    internal error. One owner for the V1 vocabulary makes it what it always was:
    a durable row the projection refuses by name.
    """
    page = RunEventPage((persisted_v1_cancellation(),), False)

    problem = failed_stream_problem(OnePageQueries(page))

    assert problem["type"].endswith(":durable-state-corrupt")


def test_one_corrupt_history_reports_one_problem_before_and_inside_the_stream() -> None:
    refused_before_headers = event_stream_client(
        OnePageQueries(EventHistoryCorrupt(), EventHistoryCorrupt())
    ).get(EVENT_TARGET)
    failed_inside_the_stream = event_stream_client(
        OnePageQueries(EventHistoryCorrupt())
    ).get(EVENT_TARGET)

    assert refused_before_headers.status_code == 500
    assert refused_before_headers.headers["content-type"] == "application/problem+json"
    assert failed_inside_the_stream.status_code == 200
    assert failed_inside_the_stream.headers["content-type"].startswith(
        "text/event-stream"
    )
    frame = json.loads(
        failed_inside_the_stream.text.split("data: ", 1)[1].split("\n", 1)[0]
    )
    assert frame == {
        "event": "STREAM_FAILED",
        "problem": refused_before_headers.json(),
    }


def test_the_document_promises_exactly_the_problem_bodies_the_stream_can_emit() -> None:
    emitted = {
        problem_identity(failed_stream_problem(OnePageQueries(EventHistoryCorrupt()))),
        problem_identity(
            failed_stream_problem(
                OnePageQueries(
                    one_event_page(RunEventKind.AGENT_COMPLETED, b"oversized")
                ),
                limit_changes={"maximum_decoded_payload_bytes": 4},
            )
        ),
        problem_identity(
            failed_stream_problem(
                OnePageQueries(RunEventPage((persisted_v1_cancellation(),), False))
            )
        ),
        # The V1-vocabulary row above now refuses by name, so the internal-error
        # promise needs the path that still reaches it: a store handing back more
        # events than the page that was asked for.
        problem_identity(
            failed_stream_problem(
                OnePageQueries(
                    RunEventPage(
                        (
                            persisted_event(1, RunEventKind.AGENT_COMPLETED, b"a"),
                            persisted_event(2, RunEventKind.AGENT_COMPLETED, b"b"),
                        ),
                        False,
                    )
                ),
                limit_changes={"event_page_size": 1},
            )
        ),
    }

    document = (
        event_stream_client(OnePageQueries(EventHistoryCorrupt()))
        .get(API_PREFIX + "/openapi.json")
        .json()
    )

    assert emitted == promised_stream_problems(document)
