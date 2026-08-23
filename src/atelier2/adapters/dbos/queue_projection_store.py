from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.schema import queue_items
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    QUEUE_PROJECTION_REVISION_OBSERVED,
    AdmitQueueItem,
    QueueAdmission,
    QueueAdmissionRationale,
    QueueItemAdmitted,
    QueueItemSnapshot,
    QueueItemState,
    QueueProjectionRevision,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.queue_projection import AdmitQueueItemResult


class DurableQueueAdmissionConflict(RuntimeError):
    """Durable rows do not form the exact admission CAS transition expected."""


def _snapshot_from_record(record: Mapping[Any, Any]) -> QueueItemSnapshot:
    item_reference = WorkItemReference(
        ProjectId(str(record["project_id"])),
        TrackerItemReference(str(record["tracker_item_reference"])),
    )
    if item_reference.item_id.value != record["item_id"]:
        raise ValueError("durable queue item id disagrees with its derived identity")
    lineage_id = record["workflow_lineage_id"]
    admission = (
        None
        if lineage_id is None
        else QueueAdmission(
            CatalogLineageId(str(lineage_id)),
            QueueAdmissionRationale(str(record["admission_rationale"])),
        )
    )
    return QueueItemSnapshot(
        item_reference,
        QueueItemState(str(record["state"])),
        QueueProjectionRevision(int(record["state_version"])),
        admission,
    )


class DbosQueueProjectionStore:
    """One item's admission lifecycle over the V29 `queue_items` table.

    A work item is dedupliated by its derived identity: the first admission
    request this store ever sees for one project and tracker reference also
    establishes that item's row, OBSERVED at revision 0 -- there is no
    separate durable "observed" write in this slice. Every transition after
    that runs `QueueItemSnapshot.admit`'s own CAS-guarded rule.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def admit(self, command: AdmitQueueItem) -> AdmitQueueItemResult:
        item_reference = command.item_reference
        try:
            with canonical_write_transaction(self._engine) as connection:
                connection.execute(
                    sa.insert(queue_items)
                    .prefix_with("OR IGNORE")
                    .values(
                        item_id=item_reference.item_id.value,
                        project_id=item_reference.project.value,
                        tracker_item_reference=item_reference.tracker_item.value,
                        state=QueueItemState.OBSERVED.value,
                        state_version=QUEUE_PROJECTION_REVISION_OBSERVED.value,
                        workflow_lineage_id=None,
                        admission_rationale=None,
                    )
                )
                record = (
                    connection.execute(
                        sa.select(queue_items).where(
                            queue_items.c.item_id == item_reference.item_id.value
                        )
                    )
                    .mappings()
                    .one()
                )
                snapshot = _snapshot_from_record(record)
                outcome = snapshot.admit(command)
                if isinstance(outcome, QueueItemAdmitted):
                    updated = connection.execute(
                        queue_items.update()
                        .where(
                            queue_items.c.item_id == item_reference.item_id.value,
                            queue_items.c.state == QueueItemState.OBSERVED.value,
                            queue_items.c.state_version
                            == command.expected_revision.value,
                        )
                        .values(
                            state=QueueItemState.ADMITTED.value,
                            state_version=outcome.revision.value,
                            workflow_lineage_id=(
                                outcome.admission.workflow_lineage_id.value
                            ),
                            admission_rationale=outcome.admission.rationale.value,
                        )
                    )
                    if updated.rowcount != 1:
                        raise DurableQueueAdmissionConflict(
                            "queue item admission CAS changed no row"
                        )
                return outcome
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
