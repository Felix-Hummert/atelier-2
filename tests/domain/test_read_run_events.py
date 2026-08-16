from __future__ import annotations

from typing import Any

import pytest

from atelier2.application.read_run_events import (
    ReadRunEventsResult,
    RunEventPageOversized,
    RunEventsRead,
    read_run_events,
)
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.runs import RunId
from atelier2.ports.run_events import (
    EventHistoryCorrupt,
    PersistedRunEvent,
    RunEventPage,
)
from atelier2.ports.workflow_revisions import QueryDurableStateCorrupt
from atelier2.ports.workflow_revisions import (
    ReadUnavailable as PortReadUnavailable,
)

RUN_ID = RunId("run")
EVENT: Any = object()


class ScriptedEvents:
    """A store that answers one page read with the one answer a case scripts."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.asked: list[tuple[Any, ...]] = []

    def read_run_event_page(
        self,
        run_id: Any,
        after_sequence: int,
        limit: int,
    ) -> Any:
        self.asked.append((run_id, after_sequence, limit))
        return self.answer

    def prepare_run_event_stream(self, run_id: Any, after_sequence: int) -> Any:
        raise AssertionError("a page read under test prepared a stream")


def read(answer: Any, page_size: int = 2) -> tuple[ReadRunEventsResult, ScriptedEvents]:
    queries = ScriptedEvents(answer)
    return (
        read_run_events(RUN_ID, 7, page_size, queries),
        queries,
    )


@pytest.mark.parametrize(
    ("port_answer", "expected"),
    [
        (
            RunEventPage((EVENT,), terminal_seen=False),
            RunEventsRead((EVENT,), terminal_seen=False),
        ),
        (
            RunEventPage((), terminal_seen=True),
            RunEventsRead((), terminal_seen=True),
        ),
        (PortReadUnavailable("store asleep"), ReadUnavailable("store asleep")),
        (EventHistoryCorrupt(), DurableStateCorrupt()),
        (QueryDurableStateCorrupt(), DurableStateCorrupt()),
    ],
    ids=["a-page", "terminal", "unavailable", "corrupt-history", "corrupt-state"],
)
@pytest.mark.proves("one-page-of-a-stream-is-decided-before-any-frame-is-written")
def test_every_port_answer_of_a_page_read_becomes_this_layers_own_outcome(
    port_answer: Any, expected: Any
) -> None:
    result, queries = read(port_answer)

    assert result == expected
    assert len(queries.asked) == 1


@pytest.mark.proves("one-page-of-a-stream-is-decided-before-any-frame-is-written")
def test_a_page_larger_than_the_one_asked_for_is_its_own_outcome() -> None:
    """The store contradicted its own contract, which is not the history being
    wrong — so it is said in our words, and kept apart from corruption because a
    caller answers the two differently."""
    result, _ = read(
        RunEventPage((EVENT, EVENT, EVENT), terminal_seen=False), page_size=2
    )

    assert result == RunEventPageOversized()


@pytest.mark.proves("one-page-of-a-stream-is-decided-before-any-frame-is-written")
def test_a_full_page_of_exactly_the_size_asked_for_is_not_oversized() -> None:
    """The boundary belongs to the test that draws it: `limit` events is a page,
    `limit + 1` is a broken promise."""
    result, _ = read(RunEventPage((EVENT, EVENT), terminal_seen=False), page_size=2)

    assert result == RunEventsRead((EVENT, EVENT), terminal_seen=False)


@pytest.mark.proves("one-page-of-a-stream-is-decided-before-any-frame-is-written")
def test_a_page_read_asks_the_store_with_exactly_what_the_caller_named() -> None:
    _, queries = read(RunEventPage((), terminal_seen=True), page_size=5)

    assert queries.asked == [(RUN_ID, 7, 5)]


def test_a_page_hands_its_events_on_by_identity_rather_than_reshaping_them() -> None:
    """Rendering belongs above this layer, so the persisted events travel untouched
    and no shape of them is asserted here."""
    persisted: Any = PersistedRunEvent
    result, _ = read(RunEventPage((persisted,), terminal_seen=False))

    assert isinstance(result, RunEventsRead)
    assert result.events[0] is persisted
