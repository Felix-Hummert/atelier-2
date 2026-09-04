"""Advance manually admitted queue items from durable proposals exactly once."""

from __future__ import annotations

from dataclasses import dataclass, replace

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.application.start_published_run import (
    AgentConfigurationRevisionMissing,
    AgentExecutorBindingUnavailable,
    AuthoredOrder,
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
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.orders import WorkItemOrderValue
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.queue_projection import (
    QueueBlockerKind,
    QueueItemId,
    QueueItemSnapshot,
    QueueItemState,
    QueueLaunchBinding,
    queue_start_order_key,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.work_items import WORK_ITEM_ORDER_SCHEMA_REVISION
from atelier2.contracts.workflow_refusals import WorkflowDocumentInvalid
from atelier2.contracts.workflows_v3 import AnyWorkflowDocument
from atelier2.ports.durable_runs import (
    DurablePublishedRunStarter,
    DurableWriteUnavailable,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.issue_observation import TrackerItemSource
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    CatalogNameMissing,
    CatalogResolver,
    PublishedRevisionFound,
    PublishedRevisionMissing,
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
from atelier2.ports.workflow_revisions import WorkflowDocumentParser

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


@dataclass(frozen=True)
class _RequiredOrderUnavailable:
    """The document declares graph inputs this sweep has no material for."""


def advance_queue(
    queue: QueueProjection,
    catalog: CatalogResolver,
    starter: DurablePublishedRunStarter,
    *,
    workflow_document_parser: WorkflowDocumentParser | None,
    served_project: ProjectId | None = None,
    tracker: TrackerItemSource | None = None,
    page_limit: int = MAXIMUM_PAGE_ITEMS,
) -> tuple[QueueAdvanceOutcome, ...]:
    """Start each exact queue launch once, carrying the item it is about.

    `workflow_document_parser` is what turns a bound revision's published bytes
    into the graph a start can read `graph_inputs` from (ADR 0007's parsing
    stays an adapter concern, so this is handed in rather than imported).
    Passed `None`, a document is started exactly as before, bindings
    unexamined -- a caller says so explicitly rather than falling into it by
    omission; the live sweep always supplies the real parser.

    An admitted item naming a project other than `served_project` is not this
    instance's item -- a foreign `project_id` reaches here through
    `PUT /queue-proposals`, or the served project changes with old rows left
    behind -- so the sweep leaves it untouched (no launch binding, no run, no
    blocker invented) and continues with the next admitted item.
    """
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
    ordered = sorted(admitted_items, key=queue_start_order_key)
    outcomes = (
        _advance_one(
            item,
            queue,
            catalog,
            starter,
            workflow_document_parser,
            served_project,
            tracker,
        )
        for item in ordered
    )
    return tuple(outcome for outcome in outcomes if outcome is not None)


def _advance_one(
    item: QueueItemSnapshot,
    queue: QueueProjection,
    catalog: CatalogResolver,
    starter: DurablePublishedRunStarter,
    workflow_document_parser: WorkflowDocumentParser | None,
    served_project: ProjectId | None,
    tracker: TrackerItemSource | None,
) -> QueueAdvanceOutcome | None:
    if served_project is not None and item.item_reference.project != served_project:
        return None
    binding = item.launch_binding
    binding_preexisted = binding is not None
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
            case QueueLaunchReserved(binding=reserved):
                binding = reserved
            case QueueLaunchAlreadyBound(binding=reserved):
                binding = reserved
                binding_preexisted = True
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
    order = _bound_work_item_order(item, binding, catalog, workflow_document_parser)
    if isinstance(order, _RequiredOrderUnavailable):
        # A binding this sweep just reserved can own no run yet, so a fresh
        # item is blocked without ever asking the starter (pinned by
        # `test_a_document_declaring_more_than_the_sweep_can_fill_is_blocked_not_guessed_at`).
        # A binding that already existed before this call may already have
        # started a run under an earlier, fillable read of the same document
        # -- the durable answer, not a second guess at the order, decides
        # whether that item is blocked or already active.
        if binding_preexisted:
            probe = start_published_run(
                binding.run_id,
                binding.workflow_revision_hash,
                None,
                starter,
                project=served_project,
            )
            if isinstance(probe, RunExisting):
                return QueueRunAlreadyActive(
                    item.item_reference.item_id, binding, probe.run
                )
        return QueueItemBlocked(
            item.item_reference.item_id,
            (QueueBlockerKind.REQUIRED_ORDER_UNAVAILABLE,),
        )
    if order is None:
        result = start_published_run(
            binding.run_id,
            binding.workflow_revision_hash,
            None,
            starter,
            project=served_project,
        )
    else:
        result = start_published_run(
            binding.run_id,
            binding.workflow_revision_hash,
            (),
            starter,
            orders=(order,),
            project=served_project,
            tracker=tracker,
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


def _bound_work_item_order(
    item: QueueItemSnapshot,
    binding: QueueLaunchBinding,
    catalog: CatalogResolver,
    workflow_document_parser: WorkflowDocumentParser | None,
) -> AuthoredOrder | _RequiredOrderUnavailable | None:
    """The one order the bound document's `graph_inputs` asks this sweep to fill.

    `None` for a document with no `graph_inputs` (starts as today) or when no
    parser was handed in. A document that declares anything else -- more than
    one input, or one pinned to a schema other than the work-item order's --
    names material this sweep has no way to supply, so it is unfillable rather
    than guessed at.
    """

    if workflow_document_parser is None:
        return None
    document = _resolve_document(
        binding.workflow_revision_hash, catalog, workflow_document_parser
    )
    graph_inputs = document.graph_inputs
    if not graph_inputs:
        return None
    if len(graph_inputs) != 1:
        return _RequiredOrderUnavailable()
    (graph_input,) = graph_inputs
    if graph_input.schema_reference.revision != WORK_ITEM_ORDER_SCHEMA_REVISION.value:
        return _RequiredOrderUnavailable()
    return AuthoredOrder(
        graph_input.name, WorkItemOrderValue(item.item_reference.tracker_item)
    )


def _resolve_document(
    revision_hash: WorkflowRevisionHash,
    catalog: CatalogResolver,
    parser: WorkflowDocumentParser,
) -> AnyWorkflowDocument:
    match catalog.resolve(
        RevisionKind.WORKFLOW, PublishedRevisionHash(revision_hash.value)
    ):
        case PublishedRevisionFound(revision=revision):
            try:
                return parser(revision.document)
            except WorkflowDocumentInvalid as error:
                raise QueueAdvanceCorrupt(
                    f"workflow revision {revision_hash.value} is bound but not a "
                    "readable document"
                ) from error
        case PublishedRevisionMissing():
            raise QueueAdvanceCorrupt(
                f"workflow revision {revision_hash.value} is bound but unpublished"
            )
        case PublishedRevisionsUnavailable():
            raise QueueAdvanceUnavailable(
                f"the catalog could not resolve workflow revision {revision_hash.value}"
            )
        case PortDurableStateCorrupt():
            raise QueueAdvanceCorrupt(
                f"workflow revision {revision_hash.value} has corrupt catalog state"
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
