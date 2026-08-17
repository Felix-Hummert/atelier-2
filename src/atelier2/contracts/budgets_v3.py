"""What a document published as a `budget_policy` revision bounds, and what it never claims.

ADR 0006 lets an Agent or Subworkflow node pin a versioned `budget` reference and
deliberately leaves its units to ADR 0008. That decision is this module's owner:
`budget-revision/v1` is immutable content under the existing
`contracts.hashing.frame` owner, and its whole point is that a reader can tell a
bound the runtime enforces from a number a provider reports afterwards.

Bytes published under the kind `budget_policy` are a budget only because someone
called them one, so the reading that turns them into one is here -- a pure
function over bytes, exactly as a schema and a tool grant are read, and asked by
the two places that must not disagree: the door that publishes a revision, and
the reference resolution that binds a run to it.

Two distinctions are the reason this module exists at all.

**A hard limit is not a reported threshold.** `attempt_deadline_seconds` and
`maximum_assistant_turns` are enforced before or during an attempt;
`reported_input_token_threshold` and `reported_output_token_threshold` are judged
only once a provider reports usage the attempt already spent. The field names
carry that difference so no surface can present a post-hoc number as a maximum,
cap, or ceiling.

**Content identity is not document identity.** `PublishedRevisionHash` is the
identity of exact bytes, and it is what the registry stores and a node pins.
`BudgetRevisionHash` is the identity of the four values those bytes decode to,
which is what a request binds when it says which bounds a run was given. They
answer different questions: reserialising the same four values keeps the content
identity and mints a new document identity, and catalog lineage, display name and
revision position -- metadata owned by the catalog -- enter neither.

Money is absent by decision, not by omission: an authentication mode selects a
credential path and creates no charge meter, so no currency field can be honest
here until an exact charge or billing-ledger owner measures one.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.contracts.hashing import Sha256Hash, frame

MAXIMUM_BUDGET_REVISION_DOCUMENT_BYTES = 4_096
"""How large a budget document may be. It names four integers; nothing needs more."""

MINIMUM_BUDGET_VALUE = 1
"""The smallest honest bound. Zero would be a budget that forbids the work it bounds."""


class BudgetField(StrEnum):
    """The closed set of fields `budget-revision/v1` content carries.

    The order is the order of the frame positions the content hash takes, so the
    persisted key and the hashed position have one owner and cannot drift apart.
    """

    ATTEMPT_DEADLINE_SECONDS = "attempt_deadline_seconds"
    MAXIMUM_ASSISTANT_TURNS = "maximum_assistant_turns"
    REPORTED_INPUT_TOKEN_THRESHOLD = "reported_input_token_threshold"
    REPORTED_OUTPUT_TOKEN_THRESHOLD = "reported_output_token_threshold"


class BudgetRevisionRefusal(StrEnum):
    """Every named way published bytes fail to be a budget this runtime honours.

    Hyphenated like the schema profile's refusals rather than the underscored
    resolution vocabulary, because these tokens reach an author as the problem
    code of the publication door and that URN admits no underscore.
    """

    DOCUMENT_TOO_LARGE = "document-too-large"
    DOCUMENT_NOT_UTF8 = "document-not-utf8"
    NOT_A_BUDGET_OBJECT = "not-a-budget-object"
    UNKNOWN_FIELD = "unknown-field"
    MISSING_ATTEMPT_DEADLINE = "missing-attempt-deadline"
    VALUE_NOT_A_POSITIVE_INT64 = "value-not-a-positive-int64"


class BudgetRevisionHash(Sha256Hash):
    """The immutable content identity of one `budget-revision/v1`."""


@dataclass(frozen=True)
class BudgetRevisionContent:
    """The four bounds one budget revision authored, and their content identity.

    Constructing this value is the validation: a budget that reached a caller is
    a budget whose every present field is a bound the runtime could enforce or
    judge, so no reader downstream re-checks a range that cannot be here.
    """

    attempt_deadline_seconds: int
    maximum_assistant_turns: int | None = None
    reported_input_token_threshold: int | None = None
    reported_output_token_threshold: int | None = None
    content_hash: BudgetRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        for budget_field in BudgetField:
            value = getattr(self, budget_field.value)
            if (
                budget_field is BudgetField.ATTEMPT_DEADLINE_SECONDS
                or value is not None
            ):
                _positive_int64(budget_field, value)
        object.__setattr__(self, "content_hash", self._hash())

    def _hash(self) -> BudgetRevisionHash:
        return BudgetRevisionHash.of(
            frame(
                "budget-revision/v1",
                *(
                    _encoded(getattr(self, budget_field.value))
                    for budget_field in BudgetField
                ),
            )
        )


@dataclass(frozen=True, slots=True)
class BudgetRevisionAccepted:
    """These bytes bound an attempt exactly this much."""

    content: BudgetRevisionContent


@dataclass(frozen=True, slots=True)
class BudgetRevisionRefused:
    """These bytes are not a budget this runtime can bound an attempt with, and why."""

    reason: BudgetRevisionRefusal
    detail: str = ""

    def __str__(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"{self.reason.value}{suffix}"


type BudgetRevisionVerdict = BudgetRevisionAccepted | BudgetRevisionRefused


def read_budget_revision_document(document: bytes) -> BudgetRevisionVerdict:
    """Whether these exact published bytes are bounds this runtime can honour."""
    if len(document) > MAXIMUM_BUDGET_REVISION_DOCUMENT_BYTES:
        return BudgetRevisionRefused(
            BudgetRevisionRefusal.DOCUMENT_TOO_LARGE,
            f"{len(document)} bytes exceeds {MAXIMUM_BUDGET_REVISION_DOCUMENT_BYTES}",
        )
    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as broken:
        return BudgetRevisionRefused(
            BudgetRevisionRefusal.DOCUMENT_NOT_UTF8, broken.reason
        )
    try:
        decoded = json.loads(text)
    except ValueError as broken:
        return BudgetRevisionRefused(
            BudgetRevisionRefusal.NOT_A_BUDGET_OBJECT, str(broken)
        )
    if not isinstance(decoded, dict):
        return BudgetRevisionRefused(
            BudgetRevisionRefusal.NOT_A_BUDGET_OBJECT,
            f"a budget revision is an object, not {type(decoded).__name__}",
        )
    named = {budget_field.value for budget_field in BudgetField}
    unknown = sorted(name for name in decoded if name not in named)
    if unknown:
        return BudgetRevisionRefused(
            BudgetRevisionRefusal.UNKNOWN_FIELD,
            f"a budget revision names {', '.join(sorted(named))} and nothing else, "
            f"not {', '.join(unknown)}",
        )
    if BudgetField.ATTEMPT_DEADLINE_SECONDS.value not in decoded:
        return BudgetRevisionRefused(
            BudgetRevisionRefusal.MISSING_ATTEMPT_DEADLINE,
            "every budget revision bounds how long one attempt may run, under "
            f"{BudgetField.ATTEMPT_DEADLINE_SECONDS.value}",
        )

    def authored(budget_field: BudgetField) -> int | None:
        """An absent optional, or whatever was written there -- never a substitute."""
        if budget_field.value not in decoded:
            return None
        return _positive_int64(budget_field, decoded[budget_field.value])

    try:
        content = BudgetRevisionContent(
            _positive_int64(
                BudgetField.ATTEMPT_DEADLINE_SECONDS,
                decoded[BudgetField.ATTEMPT_DEADLINE_SECONDS.value],
            ),
            authored(BudgetField.MAXIMUM_ASSISTANT_TURNS),
            authored(BudgetField.REPORTED_INPUT_TOKEN_THRESHOLD),
            authored(BudgetField.REPORTED_OUTPUT_TOKEN_THRESHOLD),
        )
    except ValueError as refused:
        return BudgetRevisionRefused(
            BudgetRevisionRefusal.VALUE_NOT_A_POSITIVE_INT64, str(refused)
        )
    return BudgetRevisionAccepted(content)


def _positive_int64(budget_field: BudgetField, value: object) -> int:
    """A bound is a whole positive count, or it is no bound at all.

    `type(...) is not int` rather than `isinstance`: a boolean is an integer in
    Python, and `true` as a deadline is an author's mistake, not one second. An
    explicit `null` arrives here too, and is refused the same way -- an optional
    bound is written or absent, and "written as nothing" is neither.
    """
    if type(value) is not int:
        raise ValueError(
            f"{budget_field.value} is a positive signed 64-bit integer, "
            f"not {type(value).__name__}"
        )
    if not MINIMUM_BUDGET_VALUE <= value <= MAXIMUM_SIGNED_INT64:
        raise ValueError(
            f"{budget_field.value} is between {MINIMUM_BUDGET_VALUE} and "
            f"{MAXIMUM_SIGNED_INT64}, not {value}"
        )
    return value


def _encoded(value: int | None) -> bytes:
    """One frame position: the fixed-width bound, or the absence of one.

    A zero-length field is how an absent optional occupies its position, because
    the range starts at one and no encoding of a bound can be empty. Absent is
    therefore not zero, and no two different budgets share a content identity.
    """
    return b"" if value is None else struct.pack(">q", value)
