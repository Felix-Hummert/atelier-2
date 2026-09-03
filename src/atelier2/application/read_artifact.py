"""Reading an artifact: an address in, the exact bytes it names out.

The read side of the door `publish_artifact` opened. It invents no bound and no
second identity: whatever the store holds under an address hashes to it, so the
only two answers are the material and its absence.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.artifacts import Artifact, ArtifactHash
from atelier2.ports.artifacts import ArtifactReader


@dataclass(frozen=True)
class ArtifactRead:
    artifact: Artifact


@dataclass(frozen=True)
class ArtifactNotFound:
    pass


type ReadArtifactResult = ArtifactRead | ArtifactNotFound


def read_artifact(
    artifact_hash: ArtifactHash, reader: ArtifactReader
) -> ReadArtifactResult:
    stored = reader.read_artifact(artifact_hash)
    if stored is None:
        return ArtifactNotFound()
    return ArtifactRead(stored)
