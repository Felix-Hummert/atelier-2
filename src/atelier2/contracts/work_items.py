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

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from atelier2.contracts.hashing import SHA256_HEX_DIGEST, Sha256Hash
from atelier2.contracts.queue_projection import (
    MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
    TrackerItemReference,
)
from atelier2.contracts.schemas_v3 import SUPPORTED_DIALECT
from atelier2.contracts.when import RECORDED_AT_PATTERN, RecordedAt

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
        try:
            self.body.decode("utf-8")
        except UnicodeDecodeError:
            # ADR 0010 §5's canonical rule is about the UTF-8 bytes a platform
            # served, and a run reads them back as text; bytes that are not
            # text are not a revision this contract can carry.
            raise ValueError(
                "an observed work item revision carries UTF-8 body bytes"
            ) from None
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


def work_item_order_document(revision: ObservedWorkItemRevision) -> bytes:
    """The exact bytes one work-item order carries into the run that reads it.

    A run's order is material, and material is bytes: this is the one
    serialization of an observed revision, so the value a run stores, the value
    an agent reads, and the value `WORK_ITEM_ORDER_SCHEMA_DOCUMENT` describes
    are the same thing. Keys are sorted and separators are tight, so the same
    read always produces the same bytes and therefore the same value hash.
    """

    return json.dumps(
        {
            "body": revision.body.decode("utf-8"),
            "change_marker": revision.change_marker.value,
            "digest": revision.digest.value,
            "kind": revision.kind.value,
            "observed_at": revision.observed_at.value,
            "reference": revision.item.value,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


_WORK_ITEM_ORDER_SCHEMA: Final = {
    "$schema": SUPPORTED_DIALECT,
    "title": "work item",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "body",
        "change_marker",
        "digest",
        "kind",
        "observed_at",
        "reference",
    ],
    "properties": {
        "body": {"type": "string"},
        "change_marker": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAXIMUM_WORK_ITEM_CHANGE_MARKER_CHARACTERS,
        },
        "digest": {"type": "string", "pattern": f"^{SHA256_HEX_DIGEST.pattern}$"},
        "kind": {"type": "string", "enum": [kind.value for kind in WorkItemKind]},
        "observed_at": {"type": "string", "pattern": RECORDED_AT_PATTERN},
        "reference": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAXIMUM_TRACKER_ITEM_REFERENCE_CHARACTERS,
        },
    },
}

WORK_ITEM_ORDER_SCHEMA_DOCUMENT: Final = json.dumps(
    _WORK_ITEM_ORDER_SCHEMA, sort_keys=True, separators=(",", ":")
).encode("utf-8")
"""The schema a workflow pins to declare an order as a tracker work item.

A workflow author does not invent a shape for what the adapter reads: they pin
this document's published revision and get the neutral kinds, the digest and
the change marker with it. Restricting a workflow to one kind is that author's
own schema built on this one, not a second grammar in the document.
"""
