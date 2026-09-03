from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.schema import artifacts
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.artifacts import Artifact, ArtifactHash
from atelier2.ports.artifacts import (
    ArtifactCreated,
    ArtifactExisting,
    PublishArtifactResult,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


def read_stored_artifact(
    connection: sa.Connection, artifact_hash: ArtifactHash
) -> Artifact | None:
    """The artifact this address names, read on the caller's own connection.

    A caller resolving an artifact is deciding something else at the same time --
    whether a run may start -- and that decision is serialized against the writes
    it must not race. Handing it a connection instead of an engine keeps the read
    inside that transaction rather than beside it.
    """
    content = connection.scalar(
        sa.select(artifacts.c.content).where(
            artifacts.c.artifact_hash == artifact_hash.value
        )
    )
    if content is None:
        return None
    stored = Artifact(bytes(content))
    if stored.artifact_hash != artifact_hash:
        raise RuntimeError(
            f"stored artifact {artifact_hash.value} does not hash to its address"
        )
    return stored


def read_stored_artifacts(
    connection: sa.Connection, artifact_hashes: Sequence[ArtifactHash]
) -> dict[str, Artifact]:
    """Every one of these addresses' artifacts, read in one statement.

    The batched sibling of `read_stored_artifact`: a caller resolving more
    than one artifact at once -- a page of terminal refusals (#1045) -- reads
    them all here instead of once per address. Same corruption rule: a stored
    row whose content disagrees with its own address raises rather than
    answering it as that artifact. Keyed by the hash string rather than
    `ArtifactHash` so a caller can look an entry up straight from a raw
    column value with no re-wrapping.
    """
    if not artifact_hashes:
        return {}
    found: dict[str, Artifact] = {}
    for record in connection.execute(
        sa.select(artifacts.c.artifact_hash, artifacts.c.content).where(
            artifacts.c.artifact_hash.in_(
                tuple(value.value for value in artifact_hashes)
            )
        )
    ):
        stored = Artifact(bytes(record.content))
        if stored.artifact_hash.value != str(record.artifact_hash):
            raise RuntimeError(
                f"stored artifact {record.artifact_hash} does not hash to its address"
            )
        found[str(record.artifact_hash)] = stored
    return found


def keep_artifact(
    connection: sa.Connection, artifact: Artifact
) -> ArtifactCreated | ArtifactExisting:
    """Hold these exact bytes under their address, on the caller's own connection.

    Publication reads before it writes rather than inserting `OR IGNORE`, because
    the two answers are different facts a caller acts on: the address it already
    had, or the bytes this call put there. A stored row whose content disagrees
    with its own address is corruption of the one property the address promises,
    so `read_stored_artifact` raises it where it is seen instead of answering it
    as an artifact.

    A caller already inside a transaction keeps the material and the record that
    names it in the same write, so no record can name bytes this store never got.
    """
    existing = read_stored_artifact(connection, artifact.artifact_hash)
    if existing is not None:
        return ArtifactExisting(existing)
    connection.execute(
        artifacts.insert().values(
            artifact_hash=artifact.artifact_hash.value,
            content=artifact.content,
        )
    )
    return ArtifactCreated(artifact)


class DbosArtifactStore:
    """Content-addressed material over the V19 `artifacts` table."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def publish_artifact(self, artifact: Artifact) -> PublishArtifactResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                return keep_artifact(connection, artifact)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
