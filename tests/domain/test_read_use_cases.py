from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from atelier2.adapters.markdown_agent_definitions import parse_agent_definition
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.application.evaluate_executability import (
    DocumentNotExecutable,
    ExecutableDocument,
    evaluate_executability,
)
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
from atelier2.application.read_agent_definition_revisions import (
    AgentDefinitionRevisionNotFound,
    AgentDefinitionRevisionRead,
    AgentDefinitionRevisionsListed,
    PublishedAgentDefinition,
    get_agent_definition_revision,
    list_agent_definition_revisions,
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
    WaitAnswerClassification,
    WorkflowRevisionNotFound,
    WorkflowRevisionRead,
    WorkflowRevisionsDescribed,
    WorkflowRevisionsListed,
    get_workflow_revision,
    list_described_workflow_revisions,
    list_workflow_revisions,
)
from atelier2.application.refusals import DurableStateCorrupt, ReadUnavailable
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind
from atelier2.contracts.run_projections import (
    RunPage,
)
from atelier2.contracts.runs import RunId, WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.workflow_projections import (
    DescribedWorkflowRevisionPage,
    EnrichedPageBudget,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionPage,
    AuthProfileRevisionPage,
    CatalogReadUnavailable,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionPage,
    PublishedRevisionsUnavailable,
)
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
from tests.scenarios.workflows import ANY_JSON_SCHEMA, declared_output

ONE_AGENT_DOCUMENT = b"""format_version: 3
name: One agent, nothing a resolver classifies
nodes:
  - id: implement
    type: agent
    role: builder
    mode: headless
    instruction: Do the one thing this chain is for.
""" + declared_output()

