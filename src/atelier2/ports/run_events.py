from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.run_events import PersistedRunEvent, RunEventPage
from atelier2.contracts.runs import RunId
from atelier2.contracts.when import RecordedAt
from atelier2.ports.run_queries import (
    RunQueryMissing,
)
from atelier2.ports.workflow_revisions import (
    ProjectionTooLarge,
    QueryDurableStateCorrupt,
    ReadUnavailable,
)


@dataclass(frozen=True)
class StreamReady:
    head_sequence: int
    terminal: bool
    first_after: int


@dataclass(frozen=True)
class CursorAhead:
    pass


@dataclass(frozen=True)
class EventHistoryCorrupt:
    pass


type PrepareRunEventStreamResult = (
    StreamReady
    | RunQueryMissing
    | CursorAhead
    | EventHistoryCorrupt
    | ReadUnavailable
    | QueryDurableStateCorrupt
)
type ReadRunEventPageResult = (
    RunEventPage
    | EventHistoryCorrupt
    | ReadUnavailable
    | ProjectionTooLarge
    | QueryDurableStateCorrupt
)


@dataclass(frozen=True)
class AttentionEvent:
    """One attention row together with the instant the resume predicate uses."""

    event: PersistedRunEvent
    recorded_at: RecordedAt


@dataclass(frozen=True)
class AttentionEventPage:
    events: tuple[AttentionEvent, ...]


@dataclass(frozen=True)
class AttentionCursorUnknown:
    """Last-Event-ID is not a WAITING_INPUT or AGENT_FAILED event with a V22 instant."""


type ReadAttentionEventPageResult = (
    AttentionEventPage
    | AttentionCursorUnknown
    | ReadUnavailable
    | ProjectionTooLarge
    | QueryDurableStateCorrupt
)


class RunEventQueries(Protocol):
    def prepare_run_event_stream(
        self, run_id: RunId, after_sequence: int
    ) -> PrepareRunEventStreamResult: ...

    def read_run_event_page(
        self,
        run_id: RunId,
        after_sequence: int,
        limit: int,
    ) -> ReadRunEventPageResult: ...

    def read_attention_event_page(
        self,
        after_run_id: RunId | None,
        after_sequence: int | None,
        limit: int,
        excluded_identities: tuple[tuple[RunId, int], ...],
    ) -> ReadAttentionEventPageResult: ...
