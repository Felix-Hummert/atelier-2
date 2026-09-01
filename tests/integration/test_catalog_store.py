"""`DbosCatalogStore`'s resolve seam answers many lookups on one connection, once each.

Issue #937 round 1: `resolve` alone checks out (and later returns) a fresh
connection per call -- paid once per pinned reference a listed page resolves,
not once for the whole page. (An earlier draft of this fix believed each of
those calls also took a write lock under this store's `IMMEDIATE` isolation;
a correction on landing found a plain `SELECT` opens no transaction at all
under this stack's DBAPI isolation setting, so what `resolver_session` really
removes is repeated pool checkouts, not write-lock serialization against the
WAL.) These tests pin that fix as the connection count a real engine actually
sees, not as milliseconds: `resolver_session` must answer every lookup made
through the resolver it yields on the one connection it checked out.

Issue #937 round 2: fixing the checkout did not move the measured wall time,
because the dominant cost sits downstream of a found answer, in the
jsonschema validation and YAML parsing every caller runs on the bytes this
seam hands back -- run once per resolved reference, uncached, even when many
revisions on one page pin the same schema. This store cannot cache that
downstream work, but it owns the one lookup every caller shares first:
resolving `(kind, revision_hash)` to the published bytes. `resolve` now
answers a repeat of the same found reference -- inside one session, or
across separate `resolve` calls -- without a second query, so a caller who
resolves the same reference twice queries the store once.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import initialize_schema
from atelier2.application.read_workflow_revisions import (
    WorkflowRevisionsDescribed,
    list_described_workflow_revisions,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.runs import WorkflowRevision
from atelier2.contracts.workflow_projections import EnrichedPageBudget
from atelier2.ports.published_revisions import (
    DurableStateCorrupt,
    PublishedRevisionCreated,
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionsUnavailable,
)
from tests.scenarios.api import permissive_projection_limit
from tests.scenarios.runs import publish_revision

ENRICHED_PAGE_BUDGET = EnrichedPageBudget(
    maximum_nodes=1_000, maximum_document_bytes=1 << 20
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    canonical_engine = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(canonical_engine)
    try:
        yield canonical_engine
    finally:
        canonical_engine.dispose()


class _ConnectionCheckouts:
    """How many times a real engine's pool has handed out a connection.

    A SQLAlchemy `checkout` fires once per `engine.connect()` (and once per
    `with self._engine.connect()` a store method opens), independent of
    whichever physical DBAPI connection the pool reuses underneath -- exactly
    the unit `resolver_session` is supposed to spend once per page rather
    than once per resolved reference.
    """

    def __init__(self, engine: Engine) -> None:
        self.count = 0
        event.listens_for(engine, "checkout")(self._record)

    def _record(self, *_args: object) -> None:
        self.count += 1


class _QueryExecutions:
    """How many statements a real engine's cursor has actually executed.

    Where `_ConnectionCheckouts` counts pool checkouts, this counts the SQL a
    checked-out connection runs -- the unit the resolve-seam cache (#937
    round 2) is supposed to spend once per resolved reference rather than
    once per `resolve` call.
    """

    def __init__(self, engine: Engine) -> None:
        self.count = 0
        event.listens_for(engine, "before_cursor_execute")(self._record)

    def _record(self, *_args: object) -> None:
        self.count += 1


def _published_schema(document: bytes) -> PublishedRevision:
    return PublishedRevision(RevisionKind.SCHEMA, document)


def _wait_document(name: str, schema_revision: str) -> bytes:
    return f"""format_version: 3
name: {name}
nodes:
  - id: ship
    type: wait
    prompt: Ship it?
    outputs:
      - name: decision
        schema: {{ref: decision, revision: {schema_revision}}}
""".encode()


def test_a_resolver_session_answers_every_lookup_on_one_connection(
    engine: Engine,
) -> None:
    """A page's several distinct references resolve through one checkout."""
    store = DbosCatalogStore(engine)
    boolean_schema = _published_schema(b'{"type": "boolean"}')
    string_schema = _published_schema(b'{"type": "string"}')
    for revision in (boolean_schema, string_schema):
        assert isinstance(store.publish_revision(revision), PublishedRevisionCreated)
    checkouts = _ConnectionCheckouts(engine)

    with store.resolver_session() as resolver:
        first = resolver.resolve(RevisionKind.SCHEMA, boolean_schema.revision_hash)
        second = resolver.resolve(RevisionKind.SCHEMA, string_schema.revision_hash)
        third = resolver.resolve(RevisionKind.SCHEMA, PublishedRevisionHash("f" * 64))

    assert first == PublishedRevisionFound(boolean_schema)
    assert second == PublishedRevisionFound(string_schema)
    assert third == PublishedRevisionMissing()
    assert checkouts.count == 1


def test_resolve_answers_a_repeated_lookup_without_a_second_connection(
    engine: Engine,
) -> None:
    """A found reference `resolve` already read answers again without a query (#937 round 2)."""
    store = DbosCatalogStore(engine)
    schema = _published_schema(b'{"type": "boolean"}')
    assert isinstance(store.publish_revision(schema), PublishedRevisionCreated)
    checkouts = _ConnectionCheckouts(engine)

    first = store.resolve(RevisionKind.SCHEMA, schema.revision_hash)
    second = store.resolve(RevisionKind.SCHEMA, schema.revision_hash)

    assert first == PublishedRevisionFound(schema)
    assert second == PublishedRevisionFound(schema)
    assert checkouts.count == 1


