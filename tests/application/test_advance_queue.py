"""advance_queue's own concern: which order admitted items are started in.

`queue_start_order_key` (contracts/queue_projection.py) is the one owner of
this ordering rule; `advance_queue` starts admitted items in that order, and
`GET /queue-items` (the DBOS store, tested at the integration layer) lists
every item in the same order. This file pins the rule and its wiring without a
database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Never, cast

import pytest

import atelier2.application.advance_queue as advance_queue_module
from atelier2.application.advance_queue import (
    QueueItemBlocked,
    QueueRunStarted,
    advance_queue,
)
from atelier2.application.start_published_run import RunCreated
from atelier2.contracts.catalog_v3 import CatalogLineageDisplayName, CatalogLineageId
from atelier2.contracts.host_configuration import ProjectId
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
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import Run, RunId, RunState, WorkflowRevisionHash
from atelier2.ports.durable_runs import DurablePublishedRunStarter
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    CatalogRevisionPosition,
    ResolveCatalogNameResult,
)
from atelier2.ports.queue_projection import QueueItemsPage, QueueLaunchReserved

PROJECT = ProjectId("studio")
LINEAGE = CatalogLineageId("b" * 64)
REVISION_HASH = PublishedRevisionHash("c" * 64)
RATIONALE = QueueAdmissionRationale("operator approved the inspected proposal")


def _admitted(tracker: str, rank: int) -> QueueItemSnapshot:
    reference = WorkItemReference(PROJECT, TrackerItemReference(tracker))
    proposal = QueueProposal(
        QueuePriorityRank(rank),
        LINEAGE,
        (),
        QueueAutomationDisposition.AUTOMATION_AUTHORIZED,
    )
    admission = QueueAdmission(
        LINEAGE, RATIONALE, QueueDecisionAuthority.OPERATOR, QueueProjectionRevision(1)
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

    def reconcile_open_items(
        self, project: object, items: object, observed_at: object
    ) -> Never:
        raise AssertionError("advance_queue never reconciles the open set")


class _FixedCatalogResolver:
    """Every workflow lineage resolves to the same fixture revision at head."""

    def resolve(self, kind: object, revision_hash: object) -> Never:
        raise AssertionError("advance_queue only resolves a lineage by name")

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
        return CatalogNameFound(
            cast(CatalogLineageId, lineage_id_or_name),
            REVISION_HASH,
            1,
            CatalogLineageDisplayName("fixture"),
            False,
        )


def _run_started(
    run_id: RunId,
    workflow_revision_hash: WorkflowRevisionHash,
    _bindings: object,
    _starter: object,
    **_kwargs: object,
) -> RunCreated:
    return RunCreated(
        Run(run_id, workflow_revision_hash, RunState.STARTED, "final", 0, 0)
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


def test_advance_queue_starts_admitted_items_in_the_shared_order_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    low_rank = _admitted("gh:100", rank=1)
    tie_first = _admitted("gh:150", rank=2)
    tie_second = _admitted("gh:200", rank=2)
    unranked = _legacy_admitted("gh:900")
    queue = _QueueRecording(
        QueueItemsPage((unranked, tie_second, low_rank, tie_first), None)
    )
    monkeypatch.setattr(advance_queue_module, "start_published_run", _run_started)

    outcomes = advance_queue(
        queue, _FixedCatalogResolver(), cast(DurablePublishedRunStarter, object())
    )

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
