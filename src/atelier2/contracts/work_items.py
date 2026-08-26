"""One tracker item as it stood at one read: exact bytes, their digest, provenance.

ADR 0010 §5 already owns how a platform object becomes something a run can
reproduce: the exact UTF-8 bytes the platform served as the object's body,
hashed as they are with nothing appended, carrying the object identity and the
read's change marker as provenance. That rule was written for the requirement
issue, and nothing in it is specific to one kind of item -- so this module is
that same rule generalised to whatever work item the connected tracker holds,
an issue or a change request, rather than a second snapshot idea beside
REQ-QUEUE-14's reference-only orchestration state.

The kinds are the neutral pair: a GitHub pull request and a GitLab merge
request are both `change_request`, and no platform word enters here. Which
spelling of a reference addresses which item stays the adapter's, exactly as
`TrackerItemReference` already says.

What stays out is as decided as what is here. Title, state, discussion, diff,
and linked items are either unbounded or platform-shaped, and none has a caller
in this slice; each joins with the caller that needs it, and the unbounded ones
arrive as artifacts addressed by hash rather than as a second byte budget
inside an order value (ADR 0010 decision 1, 2026-08-26 amendment).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.when import RecordedAt

MAXIMUM_WORK_ITEM_CHANGE_MARKER_CHARACTERS = 1_024


class WorkItemKind(StrEnum):
    """What a tracker item is, in words every tracker can be read into."""

    ISSUE = "issue"
    CHANGE_REQUEST = "change_request"


@dataclass(frozen=True)
class WorkItemChangeMarker:
    """The platform's own marker for the state this read saw.

    An entity tag or an update cursor, opaque here: a later read hands it back
    to ask whether anything changed (ADR 0010 §4), and a revision carries it so
    a reader can tell two reads of the same bytes apart from one read repeated.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a work item change marker must be text")
        if not 1 <= len(self.value) <= MAXIMUM_WORK_ITEM_CHANGE_MARKER_CHARACTERS:
            raise ValueError(
                "a work item change marker must contain 1 to "
                f"{MAXIMUM_WORK_ITEM_CHANGE_MARKER_CHARACTERS} characters"
            )


@dataclass(frozen=True)
class ObservedWorkItemRevision:
    """The bytes one tracker item served at one read, and what identifies them.

    The digest is derived rather than accepted, so no caller can hand in an
    identity these bytes do not have.
    """

    item: TrackerItemReference
    kind: WorkItemKind
    body: bytes
    change_marker: WorkItemChangeMarker
    observed_at: RecordedAt
    digest: Sha256Hash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.item, TrackerItemReference):
            raise TypeError(
                "an observed work item revision names its item through the contract"
            )
        if not isinstance(self.kind, WorkItemKind):
            raise TypeError("an observed work item revision carries a typed kind")
        if not isinstance(self.body, bytes):
            raise TypeError(
                "an observed work item revision carries the exact served bytes"
            )
        if not isinstance(self.change_marker, WorkItemChangeMarker):
            raise TypeError(
                "an observed work item revision carries its change marker "
                "through the contract"
            )
        if not isinstance(self.observed_at, RecordedAt):
            raise TypeError(
                "an observed work item revision carries its read time through "
                "the contract"
            )
        # Unframed on purpose: ADR 0010 §5 requires a digest a reader
        # re-derives from the object alone, which a framed preimage would make
        # Atelier-only.
        object.__setattr__(self, "digest", Sha256Hash.of(self.body))
