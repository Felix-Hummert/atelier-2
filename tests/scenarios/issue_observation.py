"""Scenario arrangement for the tracker observation port."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.ports.issue_observation import (
    ObserveOpenTrackerItemsResult,
    ObserveWorkItemRevisionResult,
)


@dataclass
class FakeTrackerItemSource:
    """A tracker that returns the scenario answers its caller configured."""

    open_items_answer: ObserveOpenTrackerItemsResult | None = None
    snapshot_answer: ObserveWorkItemRevisionResult | None = None
    expected_snapshot_reference: TrackerItemReference | None = None
    unexpected_snapshot_answer: (
        Callable[[TrackerItemReference], ObserveWorkItemRevisionResult] | None
    ) = None
    snapshot_requests: list[TrackerItemReference] = field(default_factory=list)

    def open_items(self) -> ObserveOpenTrackerItemsResult:
        if self.open_items_answer is None:
            raise AssertionError("this scenario did not arrange an open-items answer")
        return self.open_items_answer

    def snapshot(
        self, reference: TrackerItemReference
    ) -> ObserveWorkItemRevisionResult:
        self.snapshot_requests.append(reference)
        if (
            self.expected_snapshot_reference is not None
            and reference != self.expected_snapshot_reference
        ):
            if self.unexpected_snapshot_answer is not None:
                return self.unexpected_snapshot_answer(reference)
            raise AssertionError(
                f"expected {self.expected_snapshot_reference!r}, received {reference!r}"
            )
        if self.snapshot_answer is None:
            raise AssertionError("this scenario did not arrange a snapshot answer")
        return self.snapshot_answer
