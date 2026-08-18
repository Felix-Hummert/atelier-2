"""The durable door for content-addressed material.

Publication is idempotent by construction rather than by a caller's care: the
address is the bytes, so a second publication of the same content is the same
artifact and says so. A store that answered the same address with different
bytes would have lost the one property the address promises, which is why that
disagreement is a named corruption instead of a quiet overwrite.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.artifacts import Artifact
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


@dataclass(frozen=True)
class ArtifactCreated:
    """This publication wrote the bytes."""

    artifact: Artifact


@dataclass(frozen=True)
class ArtifactExisting:
    """The store already held exactly these bytes under this address."""

    artifact: Artifact


type PublishArtifactResult = (
    ArtifactCreated | ArtifactExisting | DurableWriteUnavailable | DurableStateCorrupt
)


class ArtifactPublisher(Protocol):
    def publish_artifact(self, artifact: Artifact) -> PublishArtifactResult: ...
