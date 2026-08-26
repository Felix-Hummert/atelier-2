"""Start every admitted queue item's bound workflow, once, from durable state.

Phase A gave the queue its admission and its read; Phase B gave it HTTP doors.
This is the bridge that makes the queue run itself: a launch-time sweep walks
the admitted items and starts each one's bound workflow, so an admission
becomes a live run without a further human hand. It mirrors
`converge_uncontinuable_runs` -- a sweep that reads durable state and acts
idempotently -- but here idempotency is a derivation, not a durable marker.

**Option A (Desk 24.08.).** The run's identity is a pure function of the item
and the exact head revision its binding resolves to, so a second sweep or a
restart re-derives the same `RunId` and the starter answers `RunExisting`
rather than starting a second run. There is no STARTED column and no run_id
column on the item: the item plus its resolved head *is* the run binding.
Because the derived id embeds the very revision hash the start pins, a
re-derivation always agrees with the run already stored, so the start reports
`RunExisting`, never `RunIdentityConflict`, from the sweep's own derivation. A
head advance is a different head, hence a different identity and a new run --
whether an item should re-run under a moved head is a Phase D readiness
question, named here, not decided here.

**What this slice does not do.** It names no agent binding of its own. The start
path fills every role the document declares from the served project's occupancy
(`#680`), so an item whose roles the project has cast starts here, and one with
a role nobody occupied is named `QueueItemAwaitingBinding` and waits.
Readiness, priority, and the human-vs-auto filter are Phase D. The
one precondition this slice enforces is admission itself -- an item still only
OBSERVED is never read here. A start the workflow itself refuses (its bytes are
not a runnable revision, its head moved under a competing run) is surfaced as
`QueueRunStartRefused` rather than silently skipped or allowed to block the
rest of the sweep; wiring those surfaced refusals to an operator view is Phase
D. Only a durable lie the catalog's foreign key should make impossible -- an
admitted item whose lineage does not resolve -- or a queue the store cannot
read at all fails loud, because the sweep must not guess past corruption.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.application.start_published_run import (
    AgentConfigurationRevisionMissing,
    AgentExecutorBindingUnavailable,
    AgentPlatformEffectUnreconcilable,
    BindingConstraintRefused,
    InvalidAgentBindings,
    RevisionMissing,
    RunCreated,
    RunExisting,
    RunFormatNotExecutable,
    RunIdentityConflict,
    RunInputRefused,
    WorkItemOrderUnreadable,
    start_published_run,
)
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.hashing import Sha256Hash, frame
from atelier2.contracts.pages import MAXIMUM_PAGE_ITEMS
from atelier2.contracts.queue_projection import QueueItemId, QueueItemSnapshot
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.durable_runs import DurablePublishedRunStarter
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    CatalogNameMissing,
    CatalogResolver,
    PublishedRevisionsUnavailable,
)
from atelier2.ports.queue_projection import AdmittedQueueItemsPage, QueueProjection

_QUEUE_ITEM_RUN_DOMAIN = "queue-item-run/v1"


class QueueAutoStartUnavailable(RuntimeError):
    """The admitted queue could not be read, so the sweep cannot enumerate it."""


class QueueItemWorkflowUnresolved(RuntimeError):
    """An admitted item's bound workflow lineage does not resolve at head.

    The catalog's foreign key on the admission should make this impossible; if
    it happens the durable state lies, and the sweep refuses to guess a
    workflow rather than skip the item.
    """


@dataclass(frozen=True)
class QueueRunStarted:
    """The item's bound workflow was started here, under its derived identity."""

    item_id: QueueItemId
    run: AnyRun


@dataclass(frozen=True)
class QueueRunAlreadyActive:
    """The derived run already existed: a prior sweep or start owns it."""

    item_id: QueueItemId
    run: AnyRun


@dataclass(frozen=True)
class QueueItemAwaitingBinding:
    """The bound workflow declares a role neither the sweep nor the project fills.

    The start path already reads the served project's occupancy, so this is what
    is left: a role the operator has not cast (or a deployment serving no
    project at all). It waits, named rather than started and never treated as an
    error.
    """

    item_id: QueueItemId
    workflow_lineage_id: CatalogLineageId


