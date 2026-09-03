"""Reading an artifact: an address in, the exact bytes it names out.

The read side of the door `publish_artifact` opened. It invents no bound and no
second identity: whatever the store holds under an address hashes to it, so the
answers are the material, its absence, or a named corruption when stored bytes
break that identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt
from atelier2.contracts.artifacts import Artifact, ArtifactHash
from atelier2.ports.artifacts import ArtifactReader
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt


@dataclass(frozen=True)
class ArtifactRead:
    artifact: Artifact


@dataclass(frozen=True)
class ArtifactNotFound:
    pass


type ReadArtifactResult = ArtifactRead | ArtifactNotFound | DurableStateCorrupt


def read_artifact(
    artifact_hash: ArtifactHash, reader: ArtifactReader
) -> ReadArtifactResult:
    match reader.read_artifact(artifact_hash):
        case Artifact() as stored:
            return ArtifactRead(stored)
        case None:
            return ArtifactNotFound()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
