"""The auto-start sweep, over faked ports.

The sweep's contract is pure: read the admitted items, resolve each binding's
head, derive a stable run identity, start it once. These fakes model exactly
the collaborators' contracts the sweep depends on -- the queue's id-ordered
paging, the catalog's head lookup, and the starter's create-once / existing /
identity-conflict idempotency -- so the behaviours below are the sweep's own,
not an integration's. The real wiring is proved end to end against a launched
runtime in `tests/integration/test_runtime_queue_auto_start.py`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from atelier2.application.start_admitted_queue_items import (
    QueueAutoStartUnavailable,
    QueueItemAwaitingBinding,
    QueueItemWorkflowUnresolved,
    QueueRunAlreadyActive,
    QueueRunStarted,
    QueueRunStartRefused,
    start_admitted_queue_items,
)
from atelier2.contracts.catalog_v3 import (
    CatalogLineageDisplayName,
    CatalogLineageId,
    CatalogLineageQuery,
)
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.queue_projection import (
    QUEUE_PROJECTION_REVISION_ADMITTED,
    AdmitQueueItem,
    QueueAdmission,
    QueueAdmissionRationale,
    QueueItemId,
    QueueItemSnapshot,
    QueueItemState,
    TrackerItemReference,
    WorkItemReference,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind
from atelier2.contracts.runs import Run, RunState, WorkflowRevisionHash
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurableInvalidAgentBindings,
    DurablePublishedRunResult,
    DurableRunCreated,
    DurableRunExisting,
    DurableRunIdentityConflict,
    DurableRunRevisionMissing,
    DurableStateCorrupt,
)
from atelier2.ports.published_revisions import (
    CatalogNameFound,
    CatalogNameMissing,
    CatalogRevisionPosition,
    ResolveCatalogNameResult,
    ResolvePublishedRevisionResult,
)
from atelier2.ports.queue_projection import (
    AdmitQueueItemResult,
    AdmittedQueueItemsPage,
    ListAdmittedQueueItemsResult,
    ListObservedQueueItemsResult,
    ObserveQueueItemsResult,
    QueueReadUnavailable,
)


def _lineage(seed: str) -> CatalogLineageId:
    return CatalogLineageId.of(f"lineage-{seed}".encode())


def _head(seed: str) -> PublishedRevisionHash:
    return PublishedRevisionHash.of(f"workflow-head-{seed}".encode())


def _admitted(tracker: str, lineage_id: CatalogLineageId) -> QueueItemSnapshot:
    reference = WorkItemReference(ProjectId("project1"), TrackerItemReference(tracker))
    return QueueItemSnapshot(
        reference,
        QueueItemState.ADMITTED,
        QUEUE_PROJECTION_REVISION_ADMITTED,
        QueueAdmission(lineage_id, QueueAdmissionRationale("the triage rule matched")),
    )


class _FakeQueue:
    """The admitted-items read, id-ordered and paged exactly as the store's is."""

    def __init__(
        self,
        snapshots: tuple[QueueItemSnapshot, ...] = (),
        read_failure: ListAdmittedQueueItemsResult | None = None,
    ) -> None:
        self._snapshots = snapshots
        self._read_failure = read_failure

    def list_admitted_items(
        self, after: QueueItemId | None, limit: int
    ) -> ListAdmittedQueueItemsResult:
        if self._read_failure is not None:
            return self._read_failure
        ordered = sorted(
            self._snapshots, key=lambda item: item.item_reference.item_id.value
        )
        start = 0
        if after is not None:
            start = next(
                index + 1
                for index, item in enumerate(ordered)
                if item.item_reference.item_id.value == after.value
            )
        window = ordered[start : start + limit + 1]
        has_more = len(window) > limit
        page = window[:limit]
        next_after = (
            QueueItemId(page[-1].item_reference.item_id.value)
            if has_more and page
            else None
        )
        return AdmittedQueueItemsPage(tuple(page), next_after)

    def admit(self, command: AdmitQueueItem) -> AdmitQueueItemResult:
        raise NotImplementedError("the sweep never admits")

    def observe(self, references: object) -> ObserveQueueItemsResult:
        raise NotImplementedError("the sweep never observes")

    def list_observed_items(
        self, after: QueueItemId | None, limit: int
    ) -> ListObservedQueueItemsResult:
        raise NotImplementedError("the sweep never reads the observed list")


