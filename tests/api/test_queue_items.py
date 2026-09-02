from __future__ import annotations

from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.openapi import QUEUE_ITEMS_PATH
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    QueueItemId,
    QueueItemSnapshot,
    QueueItemState,
    QueueItemTrackerObservation,
    QueueProjectionRevision,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.when import RecordedAt
from atelier2.ports.queue_projection import QueueItemsPage
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

TITLE_OBSERVED_AT = RecordedAt("2026-09-01T14:00:00Z")
RETIRED_AT = RecordedAt("2026-09-02T09:30:00Z")


@dataclass
class QueueReader:
    items: tuple[QueueItemSnapshot, ...]
    calls: list[tuple[QueueItemId | None, int]] = field(default_factory=list)

    def list_items(self, after: QueueItemId | None, limit: int) -> QueueItemsPage:
        self.calls.append((after, limit))
        return QueueItemsPage(self.items, None)


def observed_item(
    reference: str,
    *,
    observation: QueueItemTrackerObservation | None,
    retired_at: RecordedAt | None,
) -> QueueItemSnapshot:
    return QueueItemSnapshot(
        WorkItemReference(ProjectId("atelier"), TrackerItemReference(reference)),
        QueueItemState.OBSERVED,
        QueueProjectionRevision(0),
        None,
        observation=observation,
        retired_at=retired_at,
    )


def test_queue_listing_serves_dated_titles_and_keeps_retired_items_visible() -> None:
    titled_retired = observed_item(
        "gh:450",
        observation=QueueItemTrackerObservation("Preview door", TITLE_OBSERVED_AT),
        retired_at=RETIRED_AT,
    )
    never_observed = observed_item("gh:451", observation=None, retired_at=None)
    queue = QueueReader((titled_retired, never_observed))
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(queue_projection=queue),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )

    response = client.get(QUEUE_ITEMS_PATH)

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "project_id": "atelier",
                "tracker_item_reference": "gh:450",
                "item_id": titled_retired.item_reference.item_id.value,
                "state": "OBSERVED",
                "revision": 0,
                "proposal": None,
                "admission": None,
                "launch_binding": None,
                "blockers": [],
                "tracker_enrichment": "ENRICHMENT_UNAVAILABLE",
                "title": "Preview door",
                "title_observed_at": TITLE_OBSERVED_AT.value,
                "retired_at": RETIRED_AT.value,
            },
            {
                "project_id": "atelier",
                "tracker_item_reference": "gh:451",
                "item_id": never_observed.item_reference.item_id.value,
                "state": "OBSERVED",
                "revision": 0,
                "proposal": None,
                "admission": None,
                "launch_binding": None,
                "blockers": [],
                "tracker_enrichment": "ENRICHMENT_UNAVAILABLE",
                "title": None,
                "title_observed_at": None,
                "retired_at": None,
            },
        ],
        "next_after": None,
    }
    assert queue.calls == [(None, 50)]
