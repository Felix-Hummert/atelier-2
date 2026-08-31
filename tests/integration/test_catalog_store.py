"""`DbosCatalogStore.resolver_session` answers many lookups on one connection.

Issue #937: `resolve` alone opens a fresh connection per call, and this
store's `IMMEDIATE` isolation (`adapters/dbos/runtime.py`) turns every one of
those into a write lock serialized against the WAL -- paid once per pinned
reference a listed page resolves, not once for the whole page. These tests
pin the fix as the connection count a real engine actually sees, not as
milliseconds: `resolver_session` must answer every lookup made through the
resolver it yields on the one connection it opened, and `resolve` on its own
must keep opening one connection per call, exactly as before this session
existed.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import initialize_schema
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.ports.published_revisions import (
    PublishedRevisionCreated,
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishedRevisionsUnavailable,
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


def _published_schema(document: bytes) -> PublishedRevision:
    return PublishedRevision(RevisionKind.SCHEMA, document)


def test_a_resolver_session_answers_every_lookup_on_one_connection(
    engine: Engine,
) -> None:
    """A page's several distinct references resolve inside one transaction."""
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


def test_resolve_alone_still_opens_one_connection_per_call(engine: Engine) -> None:
    """Outside a session, `resolve` keeps its original per-lookup connection cost."""
    store = DbosCatalogStore(engine)
    schema = _published_schema(b'{"type": "boolean"}')
    assert isinstance(store.publish_revision(schema), PublishedRevisionCreated)
    checkouts = _ConnectionCheckouts(engine)

    store.resolve(RevisionKind.SCHEMA, schema.revision_hash)
    store.resolve(RevisionKind.SCHEMA, schema.revision_hash)

    assert checkouts.count == 2


def test_a_resolver_session_the_store_cannot_serve_answers_unavailable(
    engine: Engine,
) -> None:
    """A registry outage inside a session is a refusal, never a 500 (#701)."""
    with engine.begin() as connection:
        connection.execute(sa.text("DROP TABLE published_revisions"))
    store = DbosCatalogStore(engine)

    with store.resolver_session() as resolver:
        resolved = resolver.resolve(
            RevisionKind.SCHEMA, PublishedRevisionHash("a" * 64)
        )

    assert resolved == PublishedRevisionsUnavailable()