class _FakeCatalog:
    """Head resolution for known lineages; a `Missing` for anything else."""

    def __init__(self, heads: dict[CatalogLineageId, PublishedRevisionHash]) -> None:
        self._heads = heads

    def resolve_name(
        self,
        kind: RevisionKind,
        lineage_id_or_name: CatalogLineageQuery,
        position: CatalogRevisionPosition,
    ) -> ResolveCatalogNameResult:
        assert kind is RevisionKind.WORKFLOW
        assert position == "head"
        assert isinstance(lineage_id_or_name, CatalogLineageId)
        head = self._heads.get(lineage_id_or_name)
        if head is None:
            return CatalogNameMissing(lineage_id_or_name, position)
        return CatalogNameFound(
            lineage_id_or_name,
            head,
            1,
            CatalogLineageDisplayName("triage-workflow"),
            False,
        )

    def resolve_reference(
        self,
        kind: RevisionKind,
        lineage_id: CatalogLineageId,
        revision_hash: PublishedRevisionHash,
    ) -> ResolvePublishedRevisionResult:
        raise NotImplementedError("the sweep resolves head by name")

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        raise NotImplementedError("the sweep resolves head by name")


def _started_run(request: AnyStartPublishedRunRequest) -> Run:
    return Run(request.run_id, request.revision_hash, RunState.STARTED, "start", 0, 0)


@dataclass
class _FakeStarter:
    """Create-once / existing / identity-conflict, the real starter's idempotency.

    ``started`` is this fake's durable table: a restart is a fresh instance
    seeded with the same table, exactly as a reopened process shares the same
    database, so only a stable run derivation re-addresses the run it holds.
    """

    refusals: dict[str, DurablePublishedRunResult] = field(default_factory=dict)
    started: dict[str, str] = field(default_factory=dict)
    requests: list[AnyStartPublishedRunRequest] = field(default_factory=list)

    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult:
        self.requests.append(request)
        refusal = self.refusals.get(request.revision_hash.value)
        if refusal is not None:
            return refusal
        pinned = self.started.get(request.run_id.value)
        if pinned is None:
            self.started[request.run_id.value] = request.revision_hash.value
            return DurableRunCreated(_started_run(request))
        if pinned != request.revision_hash.value:
            return DurableRunIdentityConflict()
        return DurableRunExisting(_started_run(request))


def _one_started(outcomes: tuple[object, ...]) -> Run:
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert isinstance(outcome, QueueRunStarted)
    run = outcome.run
    assert isinstance(run, Run)
    return run


def test_an_admitted_item_starts_its_bound_workflow_exactly_once() -> None:
    lineage, head = _lineage("triage"), _head("triage")
    item = _admitted("gh:79", lineage)
    starter = _FakeStarter()

    outcomes = start_admitted_queue_items(
        _FakeQueue((item,)), _FakeCatalog({lineage: head}), starter
    )

    started = _one_started(outcomes)
    assert isinstance(outcomes[0], QueueRunStarted)
    assert outcomes[0].item_id == item.item_reference.item_id
    assert started.state is RunState.STARTED
    assert started.revision_hash == WorkflowRevisionHash(head.value)
    assert len(starter.requests) == 1


def test_a_second_sweep_finds_the_derived_run_and_does_not_start_it_again() -> None:
    lineage, head = _lineage("triage"), _head("triage")
    item = _admitted("gh:79", lineage)
    queue, catalog = _FakeQueue((item,)), _FakeCatalog({lineage: head})
    starter = _FakeStarter()

    started = _one_started(start_admitted_queue_items(queue, catalog, starter))
    second = start_admitted_queue_items(queue, catalog, starter)

    assert len(second) == 1
    replayed = second[0]
    assert isinstance(replayed, QueueRunAlreadyActive)
    assert replayed.run.run_id == started.run_id
    assert [request.run_id for request in starter.requests] == [
        started.run_id,
        started.run_id,
    ]


def test_the_same_item_derives_the_same_run_id_across_a_restart() -> None:
    lineage, head = _lineage("triage"), _head("triage")
    item = _admitted("gh:79", lineage)
    queue, catalog = _FakeQueue((item,)), _FakeCatalog({lineage: head})

    started = _one_started(start_admitted_queue_items(queue, catalog, _FakeStarter()))
    reopened = _FakeStarter(started={started.run_id.value: head.value})

    after_restart = start_admitted_queue_items(queue, catalog, reopened)

    assert len(after_restart) == 1
    replayed = after_restart[0]
    assert isinstance(replayed, QueueRunAlreadyActive)
    assert replayed.run.run_id == started.run_id
    assert reopened.requests[0].run_id == started.run_id


