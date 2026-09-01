"""The import use case: tracker answers become queue observations in this layer's words."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Never

import pytest

from atelier2.application.import_project_source_issues import (
    ProjectSourceIssuesImported,
    ProjectSourceNotConnected,
    SourcePayloadMalformed,
    import_project_source_issues,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import TrackerItemReference, WorkItemReference
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.issue_observation import (
    ObservedOpenTrackerItem,
    ObserveOpenTrackerItemsResult,
    OpenTrackerItemsObserved,
    TrackerPayloadMalformed,
    TrackerSourceUnavailable,
)
from atelier2.ports.queue_projection import ObserveQueueItemsResult, QueueItemsObserved
from tests.scenarios.issue_observation import FakeTrackerItemSource

PROJECT = ProjectId("studio")


@dataclass
class _QueueRecording:
    answer: ObserveQueueItemsResult
    observed_batches: list[tuple[WorkItemReference, ...]] = field(default_factory=list)

    def observe(
        self, references: tuple[WorkItemReference, ...]
    ) -> ObserveQueueItemsResult:
        self.observed_batches.append(references)
        return self.answer

    def admit(self, command: object) -> Never:
        raise AssertionError("an import never admits")

    def list_observed_items(self, after: object, limit: object) -> Never:
        raise AssertionError("an import never reads the observed list")

    def list_admitted_items(self, after: object, limit: object) -> Never:
        raise AssertionError("an import never reads the admitted list")


def test_open_tracker_items_become_one_observation_batch_for_the_served_project() -> (
    None
):
    source = FakeTrackerItemSource(
        open_items_answer=OpenTrackerItemsObserved(
            (
                ObservedOpenTrackerItem(
                    TrackerItemReference("gh:79"), "First listed item"
                ),
                ObservedOpenTrackerItem(
                    TrackerItemReference("gh:652"), "Second listed item"
                ),
            )
        )
    )
    queue = _QueueRecording(QueueItemsObserved(observed=2, newly_observed=1))

    outcome = import_project_source_issues(PROJECT, source, queue)

    assert outcome == ProjectSourceIssuesImported(observed=2, newly_observed=1)
    assert queue.observed_batches == [
        (
            WorkItemReference(PROJECT, TrackerItemReference("gh:79")),
            WorkItemReference(PROJECT, TrackerItemReference("gh:652")),
        )
    ]


@pytest.mark.parametrize(
    ("project", "source"),
    [
        (None, FakeTrackerItemSource(open_items_answer=OpenTrackerItemsObserved(()))),
        (PROJECT, None),
    ],
)
def test_an_unconnected_instance_is_refused_by_name_without_touching_the_queue(
    project: ProjectId | None, source: FakeTrackerItemSource | None
) -> None:
    queue = _QueueRecording(QueueItemsObserved(0, 0))

    outcome = import_project_source_issues(project, source, queue)

    assert outcome == ProjectSourceNotConnected()
    assert queue.observed_batches == []


@pytest.mark.parametrize(
    ("source_answer", "expected"),
    [
        (
            TrackerSourceUnavailable("GitHub answered 503"),
            ReadUnavailable("GitHub answered 503"),
        ),
        (
            TrackerPayloadMalformed("no numbers"),
            SourcePayloadMalformed("no numbers"),
        ),
    ],
)
def test_a_tracker_refusal_is_translated_and_writes_nothing(
    source_answer: ObserveOpenTrackerItemsResult, expected: object
) -> None:
    queue = _QueueRecording(QueueItemsObserved(0, 0))

    outcome = import_project_source_issues(
        PROJECT, FakeTrackerItemSource(open_items_answer=source_answer), queue
    )

    assert outcome == expected
    assert queue.observed_batches == []


@pytest.mark.parametrize(
    ("store_answer", "expected"),
    [
        (DurableWriteUnavailable(), WriteUnavailable()),
        (PortDurableStateCorrupt(), DurableStateCorrupt()),
    ],
)
def test_a_store_failure_is_translated_into_this_layers_word(
    store_answer: ObserveQueueItemsResult, expected: object
) -> None:
    source = FakeTrackerItemSource(
        open_items_answer=OpenTrackerItemsObserved(
            (ObservedOpenTrackerItem(TrackerItemReference("gh:1"), "Listed item"),)
        )
    )

    outcome = import_project_source_issues(
        PROJECT, source, _QueueRecording(store_answer)
    )

    assert outcome == expected
