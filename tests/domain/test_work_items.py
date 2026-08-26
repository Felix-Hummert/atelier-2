"""One tracker item as it stood at one read: its bytes, and what they identify."""

from __future__ import annotations

import pytest

from atelier2.contracts.queue_projection import TrackerItemReference
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.work_items import (
    ObservedWorkItemRevision,
    WorkItemChangeMarker,
    WorkItemKind,
)

_ITEM = TrackerItemReference("gh:712")
_OBSERVED_AT = RecordedAt("2026-08-26T09:15:00Z")


def revision(
    body: bytes = b"work item body",
    change_marker: str = 'W/"5f2a"',
) -> ObservedWorkItemRevision:
    return ObservedWorkItemRevision(
        _ITEM,
        WorkItemKind.ISSUE,
        body,
        WorkItemChangeMarker(change_marker),
        _OBSERVED_AT,
    )


def test_the_digest_is_the_plain_sha256_a_reader_recomputes_from_the_bytes() -> None:
    """`printf 'work item body' | sha256sum` answers exactly this digest.

    The point of ADR 0010 §5's rule is that a human, or a second tool, can
    re-derive the identity from the object alone -- so the digest is the
    unframed SHA-256 over the served bytes, never an Atelier-only preimage.
    """

    assert revision().digest.value == (
        "8f17afb3a2b7301dc70f9b25e738e09243c53a957e8a02e96a81d97589bd049f"
    )


def test_appending_a_newline_to_the_body_is_a_different_revision() -> None:
    assert revision(b"work item body\n").digest != revision(b"work item body").digest


def test_the_same_bytes_read_twice_carry_the_same_digest() -> None:
    assert revision(change_marker='W/"5f2a"').digest == (
        revision(change_marker='W/"b118"').digest
    )


def test_a_body_that_is_not_the_served_bytes_is_refused() -> None:
    with pytest.raises(TypeError):
        ObservedWorkItemRevision(
            _ITEM,
            WorkItemKind.ISSUE,
            "text, not the served bytes",  # type: ignore[arg-type]
            WorkItemChangeMarker('W/"5f2a"'),
            _OBSERVED_AT,
        )


@pytest.mark.parametrize("value", ["", "x" * 1_025])
def test_a_change_marker_outside_its_bound_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        WorkItemChangeMarker(value)