REVISION_HASH = WorkflowRevisionHash("a" * 64)
RUN_ID = RunId("run")
REVISION_PROJECTION = WorkflowRevisionProjection(
    WorkflowRevision(ONE_AGENT_DOCUMENT),
    parse_workflow_document(ONE_AGENT_DOCUMENT),
)
"""An executable revision, its one declared output schema resolved by name."""
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
        projection_limit: Any = None,
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

    def read_attention_event_page(self, *arguments: Any, **keywords: Any) -> Any:
        raise AssertionError("a read under test reached the attention feed")

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
        lambda queries: get_workflow_revision(
            REVISION_HASH,
            queries,
            ScriptedResolver(PublishedRevisionFound(ANY_JSON_SCHEMA)),
        ),
        [
            (
                WorkflowRevisionFound(REVISION_PROJECTION),
                WorkflowRevisionRead(REVISION_PROJECTION, None),
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
    read_revision = get_workflow_revision(
        REVISION_HASH, revision, ScriptedResolver(PublishedRevisionMissing())
    )

    assert isinstance(read_run, RunRead)
    assert read_run.projection is RUN_PROJECTION
    assert isinstance(read_revision, WorkflowRevisionRead)
    assert read_revision.projection is REVISION_PROJECTION


@pytest.mark.proves("every-read-decision-belongs-to-a-use-case")
def test_a_read_asks_its_port_with_exactly_what_the_caller_named() -> None:
    queries = ScriptedQueries(RunPage((), None))

    list_runs(RUN_ID, 25, queries)

    assert queries.asked == [(RUN_ID, 25, None)]


class ScriptedResolver:
    """A published-revision resolver that answers every resolve with one scripted answer."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.asked: list[tuple[Any, Any]] = []

    def resolve(self, kind: Any, revision_hash: Any) -> Any:
        self.asked.append((kind, revision_hash))
        return self.answer


class ScriptedSessionResolver:
    """A resolver that only answers `resolve` inside the one session it opens.

    Pins the shape `DbosCatalogStore.resolver_session` gives a composed read
    (#937): every lookup a page needs must go through the session it opened
    for that page, never through a `resolve` call made on this object
    directly -- the old, one-connection-per-lookup way.
    """

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.sessions: list[ScriptedResolver] = []

    def resolve(self, kind: Any, revision_hash: Any) -> Any:
        raise AssertionError(
            "a page resolved a reference outside the one session opened for it"
        )

    @contextmanager
    def resolver_session(self) -> Iterator[ScriptedResolver]:
        session = ScriptedResolver(self.answer)
        self.sessions.append(session)
        yield session


WAIT_NODE_ID = "ship"
WELL_FORMED_UNPUBLISHED_HASH = "b" * 64


def _wait_revision_projection(schema_revision: str) -> WorkflowRevisionProjection:
    """One published V3 revision with one wait node, pinning the named schema revision."""
    document = f"""format_version: 3
name: Ship it or hold it
nodes:
  - id: {WAIT_NODE_ID}
    type: wait
    prompt: Ship it?
    outputs:
      - name: decision
        schema: {{ref: decision, revision: {schema_revision}}}
""".encode()
    return WorkflowRevisionProjection(
        WorkflowRevision(document), parse_workflow_document(document)
    )


def test_a_document_whose_pinned_reference_nothing_published_answers_is_not_executable() -> (
    None
):
    """The reader's verdict is the start's: a form nothing binds, or a reference
    nothing published answers, and the reason names which."""
    projection = _wait_revision_projection(WELL_FORMED_UNPUBLISHED_HASH)

    evaluated = evaluate_executability(
        projection.graph, ScriptedResolver(PublishedRevisionMissing())
    )

    assert isinstance(evaluated, DocumentNotExecutable), evaluated
    assert "no published revision of this kind carries this hash" in evaluated.reason
    assert f"decision@{WELL_FORMED_UNPUBLISHED_HASH}" in evaluated.reason


def test_a_document_whose_every_reference_resolves_waits_for_nothing() -> None:
    schema = PublishedRevision(RevisionKind.SCHEMA, b'{"type": "boolean"}')
    projection = _wait_revision_projection(schema.revision_hash.value)

    evaluated = evaluate_executability(
        projection.graph, ScriptedResolver(PublishedRevisionFound(schema))
    )

    assert isinstance(evaluated, ExecutableDocument), evaluated
    assert [entry.revision_hash for entry in evaluated.resolutions] == [
        schema.revision_hash
    ]


def test_a_registry_that_cannot_answer_for_a_pinned_reference_is_a_read_refusal() -> (
    None
):
    """Not `free`, not `not executable`: the store did not answer, and the read says so."""
    projection = _wait_revision_projection(WELL_FORMED_UNPUBLISHED_HASH)
    queries = ScriptedQueries(WorkflowRevisionFound(projection))
    resolver = ScriptedResolver(PublishedRevisionsUnavailable("registry asleep"))

    result = get_workflow_revision(REVISION_HASH, queries, resolver)

    assert result == ReadUnavailable("registry asleep")


@pytest.mark.parametrize(
    ("schema_revision", "resolver_answer"),
    [
        pytest.param(
            "schema-decision", PublishedRevisionMissing(), id="malformed-pinned-hash"
        ),
        pytest.param(
            WELL_FORMED_UNPUBLISHED_HASH,
            PublishedRevisionMissing(),
            id="no-published-revision-carries-this-hash",
        ),
        pytest.param(
            WELL_FORMED_UNPUBLISHED_HASH,
            PublishedRevisionFound(
                PublishedRevision(RevisionKind.TOOL, b'{"type": "boolean"}')
            ),
            id="revision-published-under-a-different-kind",
        ),
        pytest.param(
            WELL_FORMED_UNPUBLISHED_HASH,
            PublishedRevisionFound(PublishedRevision(RevisionKind.SCHEMA, b"not json")),
            id="published-bytes-are-not-a-schema-this-product-enforces",
        ),
    ],
)
def test_a_wait_answer_schema_classifies_free_for_every_named_resolution_failure(
    schema_revision: str, resolver_answer: object
) -> None:
    """None of the four reasons `_classify_wait_answer` names is silently
    swallowed into `free`: each is its own scripted resolver answer, and each
    still ends at the same honest verdict."""
    projection = _wait_revision_projection(schema_revision)
    queries = ScriptedQueries(WorkflowRevisionFound(projection))
    resolver = ScriptedResolver(resolver_answer)

    result = get_workflow_revision(REVISION_HASH, queries, resolver)

    assert isinstance(result, WorkflowRevisionRead)
    assert result.wait_answer_classifications == (
        WaitAnswerClassification(node_id=WAIT_NODE_ID, kind="free"),
    )


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


def test_list_agent_definition_revisions_becomes_this_layers_own_outcome() -> None:
    class Listing:
        def __init__(self, answer: object) -> None:
            self.answer = answer
            self.asked: list[tuple[object, object, int]] = []

        def list_revisions(self, kind: object, after: object, limit: int) -> object:
            self.asked.append((kind, after, limit))
            return self.answer

    for port_answer, expected in (
        (PublishedRevisionPage((), None), AgentDefinitionRevisionsListed((), None)),
        (
            PublishedRevisionsUnavailable("store asleep"),
            ReadUnavailable("store asleep"),
        ),
        (PortDurableStateCorrupt(), DurableStateCorrupt()),
    ):
        listing: Any = Listing(port_answer)
        assert (
            list_agent_definition_revisions(None, 50, listing, parse_agent_definition)
            == expected
        )
        assert listing.asked == [(RevisionKind.AGENT_DEFINITION, None, 50)]


def test_a_stored_definition_that_no_longer_parses_is_named_as_corruption() -> None:
    """The publish door refuses what this parser refuses, so unreadable bytes lie.

    Skipping the entry would show a shorter catalog than the store holds, which
    is the one answer a browse must never give.
    """

    class Listing:
        def list_revisions(self, kind: object, after: object, limit: int) -> object:
            del kind, after, limit
            return PublishedRevisionPage(
                (
                    PublishedRevision(
                        RevisionKind.AGENT_DEFINITION, b"no frontmatter here\n"
                    ),
                ),
                None,
            )

    listing: Any = Listing()

    assert (
        list_agent_definition_revisions(None, 50, listing, parse_agent_definition)
        == DurableStateCorrupt()
    )


AGENT_DEFINITION_DOCUMENT = (
    b"---\n"
    b"name: stage-name-witness\n"
    b"description: Watches the stage and names what it sees.\n"
    b"model: sonnet\n"
    b"tools: Read, Grep\n"
    b"---\n"
    b"\nYou watch the stage and name what you see.\n"
)


def test_get_agent_definition_revision_becomes_this_layers_own_outcome() -> None:
    published = PublishedRevision(
        RevisionKind.AGENT_DEFINITION, AGENT_DEFINITION_DOCUMENT
    )
    definition = parse_agent_definition(AGENT_DEFINITION_DOCUMENT)

    for resolver_answer, expected in (
        (
            PublishedRevisionFound(published),
            AgentDefinitionRevisionRead(
                PublishedAgentDefinition(published.revision_hash, definition)
            ),
        ),
        (PublishedRevisionMissing(), AgentDefinitionRevisionNotFound()),
        (
            # A hash that resolves under a different published kind names no
            # AGENT_DEFINITION revision, exactly as one nobody published.
            PublishedRevisionFound(
                PublishedRevision(RevisionKind.SCHEMA, b'{"type": "boolean"}')
            ),
            AgentDefinitionRevisionNotFound(),
        ),
        (
            # The store did not say "no such revision" -- it could not answer at
            # all, which is not the same claim and must not be told as one.
            PublishedRevisionsUnavailable("registry asleep"),
            ReadUnavailable("registry asleep"),
        ),
        (PortDurableStateCorrupt(), DurableStateCorrupt()),
    ):
        resolver = ScriptedResolver(resolver_answer)
        assert (
            get_agent_definition_revision(
                published.revision_hash, resolver, parse_agent_definition
            )
            == expected
        )
        assert resolver.asked == [
            (RevisionKind.AGENT_DEFINITION, published.revision_hash)
        ]


def test_a_resolved_definition_that_no_longer_parses_is_named_as_corruption() -> None:
    published = PublishedRevision(
        RevisionKind.AGENT_DEFINITION, b"no frontmatter here\n"
    )

    result = get_agent_definition_revision(
        published.revision_hash,
        ScriptedResolver(PublishedRevisionFound(published)),
        parse_agent_definition,
    )

    assert result == DurableStateCorrupt()


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


ENRICHED_PAGE_BUDGET = EnrichedPageBudget(
    maximum_nodes=1_000, maximum_document_bytes=1 << 20
)


def test_a_described_page_resolves_every_reference_inside_one_session() -> None:
    """A page's references all resolve inside the one session opened for it, not
    one session -- or one connection -- per item or per pinned reference (#937).
    `resolver_session` is mandatory: this read never falls back to asking
    `resolve` one lookup at a time."""
    schema_one = PublishedRevision(RevisionKind.SCHEMA, b'{"type": "boolean"}')
    schema_two = PublishedRevision(RevisionKind.SCHEMA, b'{"type": "string"}')
    first_item = _wait_revision_projection(schema_one.revision_hash.value)
    second_item = _wait_revision_projection(schema_two.revision_hash.value)
    queries = ScriptedQueries(
        DescribedWorkflowRevisionPage((first_item, second_item), None)
    )
    resolver = ScriptedSessionResolver(PublishedRevisionFound(schema_one))

    result = list_described_workflow_revisions(
        None, 50, ENRICHED_PAGE_BUDGET, queries, resolver
    )

    assert isinstance(result, WorkflowRevisionsDescribed)
    assert len(result.items) == 2
    assert len(resolver.sessions) == 1, "one session must serve the whole page"
    distinct_references_resolved = set(resolver.sessions[0].asked)
    assert distinct_references_resolved == {
        (RevisionKind.SCHEMA, schema_one.revision_hash),
        (RevisionKind.SCHEMA, schema_two.revision_hash),
    }, "each item's own pinned reference must resolve through that one session"
