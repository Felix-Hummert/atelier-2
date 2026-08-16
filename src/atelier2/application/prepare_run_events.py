"""Deciding whether a run's event stream can be opened, and from where.

This is the read that happens before a stream exists: it settles the run, the
cursor and the head the stream will start from, and every way that can fail is a
member of this union. Forwarding the pages afterwards is the stream's own work
and stays where it is.

The store answers a corrupt history and a corrupt run state with two different
words for one caller decision, so this use-case collapses them here rather than
leaving every caller to spell the pair out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.runs import RunId
from atelier2.ports.run_events import (
    CursorAhead,
    EventHistoryCorrupt,
    RunEventQueries,
    StreamReady,
)
from atelier2.ports.run_queries import (
    RunQueryMissing,
)
from atelier2.ports.workflow_revisions import (
    QueryDurableStateCorrupt,
)
from atelier2.ports.workflow_revisions import (
    ReadUnavailable as PortReadUnavailable,
)


@dataclass(frozen=True)
class RunEventStreamPrepared:
    run_id: RunId
    first_after: int
    head_sequence: int
    terminal: bool


@dataclass(frozen=True)
class RunNotFound:
    pass


@dataclass(frozen=True)
class EventCursorAhead:
    pass


type PrepareRunEventsResult = (
    RunEventStreamPrepared
    | RunNotFound
    | EventCursorAhead
    | ReadUnavailable
    | DurableStateCorrupt
)


def prepare_run_events(
    run_id: RunId, after_sequence: int, queries: RunEventQueries
) -> PrepareRunEventsResult:
    match queries.prepare_run_event_stream(run_id, after_sequence):
        case StreamReady(head_sequence, terminal, first_after):
            return RunEventStreamPrepared(run_id, first_after, head_sequence, terminal)
        case RunQueryMissing():
            return RunNotFound()
        case CursorAhead():
            return EventCursorAhead()
        case EventHistoryCorrupt() | QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case _ as unreachable:
            assert_never(unreachable)
