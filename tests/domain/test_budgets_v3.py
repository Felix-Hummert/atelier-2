"""What a published `budget_policy` revision has to say to bound an attempt.

Three questions meet here, and all three are answered before any process exists:
which published bytes are bounds this runtime can honour, which exact values one
budget is identified by, and what the seam a run start uses does with bytes that
are neither. The refusals are measured at that seam -- the same door that already
refuses a `schema` revision that is not a schema -- because a run that started
under an unreadable budget has already told its author it was bounded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from atelier2.application.resolve_references import resolve_declared_reference
from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.contracts.budgets_v3 import (
    MAXIMUM_BUDGET_REVISION_DOCUMENT_BYTES,
    BudgetField,
    BudgetRevisionAccepted,
    BudgetRevisionContent,
    BudgetRevisionRefusal,
    BudgetRevisionRefused,
    read_budget_revision_document,
)
from atelier2.contracts.revisions_v3 import (
    PublishedRevision,
    PublishedRevisionHash,
    RevisionKind,
)
from atelier2.contracts.run_configuration_v3 import (
    DeclaredReference,
    ReferenceRefusal,
    ReferenceRefusalReason,
    ReferenceSite,
    ResolvedReference,
)
from atelier2.contracts.workflows_v3 import VersionedReference
from atelier2.ports.published_revisions import (
    PublishedRevisionFound,
    PublishedRevisionMissing,
    PublishRevisionResult,
    ResolvePublishedRevisionResult,
)

NODE = "implement"


def budget_document(**bounds: object) -> bytes:
    """Exactly the bounds one author wrote, as the bytes they would publish."""
    return json.dumps(bounds).encode("utf-8")


THE_SMALLEST_HONEST_BUDGET = budget_document(attempt_deadline_seconds=900)
EVERY_BOUND = budget_document(
    attempt_deadline_seconds=900,
    maximum_assistant_turns=8,
    reported_input_token_threshold=200_000,
    reported_output_token_threshold=64_000,
)


@dataclass(frozen=True)
class OneRevisionRegistry:
    """A registry carrying exactly the revision under test, and nothing else."""

    revision: PublishedRevision

    def publish_revision(self, revision: PublishedRevision) -> PublishRevisionResult:
        raise AssertionError("resolution never publishes")

    def resolve(
        self, kind: RevisionKind, revision_hash: PublishedRevisionHash
    ) -> ResolvePublishedRevisionResult:
        if kind is self.revision.kind and revision_hash == self.revision.revision_hash:
            return PublishedRevisionFound(self.revision)
        return PublishedRevisionMissing()


def resolution_of(document: bytes) -> ResolvedReference | ReferenceRefusal:
    """What the seam a run start uses answers for these exact published bytes."""
    revision = PublishedRevision(RevisionKind.BUDGET_POLICY, document)
    declared = DeclaredReference(
        ReferenceSite("budget", NODE, None),
        RevisionKind.BUDGET_POLICY,
        VersionedReference(ref="build_budget", revision=revision.revision_hash.value),
    )
    return resolve_declared_reference(declared, OneRevisionRegistry(revision))


@pytest.mark.parametrize(
    ("label", "document"),
    (
        ("only the deadline every budget must bound", THE_SMALLEST_HONEST_BUDGET),
        ("both hard limits and both reported thresholds", EVERY_BOUND),
    ),
    ids=("deadline-only", "every-bound"),
)
def test_a_published_budget_bounds_an_attempt_and_resolves(
    label: str, document: bytes
) -> None:
    del label
    verdict = read_budget_revision_document(document)

    assert isinstance(verdict, BudgetRevisionAccepted)
    assert verdict.content.attempt_deadline_seconds == 900
    assert isinstance(resolution_of(document), ResolvedReference)


def test_a_budget_without_a_turn_limit_publishes_because_the_executor_decides() -> None:
    """`maximum_assistant_turns` is optional content and a run-start requirement.

    ADR 0008 leaves whether a turn limit is required to the executor revision
    that would enforce it, so publication cannot demand it: only the binding
    knows which executor was selected.
    """
    verdict = read_budget_revision_document(THE_SMALLEST_HONEST_BUDGET)

    assert isinstance(verdict, BudgetRevisionAccepted)
    assert verdict.content.maximum_assistant_turns is None


NOT_A_BUDGET_THIS_RUNTIME_HONOURS: tuple[
    tuple[str, bytes, BudgetRevisionRefusal], ...
] = (
    (
        "a budget bounding nothing at all",
        b"{}",
        BudgetRevisionRefusal.MISSING_ATTEMPT_DEADLINE,
    ),
    (
        "thresholds without the deadline every attempt is bounded by",
        budget_document(reported_output_token_threshold=64_000),
        BudgetRevisionRefusal.MISSING_ATTEMPT_DEADLINE,
    ),
    (
        "a money field no charge meter measures",
        budget_document(attempt_deadline_seconds=900, cost_ceiling=5),
        BudgetRevisionRefusal.UNKNOWN_FIELD,
    ),
    (
        "a run budget this decision never created",
        budget_document(attempt_deadline_seconds=900, run_budget=3),
        BudgetRevisionRefusal.UNKNOWN_FIELD,
    ),
    (
        "catalog identity embedded in the content",
        budget_document(attempt_deadline_seconds=900, revision_number=2),
        BudgetRevisionRefusal.UNKNOWN_FIELD,
    ),
    (
        "a deadline of zero seconds",
        budget_document(attempt_deadline_seconds=0),
        BudgetRevisionRefusal.VALUE_NOT_A_POSITIVE_INT64,
    ),
    (
        "a negative deadline",
        budget_document(attempt_deadline_seconds=-1),
        BudgetRevisionRefusal.VALUE_NOT_A_POSITIVE_INT64,
    ),
    (
        "a deadline past signed int64",
        budget_document(attempt_deadline_seconds=MAXIMUM_SIGNED_INT64 + 1),
        BudgetRevisionRefusal.VALUE_NOT_A_POSITIVE_INT64,
    ),
    (
        "a fractional deadline",
        budget_document(attempt_deadline_seconds=900.5),
        BudgetRevisionRefusal.VALUE_NOT_A_POSITIVE_INT64,
    ),
    (
        "a whole number written as a fraction",
        b'{"attempt_deadline_seconds": 900.0}',
        BudgetRevisionRefusal.VALUE_NOT_A_POSITIVE_INT64,
    ),
    (
        "a deadline written as text",
        budget_document(attempt_deadline_seconds="900"),
        BudgetRevisionRefusal.VALUE_NOT_A_POSITIVE_INT64,
    ),
    (
        "a bound written as a boolean",
        budget_document(attempt_deadline_seconds=True),
        BudgetRevisionRefusal.VALUE_NOT_A_POSITIVE_INT64,
    ),
    (
        "an optional bound written as nothing",
        budget_document(attempt_deadline_seconds=900, maximum_assistant_turns=None),
        BudgetRevisionRefusal.VALUE_NOT_A_POSITIVE_INT64,
    ),
    (
        "a threshold of zero tokens",
        budget_document(attempt_deadline_seconds=900, reported_input_token_threshold=0),
        BudgetRevisionRefusal.VALUE_NOT_A_POSITIVE_INT64,
    ),
    (
        "prose",
        b"what one build may spend",
        BudgetRevisionRefusal.NOT_A_BUDGET_OBJECT,
    ),
    (
        "a budget that is not an object",
        b"[900]",
        BudgetRevisionRefusal.NOT_A_BUDGET_OBJECT,
    ),
    ("bytes that are not text", b"\xff\xfe", BudgetRevisionRefusal.DOCUMENT_NOT_UTF8),
)


@pytest.mark.proves("a-published-budget-bounds-an-attempt-or-is-refused-by-name")
@pytest.mark.parametrize(
    ("label", "document", "expected"),
    NOT_A_BUDGET_THIS_RUNTIME_HONOURS,
    ids=[label for label, _, _ in NOT_A_BUDGET_THIS_RUNTIME_HONOURS],
)
def test_published_bytes_that_bound_nothing_refuse_where_they_were_pinned(
    label: str, document: bytes, expected: BudgetRevisionRefusal
) -> None:
    del label
    verdict = read_budget_revision_document(document)

    assert isinstance(verdict, BudgetRevisionRefused)
    assert verdict.reason is expected

    refusal = resolution_of(document)

    assert isinstance(refusal, ReferenceRefusal)
    assert refusal.reason is ReferenceRefusalReason.UNUSABLE_BUDGET_DOCUMENT
    assert expected.value in str(refusal)
    assert NODE in str(refusal)


@pytest.mark.proves("a-published-budget-bounds-an-attempt-or-is-refused-by-name")
def test_a_budget_document_larger_than_its_bound_is_refused_before_it_is_read() -> None:
    padded = budget_document(
        attempt_deadline_seconds=900,
        **{"x" * MAXIMUM_BUDGET_REVISION_DOCUMENT_BYTES: 1},
    )

    verdict = read_budget_revision_document(padded)

    assert isinstance(verdict, BudgetRevisionRefused)
    assert verdict.reason is BudgetRevisionRefusal.DOCUMENT_TOO_LARGE


@pytest.mark.proves("a-published-budget-bounds-an-attempt-or-is-refused-by-name")
def test_the_named_bound_is_the_one_the_refusal_names() -> None:
    """An author fixing a budget is told which bound was wrong, not that one was."""
    verdict = read_budget_revision_document(
        budget_document(attempt_deadline_seconds=900, reported_output_token_threshold=0)
    )

    assert isinstance(verdict, BudgetRevisionRefused)
    assert BudgetField.REPORTED_OUTPUT_TOKEN_THRESHOLD.value in verdict.detail
    assert BudgetField.ATTEMPT_DEADLINE_SECONDS.value not in verdict.detail


ONE_CHANGED_BOUND: tuple[tuple[str, dict[str, int | None]], ...] = (
    ("the attempt deadline", {"attempt_deadline_seconds": 901}),
    ("the turn limit", {"maximum_assistant_turns": 9}),
    ("the reported input threshold", {"reported_input_token_threshold": 200_001}),
    ("the reported output threshold", {"reported_output_token_threshold": 64_001}),
    ("a hard limit withdrawn", {"maximum_assistant_turns": None}),
    ("a reported threshold withdrawn", {"reported_input_token_threshold": None}),
)


@pytest.mark.proves("a-changed-budget-bound-is-a-different-budget-revision")
@pytest.mark.parametrize(
    ("label", "change"),
    ONE_CHANGED_BOUND,
    ids=[label for label, _ in ONE_CHANGED_BOUND],
)
def test_one_changed_bound_is_a_different_budget_revision(
    label: str, change: dict[str, int | None]
) -> None:
    del label
    every_bound = BudgetRevisionContent(900, 8, 200_000, 64_000)
    fields = {
        budget_field.value: getattr(every_bound, budget_field.value)
        for budget_field in BudgetField
    }

    assert (
        BudgetRevisionContent(**(fields | change)).content_hash  # type: ignore[arg-type]
        != every_bound.content_hash
    )


@pytest.mark.proves("a-changed-budget-bound-is-a-different-budget-revision")
def test_an_absent_optional_bound_is_not_a_bound_of_zero_or_of_any_value() -> None:
    """Which position a bound was written in is part of what a budget says."""
    withheld = BudgetRevisionContent(900)

    assert withheld.content_hash != BudgetRevisionContent(900, 1).content_hash
    assert (
        BudgetRevisionContent(900, None, 1).content_hash
        != BudgetRevisionContent(900, 1).content_hash
    )


@pytest.mark.proves("a-changed-budget-bound-is-a-different-budget-revision")
def test_the_budget_content_hash_vectors_are_frozen() -> None:
    """The encoding ADR 0008 wrote down, pinned so a later reader cannot drift.

    Distinguishing four bounds needs no particular encoding, so a rewrite that
    packed an absent optional as eight zero bytes would keep every other test
    here green and silently rewrite every stored budget identity. These two
    vectors are what makes that a red test: the deadline-only shape carries three
    zero-length positions, the full shape four eight-byte ones.
    """
    assert BudgetRevisionContent(900).content_hash.value == (
        "5f39f571e8d51472dcd4f8607c094ea31b035e8ae83d2021b00f6c2c00ed9bc1"
    )
    assert BudgetRevisionContent(900, 8, 200_000, 64_000).content_hash.value == (
        "8c9b6719f7ae000587aec4e8228e422347dc1c185a2a045ff29d046a627e7272"
    )


@pytest.mark.proves("a-changed-budget-bound-is-a-different-budget-revision")
def test_the_same_bounds_serialised_differently_are_one_budget_content() -> None:
    """Content identity is the four values; document identity is the exact bytes."""
    spaced = b'{"attempt_deadline_seconds":  900 }'
    reordered = budget_document(maximum_assistant_turns=8, attempt_deadline_seconds=900)
    turn_bounded = budget_document(
        attempt_deadline_seconds=900, maximum_assistant_turns=8
    )

    read = read_budget_revision_document(spaced)
    plain = read_budget_revision_document(THE_SMALLEST_HONEST_BUDGET)
    assert isinstance(read, BudgetRevisionAccepted)
    assert isinstance(plain, BudgetRevisionAccepted)
    assert read.content.content_hash == plain.content.content_hash
    assert (
        PublishedRevision(RevisionKind.BUDGET_POLICY, spaced).revision_hash
        != PublishedRevision(
            RevisionKind.BUDGET_POLICY, THE_SMALLEST_HONEST_BUDGET
        ).revision_hash
    )

    ordered = read_budget_revision_document(reordered)
    authored = read_budget_revision_document(turn_bounded)
    assert isinstance(ordered, BudgetRevisionAccepted)
    assert isinstance(authored, BudgetRevisionAccepted)
    assert ordered.content.content_hash == authored.content.content_hash


def test_a_budget_content_refuses_a_bound_no_reader_could_have_produced() -> None:
    """The type is the invariant, so no second caller can mint an unbounded budget."""
    with pytest.raises(ValueError, match="attempt_deadline_seconds"):
        BudgetRevisionContent(0)
    with pytest.raises(ValueError, match="maximum_assistant_turns"):
        BudgetRevisionContent(900, -1)


def test_an_unpinnable_budget_reference_is_refused_as_an_unpublished_revision() -> None:
    declared = DeclaredReference(
        ReferenceSite("budget", NODE, None),
        RevisionKind.BUDGET_POLICY,
        VersionedReference(ref="build_budget", revision="f0" * 32),
    )

    refusal = resolve_declared_reference(
        declared,
        OneRevisionRegistry(
            PublishedRevision(RevisionKind.BUDGET_POLICY, THE_SMALLEST_HONEST_BUDGET)
        ),
    )

    assert isinstance(refusal, ReferenceRefusal)
    assert refusal.reason is ReferenceRefusalReason.UNPUBLISHED_REVISION
