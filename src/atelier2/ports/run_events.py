from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.effects import EffectReceipt
from atelier2.contracts.executions import RunEvent
from atelier2.contracts.runs import RunId
from atelier2.ports.run_queries import RunQueryMissing
from atelier2.ports.workflow_revisions import (
    DurableProjectionLimit,
    QueryDurableStateCorrupt,
    ReadUnavailable,
)


@dataclass(frozen=True)
class PersistedRunEvent:
    event: RunEvent
    receipt: EffectReceipt | None


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


@dataclass(frozen=True)
class RunEventPage:
    events: tuple[PersistedRunEvent, ...]
    terminal_seen: bool


type PrepareRunEventStreamResult = (
    StreamReady
    | RunQueryMissing
    | CursorAhead
    | EventHistoryCorrupt
    | ReadUnavailable
    | QueryDurableStateCorrupt
)
type ReadRunEventPageResult = (
    RunEventPage | EventHistoryCorrupt | ReadUnavailable | QueryDurableStateCorrupt
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
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ReadRunEventPageResult: ...
