"""The typed word a piece of work says about itself, and the contract it says it under.

A run's shape has always been decided before anything ran: the edges an author
wrote are the edges the engine walks. This is where that changes. A verdict is
the one thing a node produces that the engine *reads back into the control
flow* -- it decides which edge runs next -- so it cannot be prose, and it cannot
be a word an agent invents. It is a closed vocabulary with one owner, and the
answer that carries it is judged by a schema derived from that same vocabulary
before anything reads it.

**Why the schema is derived rather than written.** Two spellings of one closed
set drift the moment a token moves, and the drift would surface as a run whose
loop turns on a word the document's own schema never admitted. The published
document below is generated from `Verdict`, so the vocabulary and the contract
an answer is judged against cannot disagree.

**Why the document pins this revision by hash.** A published revision is
addressed by the hash of its own bytes, so a node whose output pins this hash
has pinned exactly these tokens. That is what makes reading a verdict total: by
the time the engine reads one, the bytes have already passed the schema seam
every V3 output passes, under a schema this product owns. A value that carries
no verdict therefore dies as a refused output, in the words of the seam that
has always judged outputs, rather than needing an ending of its own.

**Why the set is this small.** Two tokens are what this build can honour: the
work is done, or it needs another round. The third truth an agent owes -- its
own named refusal, "the order is unclear because X" -- is not here, because
nothing downstream can carry it yet: a run ends `FAILED` only under an attempt
failure code, and that column's closed value list is a store contract. Naming
the token here while the machine could not honour it would put a word in an
author's hands that the engine would silently drop.
"""

from __future__ import annotations

import json
from enum import StrEnum

from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind


class Verdict(StrEnum):
    """What a finished piece of work says about itself, in the words of one owner.

    The same vocabulary is meant to answer for a node today and for a whole run
    later, because the question is the same one at both heights: is this done,
    or does it go round again. Kept as one owner rather than one per edge, so
    the second reader inherits the tokens instead of inventing a dialect.
    """

    ACCEPTED = "accepted"
    REVISE = "revise"


VERDICT_FIELD = "verdict"
"""The one member of a verdict answer, named once for the schema and the reader."""


def _verdict_answer_schema_document() -> bytes:
    """The published schema an answer carrying a verdict is judged by.

    Derived from the vocabulary above, in the canonical JSON form the registry
    stores, because the hash of these exact bytes is the identity a document
    pins. `additionalProperties` is closed for the same reason every authored
    form in this product is closed: an answer carrying something nobody reads
    is refused rather than accepted and quietly dropped.
    """
    return json.dumps(
        {
            "type": "object",
            "properties": {
                VERDICT_FIELD: {"enum": [verdict.value for verdict in Verdict]}
            },
            "required": [VERDICT_FIELD],
            "additionalProperties": False,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


VERDICT_ANSWER_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA, _verdict_answer_schema_document()
)
"""The registry revision a node must pin to have its verdict read.

It is a published revision like any other -- the registry must carry it before a
run can judge an answer against it -- and it is the product's own, so a document
that pins it has agreed to this vocabulary rather than to bytes of its own.
"""


class VerdictUnreadable(ValueError):
    """Exact answer bytes carry no verdict this product owns.

    Reaching this from a run is the product contradicting itself: the answer was
    judged by the schema above before anything read it, so bytes that pass the
    schema and fail here mean the stored schema and this vocabulary have come
    apart. It is raised rather than answered for exactly that reason -- there is
    no honest continuation to choose.
    """


def read_verdict(answer: bytes) -> Verdict:
    """The verdict these exact answer bytes carry.

    Only the one member is read. What an answer may otherwise contain is the
    published schema's decision, not a second rule here, so this reader cannot
    grow a stricter or looser opinion than the contract the document pinned.
    """
    try:
        decoded = json.loads(answer)
    except (json.JSONDecodeError, UnicodeDecodeError) as broken:
        raise VerdictUnreadable("a verdict answer is one JSON document") from broken
    if not isinstance(decoded, dict):
        raise VerdictUnreadable(
            f"a verdict answer is a JSON object, not {type(decoded).__name__}"
        )
    try:
        return Verdict(decoded[VERDICT_FIELD])
    except KeyError as absent:
        raise VerdictUnreadable(f"a verdict answer names {VERDICT_FIELD!r}") from absent
    except ValueError as unknown:
        raise VerdictUnreadable(
            f"{decoded[VERDICT_FIELD]!r} is no verdict this product owns"
        ) from unknown
