"""advance_queue's own concern: order, and what a started run carries.

`queue_start_order_key` (contracts/queue_projection.py) is the one owner of
this ordering rule; `advance_queue` starts admitted items in that order, and
`GET /queue-items` (the DBOS store, tested at the integration layer) lists
every item in the same order. This file pins the rule and the wiring that
turns a bound document's `graph_inputs` into the order a start carries --
without a database, and through the real `start_published_run` a fake
`DurablePublishedRunStarter` answers, never by replacing that production
function itself.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Never, cast

import pytest

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.advance_queue import (
    QueueItemBlocked,
    QueueRunStarted,
    advance_queue,
)
from atelier2.contracts.catalog_v3 import CatalogLineageDisplayName, CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.orders import ObservedWorkItemOrderValue, WorkItemOrderValue
from atelier2.contracts.queue_projection import (
    QueueAdmission,
    QueueAdmissionRationale,
    QueueAutomationDisposition,
    QueueBlockerKind,
    QueueDecisionAuthority,
    QueueItemId,
    QueueItemSnapshot,
    QueueItemState,
    QueueLaunchBinding,
    QueuePriorityRank,
    QueueProjectionRevision,
    QueueProposal,
    TrackerItemReference,
    WorkItemReference,
    queue_start_order_key,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.runs import Run, RunState
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    WORK_ITEM_ORDER_SCHEMA_REVISION,
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurablePublishedRunResult,
    DurableRunCreated,
    DurableWorkItemOrderUnread,
    StartPublishedRunRequest,
    StartPublishedRunRequestV3,
)
from atelier2.ports.issue_observation import WorkItemRevisionObserved
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    CatalogRevisionPosition,
    PublishedRevisionFound,
    ResolveCatalogNameResult,
)
from atelier2.ports.queue_projection import QueueItemsPage, QueueLaunchReserved
from tests.scenarios.issue_observation import FakeTrackerItemSource
from tests.scenarios.workflows import (
    ANY_JSON_SCHEMA,
    V3_WAIT_LINE_DOCUMENT,
    graph_input_wait_line,
)

PROJECT = ProjectId("studio")
LINEAGE = CatalogLineageId("b" * 64)
REVISION_HASH = PublishedRevisionHash("c" * 64)
OTHER_LINEAGE = CatalogLineageId("d" * 64)
OTHER_REVISION_HASH = PublishedRevisionHash("e" * 64)
RATIONALE = QueueAdmissionRationale("operator approved the inspected proposal")

WORK_ITEM_WORKFLOW_DOCUMENT = graph_input_wait_line(
    WORK_ITEM_ORDER_SCHEMA_REVISION.value
)
UNFILLABLE_WORKFLOW_DOCUMENT = graph_input_wait_line(
    ANY_JSON_SCHEMA.revision_hash.value
)


def _admitted(
    tracker: str, rank: int, lineage: CatalogLineageId = LINEAGE
) -> QueueItemSnapshot:
    reference = WorkItemReference(PROJECT, TrackerItemReference(tracker))
    proposal = QueueProposal(
        QueuePriorityRank(rank),
        lineage,
        (),
        QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
    )
    admission = QueueAdmission(
        lineage, RATIONALE, QueueDecisionAuthority.OPERATOR, QueueProjectionRevision(1)
    )
    return QueueItemSnapshot(
        reference,
        QueueItemState.ADMITTED,
        QueueProjectionRevision(2),
        admission,
        proposal,
    )


def _legacy_admitted(tracker: str) -> QueueItemSnapshot:
    """An item admitted before proposals existed: no proposal, no rank."""

    reference = WorkItemReference(PROJECT, TrackerItemReference(tracker))
    admission = QueueAdmission(LINEAGE, RATIONALE)
    return QueueItemSnapshot(
        reference, QueueItemState.ADMITTED, QueueProjectionRevision(1), admission
    )


@dataclass
class _QueueRecording:
    """The queue projection reduced to what `advance_queue` may do with it."""

    page: QueueItemsPage
    reserved: list[QueueLaunchBinding] = field(default_factory=list)

    def list_items(self, after: QueueItemId | None, limit: int) -> QueueItemsPage:
        assert after is None, "this fixture serves exactly one page"
        return self.page

    def reserve_launch(self, binding: QueueLaunchBinding) -> QueueLaunchReserved:
        self.reserved.append(binding)
        return QueueLaunchReserved(binding)

    def plan(self, command: object) -> Never:
        raise AssertionError("advance_queue never plans a proposal")

    def confirm(self, command: object) -> Never:
        raise AssertionError("advance_queue never confirms an admission")

    def put_policy(self, policy: object, expected_revision: object) -> Never:
        raise AssertionError("advance_queue never publishes a policy")

    def current_policy(self, project: object) -> Never:
        raise AssertionError("advance_queue never reads the policy")

    def reconcile_open_items(
        self, project: object, items: object, observed_at: object
    ) -> Never:
        raise AssertionError("advance_queue never reconciles the open set")


@dataclass
class _CatalogResolverStub:
    """Every lineage resolves by name to its configured head; `resolve` reads
    the document that head is bound to.

    A scenario that never touches a document (`workflow_document_parser` left
    unwired) never has to populate `documents`: `resolve` is only ever called
    once a bound revision hash needs its `graph_inputs` read.
    """

    heads: dict[CatalogLineageId, PublishedRevisionHash]
    documents: dict[PublishedRevisionHash, bytes] = field(default_factory=dict)

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> PublishedRevisionFound:
        assert kind is RevisionKind.WORKFLOW
        document = self.documents.get(revision_hash)
        if document is None:
            raise AssertionError(
                f"this scenario bound no document to {revision_hash!r}"
            )
        return PublishedRevisionFound(
            PublishedRevision(RevisionKind.WORKFLOW, document)
        )

    def resolve_reference(
        self, kind: object, lineage_id: object, revision_hash: object
    ) -> Never:
        raise AssertionError("advance_queue only resolves a lineage by name")

    def resolve_name(
        self,
        kind: RevisionKind,
        lineage_id_or_name: object,
        position: CatalogRevisionPosition,
    ) -> ResolveCatalogNameResult:
        assert kind is RevisionKind.WORKFLOW
        assert position == "head"
        lineage_id = cast(CatalogLineageId, lineage_id_or_name)
        return CatalogNameFound(
            lineage_id,
            self.heads[lineage_id],
            1,
            CatalogLineageDisplayName("fixture"),
            False,
        )


ScriptedAnswer = (
    DurablePublishedRunResult
    | Callable[[AnyStartPublishedRunRequest], DurablePublishedRunResult]
)


@dataclass
class _ScriptedStarter:
    """A store that answers each ask in turn, remembering what it was handed.

    A scripted answer may be a callable of the request itself, so a "created"
    answer can echo the request's own run id and revision hash back rather
    than a test re-deriving `advance_queue`'s own `RunId` derivation to
    predict them.
    """

    answers: list[ScriptedAnswer]
    asks: list[AnyStartPublishedRunRequest] = field(default_factory=list)

    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult:
        self.asks.append(request)
        answer = self.answers[len(self.asks) - 1]
        return answer(request) if callable(answer) else answer


def _created(request: AnyStartPublishedRunRequest) -> DurablePublishedRunResult:
    return DurableRunCreated(
        Run(request.run_id, request.revision_hash, RunState.STARTED, "final", 0, 0)
    )


def test_queue_start_order_key_ranks_proposals_first_then_by_rank_then_by_item_id() -> (
    None
):
    low_rank = _admitted("gh:100", rank=1)
    tie_first = _admitted("gh:150", rank=2)
    tie_second = _admitted("gh:200", rank=2)
    unranked = _legacy_admitted("gh:900")
    assert (
        tie_first.item_reference.item_id.value < tie_second.item_reference.item_id.value
    )

    ordered = sorted(
        [unranked, tie_second, low_rank, tie_first], key=queue_start_order_key
    )

    assert ordered == [low_rank, tie_first, tie_second, unranked]


def test_advance_queue_starts_admitted_items_in_the_shared_order_key() -> None:
    low_rank = _admitted("gh:100", rank=1)
    tie_first = _admitted("gh:150", rank=2)
    tie_second = _admitted("gh:200", rank=2)
    unranked = _legacy_admitted("gh:900")
    queue = _QueueRecording(
        QueueItemsPage((unranked, tie_second, low_rank, tie_first), None)
    )
    catalog = _CatalogResolverStub({LINEAGE: REVISION_HASH})
    starter = _ScriptedStarter([_created, _created, _created])

    outcomes = advance_queue(queue, catalog, starter, workflow_document_parser=None)

    assert [outcome.item_id for outcome in outcomes] == [
        low_rank.item_reference.item_id,
        tie_first.item_reference.item_id,
        tie_second.item_reference.item_id,
        unranked.item_reference.item_id,
    ]
    assert all(isinstance(outcome, QueueRunStarted) for outcome in outcomes[:3])
    (legacy_outcome,) = outcomes[3:]
    assert isinstance(legacy_outcome, QueueItemBlocked)
    assert legacy_outcome.blockers == (QueueBlockerKind.LEGACY_REVIEW_REQUIRED,)


def test_a_document_with_no_graph_inputs_starts_exactly_as_before() -> None:
    """No `graph_inputs` means no order to fill: `bindings` stays `None`."""

    item = _admitted("gh:301", rank=1)
    queue = _QueueRecording(QueueItemsPage((item,), None))
    catalog = _CatalogResolverStub(
        {LINEAGE: REVISION_HASH}, {REVISION_HASH: V3_WAIT_LINE_DOCUMENT}
    )
    starter = _ScriptedStarter([_created])

    (outcome,) = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=parse_workflow_document,
        served_project=PROJECT,
    )

    assert isinstance(outcome, QueueRunStarted)
    (asked,) = starter.asks
    assert isinstance(asked, StartPublishedRunRequest)


@pytest.mark.proves("a-manually-approved-queue-item-starts-once")
def test_a_bound_graph_input_workflow_starts_carrying_the_items_tracker_reference() -> (
    None
):
    """The order names the item; `bindings` becomes `()`, never `None`.

    This also pins the `DurableRunIdentityConflict` argument the plan review
    named: the derived `RunId` is fresh here (this item was never bound
    before), and the old code path could never have written a row under it --
    a graph-input workflow always refused before any insert. A first start of
    a fresh item must therefore create, never conflict -- one exact run, as
    #79's ruled line 6 requires whether or not the bound workflow needs order
    material.
    """

    reference = TrackerItemReference("gh:501")
    revision = ObservedWorkItemRevision(
        reference,
        WorkItemKind.ISSUE,
        b"what the item said",
        WorkItemChangeMarker('W/"1"'),
        RecordedAt("2026-09-04T09:00:00Z"),
    )
    item = _admitted(reference.value, rank=1)
    queue = _QueueRecording(QueueItemsPage((item,), None))
    catalog = _CatalogResolverStub(
        {LINEAGE: REVISION_HASH}, {REVISION_HASH: WORK_ITEM_WORKFLOW_DOCUMENT}
    )
    tracker = FakeTrackerItemSource(snapshot_answer=WorkItemRevisionObserved(revision))
    starter = _ScriptedStarter([DurableWorkItemOrderUnread(), _created])

    (outcome,) = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=parse_workflow_document,
        served_project=PROJECT,
        tracker=tracker,
    )

    assert isinstance(outcome, QueueRunStarted)
    first_ask, second_ask = starter.asks
    assert isinstance(first_ask, StartPublishedRunRequestV3)
    assert first_ask.agent_bindings.bindings == ()
    assert first_ask.orders[0].value == WorkItemOrderValue(reference)
    assert isinstance(second_ask, StartPublishedRunRequestV3)
    assert second_ask.orders[0].value == ObservedWorkItemOrderValue(revision)


def test_a_document_declaring_more_than_the_sweep_can_fill_is_blocked_not_guessed_at() -> (
    None
):
    item = _admitted("gh:601", rank=1)
    queue = _QueueRecording(QueueItemsPage((item,), None))
    catalog = _CatalogResolverStub(
        {LINEAGE: REVISION_HASH}, {REVISION_HASH: UNFILLABLE_WORKFLOW_DOCUMENT}
    )
    starter = _ScriptedStarter([])

    (outcome,) = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=parse_workflow_document,
        served_project=PROJECT,
    )

    assert isinstance(outcome, QueueItemBlocked)
    assert outcome.blockers == (QueueBlockerKind.REQUIRED_ORDER_UNAVAILABLE,)
    assert starter.asks == []


@pytest.mark.proves("a-refused-queue-start-stays-at-its-item-while-the-sweep-continues")
def test_a_disconnected_tracker_blocks_only_the_item_that_needs_it() -> None:
    needs_tracker = _admitted("gh:701", rank=1)
    plain = _admitted("gh:702", rank=2, lineage=OTHER_LINEAGE)
    queue = _QueueRecording(QueueItemsPage((needs_tracker, plain), None))
    catalog = _CatalogResolverStub(
        {LINEAGE: REVISION_HASH, OTHER_LINEAGE: OTHER_REVISION_HASH},
        {
            REVISION_HASH: WORK_ITEM_WORKFLOW_DOCUMENT,
            OTHER_REVISION_HASH: V3_WAIT_LINE_DOCUMENT,
        },
    )
    starter = _ScriptedStarter([DurableWorkItemOrderUnread(), _created])

    blocked_outcome, started_outcome = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=parse_workflow_document,
        served_project=PROJECT,
    )

    assert isinstance(blocked_outcome, QueueItemBlocked)
    assert blocked_outcome.blockers == (QueueBlockerKind.REQUIRED_ORDER_UNAVAILABLE,)
    assert isinstance(started_outcome, QueueRunStarted)


def test_a_foreign_project_item_is_skipped_while_a_served_item_still_starts() -> None:
    """A foreign `project_id` reaches an admitted row through `PUT
    /queue-proposals`, or the served project changes with old rows left behind
    (review finding 1 on `#1145`): neither is this instance's item, so the
    sweep leaves it exactly as it found it -- no launch binding, no run, no
    blocker invented -- and keeps going with the next admitted item.
    """

    other_project = ProjectId("elsewhere")
    foreign_reference = WorkItemReference(other_project, TrackerItemReference("gh:801"))
    foreign_item = QueueItemSnapshot(
        foreign_reference,
        QueueItemState.ADMITTED,
        QueueProjectionRevision(2),
        QueueAdmission(
            LINEAGE,
            RATIONALE,
            QueueDecisionAuthority.OPERATOR,
            QueueProjectionRevision(1),
        ),
        QueueProposal(
            QueuePriorityRank(1),
            LINEAGE,
            (),
            QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
        ),
    )
    served_item = _admitted("gh:802", rank=2)
    queue = _QueueRecording(QueueItemsPage((foreign_item, served_item), None))
    catalog = _CatalogResolverStub({LINEAGE: REVISION_HASH})
    starter = _ScriptedStarter([_created])

    (outcome,) = advance_queue(
        queue,
        catalog,
        starter,
        workflow_document_parser=None,
        served_project=PROJECT,
    )

    assert isinstance(outcome, QueueRunStarted)
    assert outcome.item_id == served_item.item_reference.item_id
    assert [binding.item_id for binding in queue.reserved] == [
        served_item.item_reference.item_id
    ]
