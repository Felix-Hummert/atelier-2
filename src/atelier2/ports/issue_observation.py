"""What the connected tracker holds: which items are open, and what one says.

The queue keys orchestration state by a reference into whichever tracker holds
the item (REQ-QUEUE-14); this port is how those references enter. A source
answers with the tracker's open items in the reference grammar its own adapter
owns (`gh:<n>` for GitHub, ADR 0010), plus each item's title as the tracker
last served it. The title is an observation of a tracker-owned fact, not core
truth (ADR 0016, 2026-09-01 amendment). Labels and the item's own lifecycle do
not cross this boundary. What becomes durable from an answer is the caller's
decision; this port only observes.

Reading one named item is this port's second operation, and no more than that
(ADR 0010 decision 1, 2026-08-26 amendment): a snapshot is the observed
revision ADR 0010 §5 already defines -- the served bytes, their digest, the
item's identity and the read's change marker -- so a run pins the material it
read instead of re-reading a moving object. Nothing here enumerates what a
platform is capable of, or how its references are spelled; those grow their own
contract when a caller needs them.

Provider output is external input: a source validates the payload it was given
and answers `TrackerPayloadMalformed` for a shape it refuses, rather than
letting an unvalidated provider answer reach the queue's durable write.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.work_items import ObservedWorkItemRevision


@dataclass(frozen=True)
class ObservedOpenTrackerItem:
    """One open tracker item, with the title the tracker served for it."""

    reference: TrackerItemReference
    title: str


@dataclass(frozen=True)
class OpenTrackerItemsObserved:
    """Every open item the tracker holds right now, with its observed title."""

    items: tuple[ObservedOpenTrackerItem, ...]

    __match_args__ = ("references",)

    @property
    def references(self) -> tuple[TrackerItemReference, ...]:
        """The open references this observation carries, in listing order."""

        return tuple(item.reference for item in self.items)


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


@dataclass(frozen=True)
class WorkItemRevisionObserved:
    """The item stood at these bytes when this source read it."""

    revision: ObservedWorkItemRevision


@dataclass(frozen=True)
class TrackerItemUnknown:
    """This source addresses no item at that reference, and a retry changes nothing.

    Either the tracker answered that the item is not there, or the reference is
    not in this source's own grammar at all -- from the caller's side both say
    the same thing about the connected tracker, and neither is a read that may
    yet succeed.
    """

    reference: TrackerItemReference


type ObserveWorkItemRevisionResult = (
    WorkItemRevisionObserved
    | TrackerItemUnknown
    | TrackerSourceUnavailable
    | TrackerPayloadMalformed
)


class TrackerItemSource(Protocol):
    """One connected tracker: the open items it holds, and what one of them says."""

    def open_items(self) -> ObserveOpenTrackerItemsResult: ...

    def snapshot(
        self, reference: TrackerItemReference
    ) -> ObserveWorkItemRevisionResult: ...