def test_resolve_still_opens_one_connection_per_distinct_reference(
    engine: Engine,
) -> None:
    """The cache answers a repeat of the same reference, never a different one."""
    store = DbosCatalogStore(engine)
    boolean_schema = _published_schema(b'{"type": "boolean"}')
    string_schema = _published_schema(b'{"type": "string"}')
    for revision in (boolean_schema, string_schema):
        assert isinstance(store.publish_revision(revision), PublishedRevisionCreated)
    checkouts = _ConnectionCheckouts(engine)

    store.resolve(RevisionKind.SCHEMA, boolean_schema.revision_hash)
    store.resolve(RevisionKind.SCHEMA, string_schema.revision_hash)

    assert checkouts.count == 2


def test_resolve_does_not_cache_a_miss_a_later_publish_fills(engine: Engine) -> None:
    """An unpublished hash may be published moments later; a cached miss must not answer stale."""
    store = DbosCatalogStore(engine)
    schema = _published_schema(b'{"type": "boolean"}')

    before = store.resolve(RevisionKind.SCHEMA, schema.revision_hash)
    assert isinstance(store.publish_revision(schema), PublishedRevisionCreated)
    after = store.resolve(RevisionKind.SCHEMA, schema.revision_hash)

    assert before == PublishedRevisionMissing()
    assert after == PublishedRevisionFound(schema)


def test_a_resolver_session_resolves_the_same_reference_once(engine: Engine) -> None:
    """Two wait nodes on the page pinning the same schema query the store once (#937 round 2)."""
    store = DbosCatalogStore(engine)
    schema = _published_schema(b'{"type": "boolean"}')
    assert isinstance(store.publish_revision(schema), PublishedRevisionCreated)
    executions = _QueryExecutions(engine)

    with store.resolver_session() as resolver:
        first = resolver.resolve(RevisionKind.SCHEMA, schema.revision_hash)
        second = resolver.resolve(RevisionKind.SCHEMA, schema.revision_hash)

    assert first == PublishedRevisionFound(schema)
    assert second == PublishedRevisionFound(schema)
    assert executions.count == 1


def test_a_resolver_session_the_store_cannot_query_answers_unavailable(
    engine: Engine,
) -> None:
    """A registry outage discovered mid-session is a refusal, never a 500 (#701)."""
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE published_revisions"))
    store = DbosCatalogStore(engine)

    with store.resolver_session() as resolver:
        resolved = resolver.resolve(
            RevisionKind.SCHEMA, PublishedRevisionHash("a" * 64)
        )

    assert resolved == PublishedRevisionsUnavailable()


def test_a_resolver_session_that_cannot_open_a_connection_answers_unavailable(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A connection the store cannot open at all answers the same typed
    refusal a single `resolve` call already gives for that failure -- it must
    not escape `resolver_session.__enter__` as a raw `OperationalError`."""
    store = DbosCatalogStore(engine)

    def _refuse_to_connect() -> sa.Connection:
        raise OperationalError("connect", {}, Exception("pool exhausted"))

    monkeypatch.setattr(engine, "connect", _refuse_to_connect)

    with store.resolver_session() as resolver:
        resolved = resolver.resolve(
            RevisionKind.SCHEMA, PublishedRevisionHash("a" * 64)
        )

    assert resolved == PublishedRevisionsUnavailable()


def test_a_resolver_session_that_cannot_open_a_connection_names_corruption_too(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same mapping `resolve` gives a non-operational failure applies here."""
    store = DbosCatalogStore(engine)

    def _fail_to_connect() -> sa.Connection:
        raise RuntimeError("durable engine misconfigured")

    monkeypatch.setattr(engine, "connect", _fail_to_connect)

    with store.resolver_session() as resolver:
        resolved = resolver.resolve(
            RevisionKind.SCHEMA, PublishedRevisionHash("a" * 64)
        )

    assert resolved == DurableStateCorrupt()


def test_a_described_page_checks_out_the_query_connection_and_one_session(
    engine: Engine,
) -> None:
    """A full described page, through the real application read, checks out
    exactly two connections: `DbosQueries.list_described_workflow_revisions`
    -- a port this fix does not touch -- owns one composed read of its own,
    and `resolver_session` (this fix) owns the other for every reference the
    page resolves. Unifying the two into one shared connection would need
    `WorkflowRevisionQueries` to accept a caller's connection too, which is a
    wider change than this ladder step's smallest honest fix makes; two is
    the ruled shape for this slice, not a leftover to close later by accident.
    """
    store = DbosCatalogStore(engine)
    schema = _published_schema(b'{"type": "boolean"}')
    assert isinstance(store.publish_revision(schema), PublishedRevisionCreated)
    for index in range(3):
        publish_revision(
            engine,
            WorkflowRevision(
                _wait_document(f"revision {index}", schema.revision_hash.value)
            ),
        )
    queries = DbosQueries(engine, permissive_projection_limit())
    checkouts = _ConnectionCheckouts(engine)

    result = list_described_workflow_revisions(
        None, 50, ENRICHED_PAGE_BUDGET, queries, store
    )

    assert isinstance(result, WorkflowRevisionsDescribed)
    assert len(result.items) == 3
    assert checkouts.count == 2
