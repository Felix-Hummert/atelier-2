"""Advance manually admitted queue items from durable proposals exactly once."""

from __future__ import annotations

from dataclasses import dataclass, replace

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.application.start_published_run import (
    AgentConfigurationRevisionMissing,
    AgentExecutorBindingUnavailable,
    BindingConstraintRefused,
    InvalidAgentBindings,
    RevisionMissing,
    RunCreated,
    RunExisting,
    RunFormatNotExecutable,
    RunIdentityConflict,
    RunInputRefused,
    UncastAgentRoles,
    WorkItemOrderUnreadable,
    start_published_run,
)
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.queue_projection import (
    QueueBlockerKind,
    QueueItemId,
    QueueItemSnapshot,
    QueueItemState,
    QueueLaunchBinding,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    DurableWriteUnavailable,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    CatalogNameMissing,
    CatalogResolver,
    PublishedRevisionsUnavailable,
)
from atelier2.ports.queue_projection import (
    QueueItemsPage,
    QueueLaunchAlreadyBound,
    QueueLaunchBlocked,
    QueueLaunchReserved,
    QueueProjection,
    QueueReadUnavailable,
)

_QUEUE_ITEM_RUN_DOMAIN = "queue-item-run/v2"


class QueueAdvanceUnavailable(RuntimeError):
    """Durable queue or catalog truth could not be read safely."""


class QueueAdvanceCorrupt(RuntimeError):
    """Durable queue, catalog, or run truth contradicted its contract."""


@dataclass(frozen=True)
class QueueRunStarted:
    item_id: QueueItemId
    binding: QueueLaunchBinding
    run: AnyRun


@dataclass(frozen=True)
class QueueRunAlreadyActive:
    item_id: QueueItemId
    binding: QueueLaunchBinding
    run: AnyRun


@dataclass(frozen=True)
class QueueItemBlocked:
    item_id: QueueItemId
    blockers: tuple[QueueBlockerKind, ...]


type QueueAdvanceOutcome = QueueRunStarted | QueueRunAlreadyActive | QueueItemBlocked


def advance_queue(
    queue: QueueProjection,
    catalog: CatalogResolver,
    starter: DurablePublishedRunStarter,
    *,
    page_limit: int = MAXIMUM_PAGE_ITEMS,
) -> tuple[QueueAdvanceOutcome, ...]:
    admitted_items: list[QueueItemSnapshot] = []
    after: QueueItemId | None = None
    while True:
        page = queue.list_items(after, page_limit)
        if isinstance(page, QueueReadUnavailable):
            raise QueueAdvanceUnavailable("the queue could not be read for advance")
        if isinstance(page, PortDurableStateCorrupt):
            raise QueueAdvanceCorrupt("the queue is corrupt and cannot be advanced")
        if not isinstance(page, QueueItemsPage):
            raise QueueAdvanceCorrupt("the queue answered an unknown projection")
        for item in page.items:
            item = _validated_snapshot(item)
            # A retired item has left the pullable set (ADR 0016, 2026-09-01
            # amendment): it stays visible in the projection, but the pull
            # never starts it again.
            if item.retired_at is not None:
                continue
            if item.state is QueueItemState.ADMITTED:
                admitted_items.append(item)
        if page.next_after is None:
            break
        after = page.next_after
    ordered = sorted(
        admitted_items,
        key=lambda item: (
            item.proposal is None,
            item.proposal.priority.rank if item.proposal is not None else 0,
            item.item_reference.item_id.value,
        ),
    )
    return tuple(_advance_one(item, queue, catalog, starter) for item in ordered)


