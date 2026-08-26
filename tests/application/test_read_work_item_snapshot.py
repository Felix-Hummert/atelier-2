"""The snapshot use case: a tracker answer becomes a pinned revision, by project."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Never

import pytest

from atelier2.application.read_work_item_snapshot import (
    WorkItemNotInTracker,
    WorkItemSnapshotRead,
    read_work_item_snapshot,
)
from atelier2.application.refusals import (
    ProjectSourceNotConnected,
    ReadUnavailable,
    SourcePayloadMalformed,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import TrackerItemReference, WorkItemReference
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.ports.issue_observation import (
    ObserveWorkItemRevisionResult,
    TrackerItemUnknown,
    TrackerPayloadMalformed,
    TrackerSourceUnavailable,
    WorkItemRevisionObserved,
)

PROJECT = ProjectId("studio")
ITEM = TrackerItemReference("gh:712")

REVISION = ObservedWorkItemRevision(
    ITEM,
    WorkItemKind.ISSUE,
    b"what the item said",
    WorkItemChangeMarker('W/"5f2a"'),
    RecordedAt("2026-08-26T09:15:00Z"),
)


@dataclass
class _SnapshotSource:
    answer: ObserveWorkItemRevisionResult
    asked_for: list[TrackerItemReference] = field(default_factory=list)

    def open_items(self) -> Never:
        raise AssertionError("a snapshot never lists the open items")

    def snapshot(
        self, reference: TrackerItemReference
    ) -> ObserveWorkItemRevisionResult:
        self.asked_for.append(reference)
        return self.answer


def test_an_observed_revision_is_named_under_the_served_projects_item_identity() -> (
    None
):
    source = _SnapshotSource(WorkItemRevisionObserved(REVISION))

    outcome = read_work_item_snapshot(PROJECT, source, ITEM)

    assert outcome == WorkItemSnapshotRead(PROJECT, REVISION)
    assert isinstance(outcome, WorkItemSnapshotRead)
    assert outcome.item_reference == WorkItemReference(PROJECT, ITEM)
    assert source.asked_for == [ITEM]


def test_a_source_answering_about_another_item_is_refused_not_recorded() -> None:
    """A revision for `gh:713` must never land under the name `gh:712` was read by."""

    other = ObservedWorkItemRevision(
        TrackerItemReference("gh:713"),
        WorkItemKind.ISSUE,
        b"the other item's text",
        WorkItemChangeMarker('W/"5f2a"'),
        RecordedAt("2026-08-26T09:15:00Z"),
    )

    outcome = read_work_item_snapshot(
        PROJECT, _SnapshotSource(WorkItemRevisionObserved(other)), ITEM
    )

    assert isinstance(outcome, SourcePayloadMalformed)


def test_an_item_the_tracker_does_not_hold_is_named_by_its_item_identity() -> None:
    source = _SnapshotSource(TrackerItemUnknown(ITEM))

    outcome = read_work_item_snapshot(PROJECT, source, ITEM)

    assert outcome == WorkItemNotInTracker(WorkItemReference(PROJECT, ITEM))


@pytest.mark.parametrize(
    ("project", "source"),
    [
        (None, _SnapshotSource(WorkItemRevisionObserved(REVISION))),
        (PROJECT, None),
    ],
)
def test_an_unconnected_instance_is_refused_without_reading_the_tracker(
    project: ProjectId | None, source: _SnapshotSource | None
) -> None:
    outcome = read_work_item_snapshot(project, source, ITEM)

    assert outcome == ProjectSourceNotConnected()
    assert source is None or source.asked_for == []


@pytest.mark.parametrize(
    ("source_answer", "expected"),
    [
        (
            TrackerSourceUnavailable("GitHub answered 503"),
            ReadUnavailable("GitHub answered 503"),
        ),
        (
            TrackerPayloadMalformed("no body field"),
            SourcePayloadMalformed("no body field"),
        ),
    ],
)
def test_a_tracker_refusal_is_translated_into_this_layers_word(
    source_answer: ObserveWorkItemRevisionResult, expected: object
) -> None:
    outcome = read_work_item_snapshot(PROJECT, _SnapshotSource(source_answer), ITEM)

    assert outcome == expected