def test_distinct_items_never_derive_the_same_run() -> None:
    specs = [("gh:79", _lineage("triage")), ("gh:80", _lineage("escalation"))]
    items = tuple(_admitted(tracker, lineage) for tracker, lineage in specs)
    heads = {lineage: _head(tracker) for tracker, lineage in specs}
    starter = _FakeStarter()

    outcomes = start_admitted_queue_items(
        _FakeQueue(items), _FakeCatalog(heads), starter
    )

    assert all(isinstance(outcome, QueueRunStarted) for outcome in outcomes)
    assert len({request.run_id.value for request in starter.requests}) == 2


def test_the_sweep_pages_through_every_admitted_item() -> None:
    specs = [(f"gh:{number}", _lineage(str(number))) for number in range(3)]
    items = tuple(_admitted(tracker, lineage) for tracker, lineage in specs)
    heads = {lineage: _head(tracker) for tracker, lineage in specs}
    starter = _FakeStarter()

    outcomes = start_admitted_queue_items(
        _FakeQueue(items), _FakeCatalog(heads), starter, page_limit=1
    )

    assert len(outcomes) == 3
    assert all(isinstance(outcome, QueueRunStarted) for outcome in outcomes)
    assert len(starter.requests) == 3


def test_an_item_only_observed_is_never_seen_by_the_sweep() -> None:
    # The read answers only ADMITTED items; the sweep's one precondition is
    # admission, so an item still waiting to be admitted is simply not present.
    outcomes = start_admitted_queue_items(
        _FakeQueue(()), _FakeCatalog({}), _FakeStarter()
    )

    assert outcomes == ()


def test_a_workflow_that_needs_agent_bindings_waits_for_the_binding_decision() -> None:
    lineage, head = _lineage("reviewed-change"), _head("reviewed-change")
    item = _admitted("gh:81", lineage)
    starter = _FakeStarter(refusals={head.value: DurableInvalidAgentBindings()})

    outcomes = start_admitted_queue_items(
        _FakeQueue((item,)), _FakeCatalog({lineage: head}), starter
    )

    assert outcomes == (QueueItemAwaitingBinding(item.item_reference.item_id, lineage),)


def test_a_workflow_whose_revision_is_not_published_is_surfaced_not_skipped() -> None:
    lineage, head = _lineage("triage"), _head("triage")
    item = _admitted("gh:79", lineage)
    starter = _FakeStarter(refusals={head.value: DurableRunRevisionMissing()})

    outcomes = start_admitted_queue_items(
        _FakeQueue((item,)), _FakeCatalog({lineage: head}), starter
    )

    assert len(outcomes) == 1
    refused = outcomes[0]
    assert isinstance(refused, QueueRunStartRefused)
    assert refused.item_id == item.item_reference.item_id


def test_a_surfaced_refusal_does_not_stop_a_healthy_item_from_starting() -> None:
    broken, healthy = _lineage("broken"), _lineage("healthy")
    broken_head, healthy_head = _head("broken"), _head("healthy")
    items = (_admitted("gh:1", broken), _admitted("gh:2", healthy))
    heads = {broken: broken_head, healthy: healthy_head}
    starter = _FakeStarter(refusals={broken_head.value: DurableRunRevisionMissing()})

    outcomes = start_admitted_queue_items(
        _FakeQueue(items), _FakeCatalog(heads), starter
    )

    assert {type(outcome) for outcome in outcomes} == {
        QueueRunStartRefused,
        QueueRunStarted,
    }


def test_an_unresolvable_workflow_binding_fails_loud() -> None:
    item = _admitted("gh:79", _lineage("orphan"))

    with pytest.raises(QueueItemWorkflowUnresolved):
        start_admitted_queue_items(
            _FakeQueue((item,)), _FakeCatalog({}), _FakeStarter()
        )


def test_an_unreadable_queue_fails_loud() -> None:
    with pytest.raises(QueueAutoStartUnavailable):
        start_admitted_queue_items(
            _FakeQueue(read_failure=QueueReadUnavailable()),
            _FakeCatalog({}),
            _FakeStarter(),
        )


def test_a_corrupt_queue_read_fails_loud() -> None:
    with pytest.raises(QueueAutoStartUnavailable):
        start_admitted_queue_items(
            _FakeQueue(read_failure=DurableStateCorrupt()),
            _FakeCatalog({}),
            _FakeStarter(),
        )
