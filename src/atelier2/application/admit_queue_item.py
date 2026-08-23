"""Resolve a workflow binding, then admit an observed item under it.

**Why here and not in the port.** The projection owns whether a *transition* is
legal; it does not resolve what "the named workflow" means. That resolution is
the same read the catalog already answers for any named lineage (ADR 0007), so
this layer performs it once and hands the projection an admission that already
names an exact, existing `CatalogLineageId` -- never a query the store would
have to interpret a second way.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.catalog_v3 import CatalogLineageId, CatalogLineageQuery
from atelier2.contracts.queue_projection import (
    AdmitQueueItem,
    QueueAdmission,
    QueueAdmissionOutcome,
    QueueAdmissionRationale,
    QueueProjectionRevision,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.published_revisions import CatalogNameFound, CatalogNameMissing
from atelier2.ports.published_revisions import CatalogResolver as _CatalogResolver
from atelier2.ports.queue_projection import QueueProjection


@dataclass(frozen=True)
class QueueAdmissionWorkflowUnknown:
    """The named workflow has no head lineage this admission could bind to."""

    workflow_query: CatalogLineageQuery


@dataclass(frozen=True)
class QueueAdmissionWorkflowRetired:
    """The named workflow's lineage is retired and admits no new binding."""

    lineage_id: CatalogLineageId


type AdmitQueueItemResult = (
    QueueAdmissionOutcome
    | QueueAdmissionWorkflowUnknown
    | QueueAdmissionWorkflowRetired
    | WriteUnavailable
    | DurableStateCorrupt
)


def admit_queue_item(
    item_reference: WorkItemReference,
    workflow_query: CatalogLineageQuery,
    rationale: QueueAdmissionRationale,
    expected_revision: QueueProjectionRevision,
    catalog: _CatalogResolver,
    projection: QueueProjection,
) -> AdmitQueueItemResult:
    """Bind the item to the named workflow's current head, then admit it."""

    match catalog.resolve_name(RevisionKind.WORKFLOW, workflow_query, "head"):
        case CatalogNameFound(lineage_id=lineage_id, retired=retired):
            if retired:
                return QueueAdmissionWorkflowRetired(lineage_id)
        case CatalogNameMissing():
            return QueueAdmissionWorkflowUnknown(workflow_query)
    admission = QueueAdmission(lineage_id, rationale)
    command = AdmitQueueItem(item_reference, admission, expected_revision)
    return _application_answer(projection.admit(command))


def _application_answer[T](
    answer: T | DurableWriteUnavailable | PortDurableStateCorrupt,
) -> T | WriteUnavailable | DurableStateCorrupt:
    """Say "not now" in this layer's word, so a caller never names the store's."""

    if isinstance(answer, DurableWriteUnavailable):
        return WriteUnavailable()
    if isinstance(answer, PortDurableStateCorrupt):
        return DurableStateCorrupt()
    return answer