type StartRefusalReason = (
    RevisionMissing
    | RunIdentityConflict
    | RunFormatNotExecutable
    | RunInputRefused
    | WorkItemOrderUnreadable
    | BindingConstraintRefused
    | AgentConfigurationRevisionMissing
    | AgentExecutorBindingUnavailable
    | AgentPlatformEffectUnreconcilable
    | WriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class QueueRunStartRefused:
    """The bound workflow could not start; surfaced, never silently skipped."""

    item_id: QueueItemId
    reason: StartRefusalReason


type QueueItemAutoStartOutcome = (
    QueueRunStarted
    | QueueRunAlreadyActive
    | QueueItemAwaitingBinding
    | QueueRunStartRefused
)


def start_admitted_queue_items(
    queue: QueueProjection,
    catalog: CatalogResolver,
    starter: DurablePublishedRunStarter,
    *,
    page_limit: int = MAXIMUM_PAGE_ITEMS,
) -> tuple[QueueItemAutoStartOutcome, ...]:
    """Walk every admitted item, start its bound workflow, name each outcome."""

    outcomes: list[QueueItemAutoStartOutcome] = []
    after: QueueItemId | None = None
    while True:
        page = queue.list_admitted_items(after, page_limit)
        if not isinstance(page, AdmittedQueueItemsPage):
            raise QueueAutoStartUnavailable(
                "the admitted queue could not be read for auto-start"
            )
        for item in page.items:
            outcomes.append(_start_one(item, catalog, starter))
        if page.next_after is None:
            return tuple(outcomes)
        after = page.next_after


def _start_one(
    item: QueueItemSnapshot,
    catalog: CatalogResolver,
    starter: DurablePublishedRunStarter,
) -> QueueItemAutoStartOutcome:
    admission = item.admission
    if admission is None:
        raise QueueItemWorkflowUnresolved(
            "an admitted queue item carried no workflow binding"
        )
    lineage_id = admission.workflow_lineage_id
    revision_hash = _resolve_head(lineage_id, catalog)
    run_id = _derive_run_id(item.item_reference.item_id, lineage_id, revision_hash)
    result = start_published_run(run_id, revision_hash, None, starter)
    match result:
        case RunCreated(run):
            return QueueRunStarted(item.item_reference.item_id, run)
        case RunExisting(run):
            return QueueRunAlreadyActive(item.item_reference.item_id, run)
        case InvalidAgentBindings():
            return QueueItemAwaitingBinding(item.item_reference.item_id, lineage_id)
        case (
            RevisionMissing()
            | RunIdentityConflict()
            | RunFormatNotExecutable()
            | RunInputRefused()
            | WorkItemOrderUnreadable()
            | BindingConstraintRefused()
            | AgentConfigurationRevisionMissing()
            | AgentExecutorBindingUnavailable()
            | AgentPlatformEffectUnreconcilable()
            | WriteUnavailable()
            | DurableStateCorrupt()
        ) as refusal:
            return QueueRunStartRefused(item.item_reference.item_id, refusal)
        case _ as unreachable:
            assert_never(unreachable)


def _resolve_head(
    lineage_id: CatalogLineageId, catalog: CatalogResolver
) -> WorkflowRevisionHash:
    match catalog.resolve_name(RevisionKind.WORKFLOW, lineage_id, "head"):
        case CatalogNameFound(revision_hash=revision_hash):
            return WorkflowRevisionHash(revision_hash.value)
        case CatalogNameMissing():
            raise QueueItemWorkflowUnresolved(
                f"admitted workflow lineage {lineage_id.value} does not resolve at head"
            )
        case PublishedRevisionsUnavailable() | PortDurableStateCorrupt():
            raise QueueAutoStartUnavailable(
                f"the catalog could not resolve workflow lineage {lineage_id.value}"
                " for auto-start"
            )
        case _ as unreachable:
            assert_never(unreachable)


def _derive_run_id(
    item_id: QueueItemId,
    lineage_id: CatalogLineageId,
    revision_hash: WorkflowRevisionHash,
) -> RunId:
    """Derive the run's stable identity from the item, its binding, and its head.

    Distinct items never collide (the item id is in the preimage); the same item
    at the same head always derives the same id (every field is durable and
    stable); and the id embeds the exact revision it starts, so a re-derivation
    agrees with the run already stored.
    """

    return RunId(
        Sha256Hash.of(
            frame(
                _QUEUE_ITEM_RUN_DOMAIN,
                item_id.value.encode("ascii"),
                lineage_id.value.encode("ascii"),
                revision_hash.value.encode("ascii"),
            )
        ).value
    )
