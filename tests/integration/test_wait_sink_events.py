"""A finished Wait-sink run is a run whose history opens, not one reported corrupt.

`test_v3_self_driving_run.py` proved the same story for a V3 line ending on its
**agent** sink: the stream pre-flight knew only one spelling of a V3 ending and
called every other finished V3 line's honest history corrupt. `test_v3_wait_run.py`
proved a Wait node standing last carries its own run to COMPLETED. Neither head
proved that a COMPLETED Wait-sink run can actually be *read back* -- and it
could not: the pre-flight's V3 ending set named `AGENT_COMPLETED`,
`AGENT_FAILED` and `ACTION_COMPLETED` but not `WAIT_ANSWERED`, so a run that
paused for a person and then finished on their answer left every reader of its
own event stream a `durable-state-corrupt` 500 (#510).

What is driven here is the whole surface an operator's cockpit actually calls:
the durable page read the stream's first page carries, and the real HTTP SSE
route behind it -- both against a run the public start-and-answer path finished
on its own.
"""

from __future__ import annotations

import json
from typing import cast

from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.api.references import encode_public_run_reference
from atelier2.application.answer_wait import AnswerAcceptedPending
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.runs import RunState
from atelier2.ports.run_events import RunEventPage, StreamReady
from tests.integration.test_v3_wait_run import (
    ANSWER,
    RUN,
    WAIT_AS_THE_SINK,
    WAIT_NODE,
    answer,
    start_and_launch,
    wait_for_state,
)
from tests.integration.test_v3_wait_run import runtime as wait_runtime
from tests.scenarios.agents import RecordingAgentExecutorFactoryV2
from tests.scenarios.api import durable_api_client, durable_queries

runtime = wait_runtime


def _parse_sse_events(body: str) -> list[dict[str, object]]:
    parsed: list[dict[str, object]] = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", maxsplit=1) for line in block.splitlines())
        assert set(fields) == {"id", "data"}
        parsed.append({"id": fields["id"], "data": json.loads(fields["data"])})
    return parsed


def test_a_wait_sink_run_reads_its_events_back_as_a_page(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The durable page read behind the stream opens on a Wait-sink run.

    Before #510, this run's own answer path finished it as COMPLETED while
    every reader of its events answered `durable-state-corrupt`, because the
    pre-flight's V3 ending set did not know `WAIT_ANSWERED`.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_AS_THE_SINK)
    wait_for_state(started, RunState.WAITING_INPUT)

    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)
    wait_for_state(started, RunState.COMPLETED)

    queries = durable_queries(started.engine)
    prepared = queries.prepare_run_event_stream(RUN, 0)
    page = queries.read_run_event_page(RUN, 0, 50)

    assert isinstance(prepared, StreamReady), prepared
    assert prepared.terminal is True
    assert isinstance(page, RunEventPage), page
    assert page.terminal_seen is True
    assert [event.event.node_id for event in page.events] == [
        "implement",
        WAIT_NODE,
        WAIT_NODE,
    ]
    assert [event.event.event_kind for event in page.events] == [
        RunEventKind.AGENT_COMPLETED,
        RunEventKind.WAITING_INPUT,
        RunEventKind.WAIT_ANSWERED,
    ]


def test_a_wait_sink_run_streams_its_events_over_the_real_sse_route(
    runtime: tuple[DbosRuntime, RecordingAgentExecutorFactoryV2],
) -> None:
    """The HTTP route a cockpit actually opens answers 200 with the full history.

    Driven through the composed app rather than the port directly, so what is
    asserted is the same 500-vs-200 an operator's browser would have seen.
    """
    started, _ = runtime
    workflow = start_and_launch(started, WAIT_AS_THE_SINK)
    wait_for_state(started, RunState.WAITING_INPUT)

    assert isinstance(answer(started, workflow, ANSWER), AnswerAcceptedPending)
    wait_for_state(started, RunState.COMPLETED)

    client = durable_api_client(started)
    path = f"/atelier/api/v1/runs/{encode_public_run_reference(RUN)}/events"

    response = client.get(path)

    assert response.status_code == 200, response.text
    events = _parse_sse_events(response.text)
    assert [cast(dict[str, object], event["data"])["event"] for event in events] == [
        "AGENT_COMPLETED",
        "WAITING_INPUT",
        "WAIT_ANSWERED",
    ]