def _advance_one(
    item: QueueItemSnapshot,
    queue: QueueProjection,
    catalog: CatalogResolver,
    starter: DurablePublishedRunStarter,
) -> QueueAdvanceOutcome:
    binding = item.launch_binding
    if binding is None:
        proposal = item.proposal
        admission = item.admission
        if (
            item.state is QueueItemState.ADMITTED
            and proposal is None
            and admission is not None
            and admission.authority is None
            and admission.proposal_revision is None
        ):
            return QueueItemBlocked(
                item.item_reference.item_id,
                (QueueBlockerKind.LEGACY_REVIEW_REQUIRED,),
            )
        if (
            item.state is not QueueItemState.ADMITTED
            or proposal is None
            or admission is None
            or admission.authority is None
            or admission.proposal_revision is None
        ):
            raise QueueAdvanceCorrupt(
                "the queue item does not carry one complete admitted proposal"
            )
        if item.blockers:
            return QueueItemBlocked(item.item_reference.item_id, item.blockers)
        revision_hash = _resolve_head(proposal.workflow_lineage_id, catalog)
        if revision_hash is None:
            return QueueItemBlocked(
                item.item_reference.item_id,
                (QueueBlockerKind.BINDING_UNRESOLVED,),
            )
        proposed_binding = QueueLaunchBinding(
            item.item_reference.item_id,
            admission.proposal_revision,
            _derive_run_id(
                item.item_reference.item_id, admission.proposal_revision.value
            ),
            revision_hash,
        )
        reservation = queue.reserve_launch(proposed_binding)
        match reservation:
            case (
                QueueLaunchReserved(binding=reserved)
                | QueueLaunchAlreadyBound(binding=reserved)
            ):
                binding = reserved
            case QueueLaunchBlocked(item=blocked):
                blocked = _validated_snapshot(blocked)
                return QueueItemBlocked(
                    blocked.item_reference.item_id, blocked.blockers
                )
            case DurableWriteUnavailable():
                raise QueueAdvanceUnavailable("the launch reservation could not commit")
            case PortDurableStateCorrupt():
                raise QueueAdvanceCorrupt("the launch reservation found corrupt state")
            case _:
                raise QueueAdvanceCorrupt(
                    "the queue answered an unknown launch reservation outcome"
                )
    result = start_published_run(
        binding.run_id,
        binding.workflow_revision_hash,
        None,
        starter,
        project=item.item_reference.project,
    )
    match result:
        case RunCreated(run):
            return QueueRunStarted(item.item_reference.item_id, binding, run)
        case RunExisting(run):
            return QueueRunAlreadyActive(item.item_reference.item_id, binding, run)
        case InvalidAgentBindings() | UncastAgentRoles():
            return QueueItemBlocked(
                item.item_reference.item_id,
                (QueueBlockerKind.BINDING_UNRESOLVED,),
            )
        case WorkItemOrderUnreadable() | RunInputRefused():
            return QueueItemBlocked(
                item.item_reference.item_id,
                (QueueBlockerKind.REQUIRED_ORDER_UNAVAILABLE,),
            )
        case (
            RevisionMissing()
            | RunIdentityConflict()
            | RunFormatNotExecutable()
            | BindingConstraintRefused()
            | AgentConfigurationRevisionMissing()
            | AgentExecutorBindingUnavailable()
        ):
            return QueueItemBlocked(
                item.item_reference.item_id,
                (QueueBlockerKind.START_REFUSED,),
            )
        case WriteUnavailable():
            raise QueueAdvanceUnavailable("the reserved queue run could not start")
        case DurableStateCorrupt():
            raise QueueAdvanceCorrupt("the reserved queue run found corrupt state")
        case _:
            raise QueueAdvanceCorrupt("run start answered an unknown outcome")


def _resolve_head(
    lineage_id: CatalogLineageId, catalog: CatalogResolver
) -> WorkflowRevisionHash | None:
    match catalog.resolve_name(RevisionKind.WORKFLOW, lineage_id, "head"):
        case CatalogNameFound(revision_hash=revision_hash):
            return WorkflowRevisionHash(revision_hash.value)
        case CatalogNameMissing():
            return None
        case PublishedRevisionsUnavailable():
            raise QueueAdvanceUnavailable(
                f"the catalog could not resolve workflow lineage {lineage_id.value}"
            )
        case PortDurableStateCorrupt():
            raise QueueAdvanceCorrupt(
                f"workflow lineage {lineage_id.value} has corrupt catalog state"
            )
        case _:
            raise QueueAdvanceCorrupt("the catalog answered an unknown resolve outcome")


def _validated_snapshot(item: QueueItemSnapshot) -> QueueItemSnapshot:
    """Re-run the snapshot's own validation without silently dropping a field.

    `dataclasses.replace` reads every field `QueueItemSnapshot` declares --
    including one a later change adds -- rather than a fixed positional list
    that would carry on quietly forgetting it.
    """

    try:
        return replace(item)
    except (AttributeError, TypeError, ValueError) as error:
        raise QueueAdvanceCorrupt(
            "the queue projection returned an inconsistent item"
        ) from error


def _derive_run_id(item_id: QueueItemId, proposal_revision: int) -> RunId:
    return RunId(
        Sha256Hash.of(
            frame(
                _QUEUE_ITEM_RUN_DOMAIN,
                item_id.value.encode("ascii"),
                str(proposal_revision).encode("ascii"),
            )
        ).value
    )
