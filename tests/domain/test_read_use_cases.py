from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from atelier2.application.prepare_run_events import (
    EventCursorAhead,
    RunEventStreamPrepared,
    prepare_run_events,
)
from atelier2.application.prepare_run_events import RunNotFound as EventRunNotFound
from atelier2.application.read_agent_configurations import (
    AgentConfigurationRevisionsListed,
    AuthProfileRevisionsListed,
    list_agent_configuration_revisions,
    list_auth_profile_revisions,
)
from atelier2.application.read_runs import (
    RunNotFound,
    RunRead,
    RunReceiptsRead,
    RunsListed,
    get_run,
    list_run_receipts,
    list_runs,
)
from atelier2.application.read_workflow_revisions import (
    WorkflowRevisionNotFound,
    WorkflowRevisionRead,
    WorkflowRevisionsListed,
    get_workflow_revision,
    list_workflow_revisions,
)
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.run_projections import (
    RunPage,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.contracts.workflow_projections import (
    WorkflowRevisionPage,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionPage,
    AuthProfileRevisionPage,
    CatalogReadUnavailable,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.run_events import (
    CursorAhead,
    EventHistoryCorrupt,
    StreamReady,
)
from atelier2.ports.run_queries import (
    RunFound,
    RunQueryMissing,
    RunReceiptsFound,
)
from atelier2.ports.workflow_revisions import (
    QueryDurableStateCorrupt,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
)
from atelier2.ports.workflow_revisions import (
    ReadUnavailable as PortReadUnavailable,
)

REVISION_HASH = WorkflowRevisionHash("a" * 64)
RUN_ID = RunId("run")
REVISION_PROJECTION: Any = object()
RUN_PROJECTION: Any = object()


class ScriptedQueries:
    """One store that answers every read with the one answer a case scripts.

    Each use-case calls exactly one method, so a single scripted answer is the
    whole store a case needs. `asked` is what proves the call happened rather than
    the outcome being invented, and every method is spelled out so that this fake
    satisfies the three query protocols by shape instead of by assertion.
    """

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.asked: list[tuple[Any, ...]] = []

    def _record(self, *arguments: Any) -> Any:
        self.asked.append(arguments)
        return self.answer

    def get_workflow_revision(
        self, revision_hash: Any, projection_limit: Any = None
    ) -> Any:
        return self._record(revision_hash)

    def list_described_workflow_revisions(
        self, after: Any, limit: Any, budget: Any, projection_limit: Any = None
    ) -> Any:
        return self._record(after, limit, budget)

    def list_workflow_revisions(self, after: Any, limit: int) -> Any:
        return self._record(after, limit)

    def get_run(self, run_id: Any, projection_limit: Any = None) -> Any:
        return self._record(run_id)

    def list_runs(
        self,
        after: Any,
        limit: int,
        state: Any = None,
        project_id: Any = None,
    ) -> Any:
        return self._record(after, limit, state)

    def get_node_detail(self, run_id: Any, node_id: str) -> Any:
        return self._record(run_id, node_id)

    def list_run_receipts(self, run_id: Any) -> Any:
        return self._record(run_id)

    def prepare_run_event_stream(self, run_id: Any, after_sequence: int) -> Any:
        return self._record(run_id, after_sequence)

    def read_run_event_page(self, *arguments: Any, **keywords: Any) -> Any:
        raise AssertionError("a read under test reached the event page port")

    def get_reconciliation_retry_target(self, *arguments: Any, **keywords: Any) -> Any:
        raise AssertionError("a read under test reached the retry target port")


PORT_REFUSALS = [
    (PortReadUnavailable("store asleep"), ReadUnavailable("store asleep")),
    (QueryDurableStateCorrupt(), DurableStateCorrupt()),
]

READS: list[
    tuple[str, Callable[[ScriptedQueries], object], list[tuple[Any, object]]]
] = [
    (
        "get-workflow-revision",
        lambda queries: get_workflow_revision(REVISION_HASH, queries),
        [
            (
                WorkflowRevisionFound(REVISION_PROJECTION),
                WorkflowRevisionRead(REVISION_PROJECTION),
            ),
            (WorkflowRevisionMissing(), WorkflowRevisionNotFound()),
            *PORT_REFUSALS,
        ],
    ),
    (
        "list-workflow-revisions",
        lambda queries: list_workflow_revisions(None, 50, queries),
        [
            (
                WorkflowRevisionPage((REVISION_HASH,), None),
                WorkflowRevisionsListed((REVISION_HASH,), None),
            ),
            *PORT_REFUSALS,
        ],
    ),
    (
        "get-run",
        lambda queries: get_run(RUN_ID, queries),
        [
            (RunFound(RUN_PROJECTION), RunRead(RUN_PROJECTION)),
            (RunQueryMissing(), RunNotFound()),
            *PORT_REFUSALS,
        ],
    ),
    (
        "list-runs",
        lambda queries: list_runs(None, 50, queries),
        [
            (RunPage((RUN_PROJECTION,), RUN_ID), RunsListed((RUN_PROJECTION,), RUN_ID)),
            *PORT_REFUSALS,
        ],
    ),
    (
        "list-run-receipts",
        lambda queries: list_run_receipts(RUN_ID, queries),
        [
            (RunReceiptsFound(()), RunReceiptsRead(())),
            (RunQueryMissing(), RunNotFound()),
            *PORT_REFUSALS,
        ],
    ),
    (
        "prepare-run-events",
        lambda queries: prepare_run_events(RUN_ID, 7, queries),
        [
            (
                StreamReady(head_sequence=9, terminal=True, first_after=7),
                RunEventStreamPrepared(RUN_ID, 7, 9, True),
            ),
            (RunQueryMissing(), EventRunNotFound()),
            (CursorAhead(), EventCursorAhead()),
            (EventHistoryCorrupt(), DurableStateCorrupt()),
            *PORT_REFUSALS,
        ],
    ),
]


@pytest.mark.proves("every-read-decision-belongs-to-a-use-case")
@pytest.mark.parametrize(
    ("read", "port_answer", "expected"),
    [
        pytest.param(
            read, port_answer, expected, id=f"{name}-{type(port_answer).__name__}"
        )
        for name, read, outcomes in READS
        for port_answer, expected in outcomes
    ],
)
def test_every_port_answer_of_a_read_becomes_this_layers_own_outcome(
    read: Callable[[ScriptedQueries], object], port_answer: object, expected: object
) -> None:
    queries = ScriptedQueries(port_answer)

    assert read(queries) == expected
    assert len(queries.asked) == 1


@pytest.mark.proves("every-read-decision-belongs-to-a-use-case")
def test_a_read_hands_the_projection_on_untouched_rather_than_rendering_it() -> None:
    """The use-case decides; rendering stays above it, so the projection travels
    by identity and no shape of it is asserted here — that contract is the
    projection module's, not this layer's."""
    run = ScriptedQueries(RunFound(RUN_PROJECTION))
    revision = ScriptedQueries(WorkflowRevisionFound(REVISION_PROJECTION))

    read_run = get_run(RUN_ID, run)
    read_revision = get_workflow_revision(REVISION_HASH, revision)

    assert isinstance(read_run, RunRead)
    assert read_run.projection is RUN_PROJECTION
    assert isinstance(read_revision, WorkflowRevisionRead)
    assert read_revision.projection is REVISION_PROJECTION


@pytest.mark.proves("every-read-decision-belongs-to-a-use-case")
def test_a_read_asks_its_port_with_exactly_what_the_caller_named() -> None:
    queries = ScriptedQueries(RunPage((), None))

    list_runs(RUN_ID, 25, queries)

    assert queries.asked == [(RUN_ID, 25, None)]


def test_list_agent_configuration_revisions_becomes_this_layers_own_outcome() -> None:
    listed = AgentConfigurationRevisionPage((), None)

    class Catalog:
        def __init__(self, answer: object) -> None:
            self.answer = answer
            self.asked: list[tuple[object, int]] = []

        def list_agent_configuration_revisions(
            self, after: object, limit: int
        ) -> object:
            self.asked.append((after, limit))
            return self.answer

    for port_answer, expected in (
        (listed, AgentConfigurationRevisionsListed((), None)),
        (CatalogReadUnavailable("store asleep"), ReadUnavailable("store asleep")),
        (PortDurableStateCorrupt(), DurableStateCorrupt()),
    ):
        catalog: Any = Catalog(port_answer)
        assert list_agent_configuration_revisions(None, 50, catalog) == expected
        assert catalog.asked == [(None, 50)]


def test_list_auth_profile_revisions_becomes_this_layers_own_outcome() -> None:
    listed = AuthProfileRevisionPage((), None)

    class Catalog:
        def __init__(self, answer: object) -> None:
            self.answer = answer
            self.asked: list[tuple[object, int]] = []

        def list_auth_profile_revisions(self, after: object, limit: int) -> object:
            self.asked.append((after, limit))
            return self.answer

    for port_answer, expected in (
        (listed, AuthProfileRevisionsListed((), None)),
        (CatalogReadUnavailable("store asleep"), ReadUnavailable("store asleep")),
        (PortDurableStateCorrupt(), DurableStateCorrupt()),
    ):
        catalog: Any = Catalog(port_answer)
        assert list_auth_profile_revisions(None, 50, catalog) == expected
        assert catalog.asked == [(None, 50)]
