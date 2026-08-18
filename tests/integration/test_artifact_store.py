"""The durable door for content-addressed material.

An artifact is the one thing this store keeps that nobody names: it has no kind,
no revision and no reference from a document, only the bytes it is. What this
file pins is what that buys -- a publication that can be retried, an address
that is the content, and a row nothing may rewrite afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from atelier2.adapters.dbos.artifact_store import (
    DbosArtifactStore,
    read_stored_artifact,
)
from atelier2.adapters.dbos.runtime import create_canonical_engine
from atelier2.adapters.dbos.schema import artifacts, initialize_schema
from atelier2.contracts.artifacts import (
    MAXIMUM_ARTIFACT_BYTES,
    Artifact,
    ArtifactHash,
)
from atelier2.ports.artifacts import ArtifactCreated, ArtifactExisting

A_DIFF = b"diff --git a/one b/one\n+one\n"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    opened = create_canonical_engine(tmp_path / "atelier.sqlite")
    initialize_schema(opened)
    try:
        yield opened
    finally:
        opened.dispose()


def publish(engine: Engine, content: bytes) -> Artifact:
    result = DbosArtifactStore(engine).publish_artifact(Artifact(content))
    assert isinstance(result, (ArtifactCreated, ArtifactExisting)), result
    return result.artifact


def test_publishing_the_same_bytes_twice_lands_on_one_artifact(engine: Engine) -> None:
    """A caller that retries has not created a second artifact, and hears so."""
    store = DbosArtifactStore(engine)

    first = store.publish_artifact(Artifact(A_DIFF))
    second = store.publish_artifact(Artifact(A_DIFF))

    assert isinstance(first, ArtifactCreated)
    assert isinstance(second, ArtifactExisting)
    assert first.artifact.artifact_hash == second.artifact.artifact_hash
    with engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(artifacts)) == 1


def test_an_artifact_reads_back_as_the_exact_bytes_its_address_names(
    engine: Engine,
) -> None:
    stored = publish(engine, A_DIFF)

    with engine.connect() as connection:
        assert read_stored_artifact(connection, stored.artifact_hash) == Artifact(
            A_DIFF
        )


def test_an_address_nothing_was_published_under_reads_as_absent(
    engine: Engine,
) -> None:
    with engine.connect() as connection:
        assert read_stored_artifact(connection, ArtifactHash("ab" * 32)) is None


def test_the_largest_artifact_this_product_admits_survives_a_round_trip(
    engine: Engine,
) -> None:
    """The bound is what the store keeps, not what the contract merely says."""
    content = b"x" * MAXIMUM_ARTIFACT_BYTES
    stored = publish(engine, content)

    with engine.connect() as connection:
        read_back = read_stored_artifact(connection, stored.artifact_hash)

    assert read_back is not None
    assert read_back.content == content


@pytest.mark.parametrize(
    "rewrite",
    [
        pytest.param(artifacts.update().values(content=b"tampered"), id="update"),
        pytest.param(artifacts.delete(), id="delete"),
    ],
)
def test_a_published_artifact_can_no_longer_be_rewritten(
    engine: Engine, rewrite: sa.Executable
) -> None:
    """An address that could come to mean other bytes would promise nothing."""
    publish(engine, A_DIFF)

    with (
        pytest.raises(IntegrityError, match="artifacts are immutable"),
        engine.begin() as connection,
    ):
        connection.execute(rewrite)


def test_the_store_refuses_content_the_contract_bound_does_not_admit(
    engine: Engine,
) -> None:
    """The contract's bound is also the store's, so neither can drift alone."""
    oversized = b"x" * (MAXIMUM_ARTIFACT_BYTES + 1)

    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(
            artifacts.insert().values(
                artifact_hash=ArtifactHash.of(oversized).value, content=oversized
            )
        )
