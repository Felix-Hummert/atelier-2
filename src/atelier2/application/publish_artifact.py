"""Publishing an artifact: exact bytes in, the store's own write, an address out.

The bound and the emptiness rule live with the contract that owns what an
artifact is; this is the door that turns that verdict into an outcome a caller
can act on, and it invents no second bound of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.artifacts import (
    Artifact,
    ArtifactRefused,
    read_artifact_content,
)
from atelier2.ports.artifacts import (
    ArtifactCreated,
    ArtifactExisting,
    ArtifactPublisher,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable


@dataclass(frozen=True)
class ArtifactPublicationCreated:
    artifact: Artifact


@dataclass(frozen=True)
class ArtifactPublicationExisting:
    artifact: Artifact


@dataclass(frozen=True)
class ArtifactPublicationInvalid:
    verdict: ArtifactRefused


type PublishArtifactUseCaseResult = (
    ArtifactPublicationCreated
    | ArtifactPublicationExisting
    | ArtifactPublicationInvalid
    | WriteUnavailable
    | DurableStateCorrupt
)


def publish_artifact(
    content: bytes, publisher: ArtifactPublisher
) -> PublishArtifactUseCaseResult:
    verdict = read_artifact_content(content)
    if isinstance(verdict, ArtifactRefused):
        return ArtifactPublicationInvalid(verdict)
    result = publisher.publish_artifact(verdict)
    match result:
        case ArtifactCreated(stored):
            return ArtifactPublicationCreated(stored)
        case ArtifactExisting(stored):
            return ArtifactPublicationExisting(stored)
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
