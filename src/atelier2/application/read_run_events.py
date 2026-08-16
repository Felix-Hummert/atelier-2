"""Reading one page of a run's events, as a decision rather than a port answer.

A stream is a loop around this decision. The loop belongs to the API — how long to
wait, when to reconnect, what the query budget allows — but *what one page means*
does not: the store either answered with a page, or it could not answer, or it
answered with something no sequence of writes could have produced.

That last case is why this is a use-case and not a passthrough. A store that
returns more events than the page it was asked for has contradicted its own
contract, and deciding that is a judgement about durable state, not about HTTP.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
)
from atelier2.contracts.runs import RunId
from atelier2.ports.run_events import (
    EventHistoryCorrupt,
    PersistedRunEvent,
    RunEventPage,
    RunEventQueries,
)
from atelier2.ports.workflow_revisions import (
    ProjectionTooLarge as PortProjectionTooLarge,
)
from atelier2.ports.workflow_revisions import (
    QueryDurableStateCorrupt,
)
from atelier2.ports.workflow_revisions import (
    ReadUnavailable as PortReadUnavailable,
)


@dataclass(frozen=True)
class RunEventsRead:
    events: tuple[PersistedRunEvent, ...]
    terminal_seen: bool


@dataclass(frozen=True)
class RunEventPageOversized:
    """The store answered with more events than the page it was asked for.

    Kept apart from a corrupt history on purpose: nothing about the stored events
    is known to be wrong, so this is our own contract that broke, and a caller
    says so in its own words rather than blaming the history.
    """


type ReadRunEventsResult = (
    RunEventsRead
    | RunEventPageOversized
    | ReadUnavailable
    | ProjectionTooLarge
    | DurableStateCorrupt
)


def read_run_events(
    run_id: RunId,
    after_sequence: int,
    page_size: int,
    queries: RunEventQueries,
) -> ReadRunEventsResult:
    """One page of events after a cursor, or why there is none to hand on."""
    match queries.read_run_event_page(run_id, after_sequence, page_size):
        case RunEventPage(events, terminal_seen):
            if len(events) > page_size:
                return RunEventPageOversized()
            return RunEventsRead(events, terminal_seen)
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortProjectionTooLarge():
            return ProjectionTooLarge()
        case EventHistoryCorrupt() | QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
