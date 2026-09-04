"""When a start reads the tracker, and when it must not: the order is the contract."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import pytest

from atelier2.application.refusals import ReadUnavailable, WriteUnavailable
from atelier2.application.start_published_run import (
    AgentExecutorBindingUnavailable,
    AuthoredOrder,
    RunCreated,
    RunExisting,
    WorkItemOrderUnreadable,
    start_published_run,
)
from atelier2.contracts.agents import AgentBindingSet
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.orders import ObservedWorkItemOrderValue, WorkItemOrderValue
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.run_bindings import AnyRun, RunV2
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)
from atelier2.ports.durable_runs import (
    AnyStartPublishedRunRequest,
    DurableAgentExecutorWithoutWorkspaceFileTools,
    DurablePublishedRunResult,
    DurableRunCreated,
    DurableRunExisting,
    DurableWorkItemOrderUnread,
    DurableWriteUnavailable,
    StartPublishedRunRequestV3,
)
from atelier2.ports.issue_observation import (
    TrackerItemUnknown,
    TrackerSourceUnavailable,
    WorkItemRevisionObserved,
)
from tests.scenarios.issue_observation import FakeTrackerItemSource

PROJECT = ProjectId("studio")
ITEM = TrackerItemReference("gh:712")
REVISION_HASH = WorkflowRevisionHash("a" * 64)
RUN_ID = RunId("v3/work-item")
BINDINGS: tuple[()] = ()

REVISION = ObservedWorkItemRevision(
    ITEM,
    WorkItemKind.ISSUE,
    b"what the item said",
    WorkItemChangeMarker('W/"5f2a"'),
    RecordedAt("2026-08-26T09:15:00Z"),
)


def a_run() -> AnyRun:
    return RunV2(
        RUN_ID,
        REVISION_HASH,
        AgentBindingSet(()).binding_set_hash,
        (),
        RunState.STARTED,
        "node",
        0,
        0,
        None,
    )


@dataclass
class _ScriptedStarter:
    """A store that answers each ask in turn, remembering what it was handed."""

    answers: list[DurablePublishedRunResult]
    asks: list[AnyStartPublishedRunRequest] = field(default_factory=list)

    def start_published(
        self, request: AnyStartPublishedRunRequest
    ) -> DurablePublishedRunResult:
        self.asks.append(request)
        return self.answers[len(self.asks) - 1]


def start(starter: _ScriptedStarter, tracker: FakeTrackerItemSource) -> object:
    return start_published_run(
        RUN_ID,
        REVISION_HASH,
        BINDINGS,
        starter,
        (AuthoredOrder("order", WorkItemOrderValue(ITEM)),),
        PROJECT,
        tracker,
    )


def test_a_run_that_already_exists_is_answered_without_reading_the_item() -> None:
    """The durable answer comes first, so a retry never re-reads a moving object."""

    tracker = FakeTrackerItemSource(snapshot_answer=WorkItemRevisionObserved(REVISION))
    starter = _ScriptedStarter([DurableRunExisting(a_run())])

    outcome = start(starter, tracker)

    assert isinstance(outcome, RunExisting)
    assert tracker.snapshot_requests == []
    asked = starter.asks[0]
    assert isinstance(asked, StartPublishedRunRequestV3)
    assert asked.orders[0].value == WorkItemOrderValue(ITEM)


def test_an_unreachable_tracker_does_not_spoil_a_retry_of_an_existing_run() -> None:
    tracker = FakeTrackerItemSource(
        snapshot_answer=TrackerSourceUnavailable("GitHub answered 503")
    )
    starter = _ScriptedStarter([DurableRunExisting(a_run())])

    assert isinstance(start(starter, tracker), RunExisting)
    assert tracker.snapshot_requests == []


def test_a_start_with_nothing_to_answer_from_reads_the_item_exactly_once() -> None:
    tracker = FakeTrackerItemSource(snapshot_answer=WorkItemRevisionObserved(REVISION))
    starter = _ScriptedStarter(
        [DurableWorkItemOrderUnread(), DurableRunCreated(a_run())]
    )

    outcome = start(starter, tracker)

    assert isinstance(outcome, RunCreated)
    assert len(tracker.snapshot_requests) == 1
    second = starter.asks[1]
    assert isinstance(second, StartPublishedRunRequestV3)
    assert second.orders[0].value == ObservedWorkItemOrderValue(REVISION)


def test_an_item_the_start_cannot_read_refuses_before_the_store_is_asked_again() -> (
    None
):
    tracker = FakeTrackerItemSource(snapshot_answer=TrackerItemUnknown(ITEM))
    starter = _ScriptedStarter([DurableWorkItemOrderUnread()])

    outcome = start(starter, tracker)

    assert isinstance(outcome, WorkItemOrderUnreadable)
    assert len(starter.asks) == 1


def test_a_read_whose_write_failed_is_read_again_by_the_next_start() -> None:
    """The read commits nothing: a failed write leaves the next start free to read."""

    tracker = FakeTrackerItemSource(snapshot_answer=WorkItemRevisionObserved(REVISION))
    failing = _ScriptedStarter(
        [DurableWorkItemOrderUnread(), DurableWriteUnavailable()]
    )

    assert isinstance(start(failing, tracker), WriteUnavailable)
    assert len(tracker.snapshot_requests) == 1

    retried = _ScriptedStarter(
        [DurableWorkItemOrderUnread(), DurableRunCreated(a_run())]
    )

    assert isinstance(start(retried, tracker), RunCreated)
    assert len(tracker.snapshot_requests) == 2


def test_an_unavailable_tracker_on_a_first_start_is_named_not_swallowed() -> None:
    tracker = FakeTrackerItemSource(
        snapshot_answer=TrackerSourceUnavailable("GitHub answered 503")
    )
    starter = _ScriptedStarter([DurableWorkItemOrderUnread()])

    outcome = start(starter, tracker)

    assert isinstance(outcome, WorkItemOrderUnreadable)
    assert isinstance(outcome.reason, ReadUnavailable)


def test_a_role_cast_onto_an_executor_that_touches_no_file_is_named_in_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The caller hears one word; the operator hears which cast has to change.

    Three different refusals share the answer `agent-executor-binding-
    unavailable`, so the role and the executor revision the store named would
    otherwise be dropped at this seam -- and an operator would be left guessing
    which of a run's bindings is the wrong one (#1166).
    """

    tracker = FakeTrackerItemSource(snapshot_answer=WorkItemRevisionObserved(REVISION))
    starter = _ScriptedStarter(
        [
            DurableAgentExecutorWithoutWorkspaceFileTools(
                "builder", "claude-subscription/v1"
            )
        ]
    )

    with caplog.at_level(logging.WARNING, logger="atelier2"):
        outcome = start(starter, tracker)

    assert isinstance(outcome, AgentExecutorBindingUnavailable)
    named = [
        record
        for record in caplog.records
        if getattr(record, "event", None)
        == "agent_executor_without_workspace_file_tools"
    ]
    assert [
        (
            record.levelno,
            getattr(record, "run_id", None),
            getattr(record, "role", None),
            getattr(record, "executor_revision", None),
        )
        for record in named
    ] == [(logging.WARNING, RUN_ID.value, "builder", "claude-subscription/v1")]
