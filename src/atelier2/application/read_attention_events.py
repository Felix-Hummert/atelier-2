"""Reading one page of the attention feed, as a decision rather than a port answer.

GET /events is a loop around this decision. The loop belongs to the API — how
long to wait, when to reconnect, what the query budget allows — but what one
page means does not: the store either answered with the next WAITING_INPUT and
AGENT_FAILED events after a cursor, or it could not place that cursor on this
feed, or it answered with something no sequence of writes could have produced.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ProjectionTooLarge,
    ReadUnavailable,
)
from atelier2.contracts.run_events import PersistedRunEvent
from atelier2.contracts.runs import RunId
from atelier2.ports.run_events import (
    AttentionCursorUnknown as PortAttentionCursorUnknown,
)
from atelier2.ports.run_events import (
    AttentionEventPage,
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
class AttentionEventsRead:
    events: tuple[PersistedRunEvent, ...]


@dataclass(frozen=True)
class AttentionCursorUnknown:
    pass


@dataclass(frozen=True)
class AttentionEventPageOversized:
    """The store answered with more events than the page it was asked for."""


type ReadAttentionEventsResult = (
    AttentionEventsRead
    | AttentionCursorUnknown
    | AttentionEventPageOversized
    | ReadUnavailable
    | ProjectionTooLarge
    | DurableStateCorrupt
)


def read_attention_events(
    after_run_id: RunId | None,
    after_sequence: int | None,
    page_size: int,
    queries: RunEventQueries,
) -> ReadAttentionEventsResult:
    """One page of attention events after a cursor, or why there is none to hand on."""
    match queries.read_attention_event_page(after_run_id, after_sequence, page_size):
        case AttentionEventPage(events):
            if len(events) > page_size:
                return AttentionEventPageOversized()
            return AttentionEventsRead(events)
        case PortAttentionCursorUnknown():
            return AttentionCursorUnknown()
        case PortReadUnavailable(detail):
            return ReadUnavailable(detail)
        case PortProjectionTooLarge():
            return ProjectionTooLarge()
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
