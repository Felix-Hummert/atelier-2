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
    """A `QueueItemsReader` double that answers one scripted page per call.

    The route trusts the projection for order and cursor continuation (the
    SQL seek itself is proven at the store's own integration tests); this
    double lets a route-level test walk a multi-page answer and check the
    route forwards it without reordering, dropping, or repeating an item.
    """

    pages: tuple[QueueItemsPage, ...]
    calls: list[tuple[QueueItemId | None, int]] = field(default_factory=list)

    def list_items(self, after: QueueItemId | None, limit: int) -> QueueItemsPage:
        self.calls.append((after, limit))
        return self.pages[len(self.calls) - 1]


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
    queue = QueueReader((QueueItemsPage((titled_retired, never_observed), None),))
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


def test_queue_listing_walks_pages_in_the_order_the_projection_serves_them() -> None:
    first = observed_item("gh:10", observation=None, retired_at=None)
    second = observed_item("gh:20", observation=None, retired_at=None)
    third = observed_item("gh:30", observation=None, retired_at=None)
    queue = QueueReader(
        (
            QueueItemsPage((first, second), second.item_reference.item_id),
            QueueItemsPage((third,), None),
        )
    )
    client = TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(queue_projection=queue),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
        )
    )

    first_page = client.get(QUEUE_ITEMS_PATH, params={"limit": "2"})

    assert first_page.status_code == 200
    first_body = first_page.json()
    assert [item["tracker_item_reference"] for item in first_body["items"]] == [
        "gh:10",
        "gh:20",
    ]
    assert first_body["next_after"] == second.item_reference.item_id.value

    second_page = client.get(
        QUEUE_ITEMS_PATH,
        params={"after": first_body["next_after"], "limit": "2"},
    )

    assert second_page.status_code == 200
    second_body = second_page.json()
    assert [item["tracker_item_reference"] for item in second_body["items"]] == [
        "gh:30"
    ]
    assert second_body["next_after"] is None
    assert queue.calls == [(None, 2), (second.item_reference.item_id, 2)]
