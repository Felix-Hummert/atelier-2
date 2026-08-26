"""How the value of one order is supplied: as bytes, as an artifact, as an item.

An order is material a start carries beside the document, and there are exactly
two honest ways to hand *bytes* over. Inline is what a start can carry itself,
and it stays bounded by `MAXIMUM_INSTANCE_DOCUMENT_BYTES`. An artifact reference
is what material larger than that looks like from the start's side: the bytes
were published before, and the start resolves the address to them.

They are separate types rather than one field that sometimes holds a hash,
because a caller must not be able to say both or neither, and because a reader
must not have to guess which of them a string is.

A work item is the third way, and it is different in kind: the caller names an
item in the connected project's tracker and the *start* reads it, so the bytes
the run pins are the platform's own at that moment rather than something the
caller typed. It is therefore what a start door accepts, never what the durable
start carries: the reading resolves it into an inline value -- the exact
observed revision (ADR 0010 §5) -- before any durable row exists.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.artifacts import ArtifactHash
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.work_items import ObservedWorkItemRevision


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


@dataclass(frozen=True, slots=True)
class WorkItemOrderValue:
    """The tracker item whose observed revision this order is to become."""

    reference: TrackerItemReference

    def __post_init__(self) -> None:
        if not isinstance(self.reference, TrackerItemReference):
            raise TypeError(
                "a work item order value names its item through the contract"
            )


@dataclass(frozen=True, slots=True)
class ObservedWorkItemOrderValue:
    """The item this order named, as the start read it.

    It stays a work item all the way to the durable write rather than becoming
    anonymous bytes, because two things downstream depend on knowing what it
    is: the start refuses to store it under a schema other than the one the
    house owns, and a retry compares the item it names instead of bytes a
    second read could never reproduce.
    """

    revision: ObservedWorkItemRevision

    def __post_init__(self) -> None:
        if not isinstance(self.revision, ObservedWorkItemRevision):
            raise TypeError(
                "an observed work item order carries its revision through the contract"
            )


type AuthoredOrderValue = (
    InlineOrderValue | ArtifactOrderValue | ObservedWorkItemOrderValue
)
"""What a durable start carries: bytes, a published address, or an item it read."""

type StartOrderValue = AuthoredOrderValue | WorkItemOrderValue
"""What a start door accepts, before the reading a work item still needs."""
