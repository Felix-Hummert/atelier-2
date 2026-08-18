"""Material a run refers to instead of carrying: stored once, named by its bytes.

**Why this exists.** An order travels inside the start that supplies it, and that
start is bounded because a run must not be asked to carry a value nobody can
read. Real material outgrew the bound: a full pull-request diff is measured in
hundreds of kilobytes, so the only way to hand one to an agent was not to hand it
at all. An artifact is that material published beside the order -- the inline
bound stays strict, the order stays slim, and the bytes still reach the agent.

**Why the content is the identity.** The bytes are the name. Publishing the same
bytes twice lands on the same artifact, so a caller that retries has not created
a second one, and a record naming an artifact names exactly the bytes that were
read. It is the discipline a published revision already keeps, applied to
material that is not a revision: nobody references an artifact from a document,
and nothing about it is versioned.

**Why it is not a `node-artifact/v3`.** That one is a value a node produced,
bound to the node execution that produced it and to the schema it satisfied.
This is material somebody published before any node ran, bound to nothing but its
own bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.hashing import Sha256Hash

MAXIMUM_ARTIFACT_BYTES = 1_048_576
"""What one artifact may cost, everywhere it is read whole.

An artifact is never streamed: it is read into memory to publish it, again to
judge it against the schema its order pinned, and again to compose the job an
agent is handed. This bound is therefore the cost of a single resident copy of
the largest material this exists for -- a full pull-request diff, measured at
roughly a hundred kilobytes on the thread that raised it -- with an order of
magnitude above that measurement, rather than a number chosen to feel large.
"""


class ArtifactHash(Sha256Hash):
    """The content address of one artifact: the SHA-256 of its exact bytes."""


class ArtifactRefusal(StrEnum):
    """Every named way bytes fail to be an artifact this store keeps."""

    ARTIFACT_EMPTY = "artifact-empty"
    ARTIFACT_TOO_LARGE = "artifact-too-large"


@dataclass(frozen=True, slots=True)
class ArtifactRefused:
    """Why these bytes are not material this store keeps."""

    refusal: ArtifactRefusal
    subject: str | None = None

    def __str__(self) -> str:
        if self.subject is None:
            return self.refusal.value
        return f"{self.refusal.value}: {self.subject}"


@dataclass(frozen=True)
class Artifact:
    """Exact bytes, under the address they hash to."""

    content: bytes
    artifact_hash: ArtifactHash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("an artifact carries exact bytes")
        object.__setattr__(self, "artifact_hash", ArtifactHash.of(self.content))


type ArtifactVerdict = Artifact | ArtifactRefused


def read_artifact_content(content: bytes) -> ArtifactVerdict:
    """Whether these exact bytes are material this store keeps, and under which address.

    Emptiness is refused before size because an artifact nobody can read is not
    the same fault as one nobody can afford, and a caller fixes them differently.
    """
    if not content:
        return ArtifactRefused(ArtifactRefusal.ARTIFACT_EMPTY)
    if len(content) > MAXIMUM_ARTIFACT_BYTES:
        return ArtifactRefused(
            ArtifactRefusal.ARTIFACT_TOO_LARGE,
            f"{len(content)} bytes exceeds {MAXIMUM_ARTIFACT_BYTES}",
        )
    return Artifact(content)
