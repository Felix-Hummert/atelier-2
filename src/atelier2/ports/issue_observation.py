"""Which open work items the connected tracker holds, as references and no more.

The queue keys orchestration state by a reference into whichever tracker holds
the item (REQ-QUEUE-14); this port is how those references enter. A source
answers with the tracker's open items in the reference grammar its own adapter
owns (`gh:<n>` for GitHub, ADR 0010) -- never titles, bodies, or labels, which
stay the tracker's. What becomes durable from an answer is the caller's
decision; this port only observes.

Provider output is external input: a source validates the payload it was given
and answers `TrackerPayloadMalformed` for a shape it refuses, rather than
letting an unvalidated provider answer reach the queue's durable write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.queue_projection import TrackerItemReference


@dataclass(frozen=True)
class OpenTrackerItemsObserved:
    """Every open item the tracker holds right now, as adapter-owned references."""

    references: tuple[TrackerItemReference, ...]


@dataclass(frozen=True)
class TrackerSourceUnavailable:
    """The tracker did not answer this observation; a later attempt may succeed."""

    detail: str


@dataclass(frozen=True)
class TrackerPayloadMalformed:
    """The tracker answered with a shape this source refuses to read items from.

    Not a retry case: the same request will earn the same answer until the
    provider or the adapter changes, so the refusal names what was wrong.
    """

    detail: str


type ObserveOpenTrackerItemsResult = (
    OpenTrackerItemsObserved | TrackerSourceUnavailable | TrackerPayloadMalformed
)


class TrackerItemSource(Protocol):
    """One connected tracker, observed for the open items it currently holds."""

    def open_items(self) -> ObserveOpenTrackerItemsResult: ...
