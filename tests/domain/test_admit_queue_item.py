from __future__ import annotations

from typing import Any

import pytest

from atelier2.application.admit_queue_item import (
    AdmittedQueueItemsListed,
    admit_queue_item,
    list_admitted_queue_items,
)
from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    AdmitQueueItem,
    QueueAdmission,
    QueueAdmissionAlreadyCurrent,
    QueueAdmissionAlreadyDecided,
    QueueAdmissionRationale,
    QueueAdmissionRevisionConflict,
    QueueItemAdmitted,
    QueueItemId,
    QueueItemSnapshot,
    QueueItemState,
    QueueProjectionRevision,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.queue_projection import AdmittedQueueItemsPage, QueueReadUnavailable

_WORKFLOW_LINEAGE_ID = CatalogLineageId(
    "d392f5891160350355c3cb56a70c66afc725c19a5a39c7616198fcd3cf1e731e"
)


class FakeQueueProjection:
    """One port double that answers `admit` and `list_admitted_items` as scripted.

    Recording calls proves each use-case hands its port exactly the caller's
    arguments, rather than inventing or dropping any of them.
    """

    def __init__(self, admit_answer: Any = None, list_answer: Any = None) -> None:
        self.admit_answer = admit_answer
        self.list_answer = list_answer
        self.admit_calls: list[AdmitQueueItem] = []
        self.list_calls: list[tuple[QueueItemId | None, int]] = []

    def admit(self, command: AdmitQueueItem) -> Any:
        self.admit_calls.append(command)
        return self.admit_answer

    def list_admitted_items(self, after: QueueItemId | None, limit: int) -> Any:
        self.list_calls.append((after, limit))
        return self.list_answer


def _reference() -> WorkItemReference:
    return WorkItemReference(ProjectId("project1"), TrackerItemReference("gh:79"))


def _admission() -> QueueAdmission:
    return QueueAdmission(
        _WORKFLOW_LINEAGE_ID, QueueAdmissionRationale("matches the triage rule")
    )


def _command(revision: int = 0) -> AdmitQueueItem:
    return AdmitQueueItem(_reference(), _admission(), QueueProjectionRevision(revision))


ADMIT_OUTCOMES: list[tuple[str, Any]] = [
    (
        "admitted",
        QueueItemAdmitted(_reference(), _admission(), QueueProjectionRevision(1)),
    ),
    (
        "revision-conflict",
        QueueAdmissionRevisionConflict(
            QueueProjectionRevision(3), QueueProjectionRevision(1)
        ),
    ),
    (
        "already-current",
        QueueAdmissionAlreadyCurrent(
            _reference(), _admission(), QueueProjectionRevision(1)
        ),
    ),
    (
        "already-decided-under-a-different-binding",
        QueueAdmissionAlreadyDecided(_reference(), _admission()),
    ),
]


@pytest.mark.parametrize(
    ("name", "outcome"), ADMIT_OUTCOMES, ids=[case[0] for case in ADMIT_OUTCOMES]
)
def test_admit_queue_item_hands_the_ports_own_outcome_on_untouched(
    name: str, outcome: Any
) -> None:
    queue = FakeQueueProjection(admit_answer=outcome)
    command = _command()

    result = admit_queue_item(command, queue)

    assert result == outcome
    assert queue.admit_calls == [command]


def test_admit_queue_item_translates_write_unavailable() -> None:
    queue = FakeQueueProjection(admit_answer=DurableWriteUnavailable())

    result = admit_queue_item(_command(), queue)

    assert result == WriteUnavailable()


def test_admit_queue_item_translates_durable_state_corrupt() -> None:
    queue = FakeQueueProjection(admit_answer=PortDurableStateCorrupt())

    result = admit_queue_item(_command(), queue)

    assert result == DurableStateCorrupt()


def test_list_admitted_queue_items_returns_admitted_items_with_binding_and_rationale() -> (
    None
):
    admitted = QueueItemSnapshot(
        _reference(), QueueItemState.ADMITTED, QueueProjectionRevision(1), _admission()
    )
    queue = FakeQueueProjection(list_answer=AdmittedQueueItemsPage((admitted,), None))

    result = list_admitted_queue_items(None, 50, queue)

    assert result == AdmittedQueueItemsListed((admitted,), None)
    assert isinstance(result, AdmittedQueueItemsListed)
    assert result.items[0].admission == _admission()
    assert queue.list_calls == [(None, 50)]


def test_list_admitted_queue_items_of_an_empty_queue_is_an_empty_answer() -> None:
    queue = FakeQueueProjection(list_answer=AdmittedQueueItemsPage((), None))

    result = list_admitted_queue_items(None, 50, queue)

    assert result == AdmittedQueueItemsListed((), None)


def test_list_admitted_queue_items_translates_read_unavailable() -> None:
    queue = FakeQueueProjection(list_answer=QueueReadUnavailable())

    result = list_admitted_queue_items(None, 50, queue)

    assert result == ReadUnavailable()


def test_list_admitted_queue_items_translates_durable_state_corrupt() -> None:
    queue = FakeQueueProjection(list_answer=PortDurableStateCorrupt())

    result = list_admitted_queue_items(None, 50, queue)

    assert result == DurableStateCorrupt()
