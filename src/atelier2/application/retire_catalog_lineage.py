"""Take one catalog lineage off the shelf without touching its history."""

from __future__ import annotations

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.catalog_v3 import (
    CatalogActivatedAt,
    CatalogActor,
    CatalogLineageId,
    CatalogRetirementState,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import DurableWriteUnavailable
from atelier2.ports.published_revisions import (
    CatalogAdmissions,
    RetireCatalogLineageResult,
)

type RetireCatalogLineageUseCaseResult = (
    RetireCatalogLineageResult | WriteUnavailable | DurableStateCorrupt
)


def retire_catalog_lineage(
    lineage_id: CatalogLineageId,
    actor: CatalogActor,
    activated_at: CatalogActivatedAt,
    admissions: CatalogAdmissions,
) -> RetireCatalogLineageUseCaseResult:
    """Append retirement for this lineage; its members and prior runs remain."""

    answer = admissions.retire_lineage(
        lineage_id, CatalogRetirementState.RETIRED, actor, activated_at
    )
    if isinstance(answer, DurableWriteUnavailable):
        return WriteUnavailable()
    if isinstance(answer, PortDurableStateCorrupt):
        return DurableStateCorrupt()
    return answer
