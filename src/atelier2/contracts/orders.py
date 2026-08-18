"""How the value of one order is supplied: as bytes, or as the artifact holding them.

An order is material a start carries beside the document, and there are exactly
two honest ways to hand it over. Inline is what a start can carry itself, and it
stays bounded by `MAXIMUM_INSTANCE_DOCUMENT_BYTES`. An artifact reference is what
material larger than that looks like from the start's side: the bytes were
published before, and the start resolves the address to them.

They are two types rather than one field that sometimes holds a hash, because a
caller must not be able to say both or neither, and because a reader must not
have to guess which of the two a string is.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.artifacts import ArtifactHash


@dataclass(frozen=True, slots=True)
class InlineOrderValue:
    """The exact bytes a caller wrote into the start itself."""

    content: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes):
            raise TypeError("an inline order value carries exact bytes")


@dataclass(frozen=True, slots=True)
class ArtifactOrderValue:
    """The published artifact whose bytes this order is."""

    artifact_hash: ArtifactHash

    def __post_init__(self) -> None:
        if not isinstance(self.artifact_hash, ArtifactHash):
            raise TypeError("an artifact order value names a typed artifact hash")


type AuthoredOrderValue = InlineOrderValue | ArtifactOrderValue
