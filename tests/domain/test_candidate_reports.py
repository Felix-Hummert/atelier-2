"""What a build node's report value carries once the atelier writes its diff in."""

from __future__ import annotations

import json

import pytest

from atelier2.contracts.candidate_reports import (
    CANDIDATE_DIFF_PROPERTY,
    CANDIDATE_DIFF_TRUNCATION_MARKER,
    CandidateReportDoesNotFit,
    ReadPatch,
    patch_safe_to_show,
    report_carrying_candidate_diff,
    schema_declares_candidate_diff,
)
from atelier2.contracts.secret_redaction import (
    MAXIMUM_PRIVATE_KEY_INTERIOR_CHARACTERS,
    REDACTION_MARKER,
)
from atelier2.ports.candidate_store import (
    CANDIDATE_DIFF_READ_BYTES,
    MAXIMUM_CANDIDATE_DIFF_BYTES,
)
from tests.scenarios.credentials import assembled

SPACIOUS = 1_000_000
"""A bound no report in these cases can reach, so only truncation cases cut."""


def report(**named: object) -> bytes:
    return json.dumps(
        {"summary": "wrote the line", "changed_paths": [], **named}
    ).encode()


def compactly(**named: object) -> bytes:
    """A report already encoded the way this owner encodes what it writes back."""

    return json.dumps(
        {"changed_paths": [], **named}, ensure_ascii=False, separators=(",", ":")
    ).encode()


def carried(value: bytes) -> object:
    decoded = json.loads(value)
    assert isinstance(decoded, dict)
    return decoded.get(CANDIDATE_DIFF_PROPERTY)


def test_the_report_carries_the_diff_the_runtime_read() -> None:
    value = report_carrying_candidate_diff(report(), "diff --git a/x b/x\n", SPACIOUS)

    assert carried(value) == "diff --git a/x b/x\n"
    assert json.loads(value)["summary"] == "wrote the line"


def test_a_report_carries_no_diff_where_the_runtime_read_none() -> None:
    assert carried(report_carrying_candidate_diff(report(), None, SPACIOUS)) is None


@pytest.mark.parametrize("runtime_diff", [None, "the atelier's own patch\n"])
def test_a_diff_the_provider_wrote_itself_never_survives(
    runtime_diff: str | None,
) -> None:
    """The property is the atelier's word, so a builder cannot answer with one."""

    invented = report(candidate_diff="I promise I changed nothing dangerous")

    value = report_carrying_candidate_diff(invented, runtime_diff, SPACIOUS)

    assert carried(value) == runtime_diff


def test_a_diff_too_long_for_one_node_value_is_cut_and_says_so() -> None:
    bound = 2_048

    value = report_carrying_candidate_diff(report(), "+line\n" * 1_000, bound)

    kept = carried(value)
    assert isinstance(kept, str)
    assert len(value) <= bound
    assert kept.startswith("+line\n")
    assert kept.endswith(CANDIDATE_DIFF_TRUNCATION_MARKER)
    assert "+line\n" * 100 in kept


def test_a_bound_leaving_only_the_marker_still_says_the_patch_was_cut() -> None:
    """The marker is what a reader is owed even where no patch line fits."""

    original = report()
    marked = len(original) + len(CANDIDATE_DIFF_TRUNCATION_MARKER) + 40

    value = report_carrying_candidate_diff(original, "+line\n" * 100, marked)

    assert carried(value) == CANDIDATE_DIFF_TRUNCATION_MARKER
    assert len(value) <= marked


def test_a_report_with_no_room_left_refuses_rather_than_dropping_the_patch() -> None:
    """A value silently missing the property would read as a change of nothing."""

    with pytest.raises(CandidateReportDoesNotFit):
        report_carrying_candidate_diff(report(), "+line\n" * 100, len(report()) + 1)


def test_a_report_that_exactly_fills_the_bound_is_written_as_it_stands() -> None:
    """The bound is measured on the compact encoding this owner actually writes."""

    original = compactly(summary="wrote the line")

    value = report_carrying_candidate_diff(original, None, len(original))

    assert value == original


def test_a_report_the_bound_cannot_hold_at_all_refuses() -> None:
    """One byte under its own size is a value no produced-value route carries."""

    original = compactly(summary="the whole answer")

    with pytest.raises(CandidateReportDoesNotFit):
        report_carrying_candidate_diff(original, None, len(original) - 1)


def test_a_value_that_is_no_object_is_handed_back_untouched() -> None:
    assert report_carrying_candidate_diff(b'"an answer"', "a patch", SPACIOUS) == (
        b'"an answer"'
    )


@pytest.mark.parametrize(
    ("document", "declared"),
    [
        (
            b'{"type": "object", "properties": {"candidate_diff": {"type": "string"}}}',
            True,
        ),
        (b'{"type": "object", "properties": {"summary": {"type": "string"}}}', False),
        (b'{"type": "string"}', False),
        (b"true", False),
    ],
)
def test_only_a_schema_naming_the_property_makes_room_for_it(
    document: bytes, declared: bool
) -> None:
    assert schema_declares_candidate_diff(document) is declared


LONGEST_KEY_LABEL = "A VERY LONG LABEL FOR ONE KEYXYZ"
"""The widest label the armoured-block shape reads between `BEGIN` and `KEY`."""

KEY_BLOCK_OPENS = assembled("-----BEGIN ", LONGEST_KEY_LABEL, "PRIVATE KEY", "-----")
KEY_BLOCK_CLOSES = assembled("-----END ", LONGEST_KEY_LABEL, "PRIVATE KEY", "-----")
KEY_MATERIAL_CHARACTER = "K"
LONGEST_RECOGNISABLE_KEY_BLOCK = (
    KEY_BLOCK_OPENS
    + KEY_MATERIAL_CHARACTER * MAXIMUM_PRIVATE_KEY_INTERIOR_CHARACTERS
    + KEY_BLOCK_CLOSES
)
"""The widest block a redactor can still recognise: markers, label and material."""

WIDE_KEY_MATERIAL_CHARACTER = "\N{ROCKET}"
"""Key material every character of which spends four bytes of a byte-counted read."""

KEY_BLOCK_IN_WIDE_MATERIAL = (
    KEY_BLOCK_OPENS
    + WIDE_KEY_MATERIAL_CHARACTER * MAXIMUM_PRIVATE_KEY_INTERIOR_CHARACTERS
    + KEY_BLOCK_CLOSES
)
"""A block the redactor may read whole, and no byte-counted store ever reads whole."""

PADDING_CHARACTER = "a"


def read_as_the_store_reads_it(text: str) -> ReadPatch:
    """This text as a candidate store hands it on: its own bound, and whether it cut."""

    whole = text.encode("utf-8")
    return ReadPatch(
        whole[:CANDIDATE_DIFF_READ_BYTES], len(whole) > CANDIDATE_DIFF_READ_BYTES
    )


def test_a_key_block_straddling_the_readers_cut_is_replaced_whole() -> None:
    """The block below opens before what a reader is shown ends and closes past it.

    Only its closing marker makes it a key block at all, so a store that read
    just far enough for the reader's own bound would hand this on with the
    marker missing -- and the key material standing before the cut would survive
    into an artifact and into another provider's job. The look-ahead is the
    whole block, not the material alone.
    """

    where_the_block_opens = MAXIMUM_CANDIDATE_DIFF_BYTES - 100

    shown = patch_safe_to_show(
        read_as_the_store_reads_it(
            PADDING_CHARACTER * where_the_block_opens + LONGEST_RECOGNISABLE_KEY_BLOCK
        ),
        MAXIMUM_CANDIDATE_DIFF_BYTES,
    )

    assert KEY_MATERIAL_CHARACTER not in shown.text
    assert "PRIVATE KEY" not in shown.text
    assert shown.redacted is True


def test_a_key_block_whose_close_fell_past_the_read_leaves_no_head_behind() -> None:
    """The block below is key material a store's own bound stops reading inside.

    What a redactor reads between the markers it counts in characters, while a
    store reads bytes, so material written in characters four bytes wide puts
    the closing marker past everything the store ever hands on. Nothing then
    names the block, and the opening plus the material behind it would stand in
    the cut text as the key it is -- so an opening whose close is not there is
    itself enough to take the rest of the text out.
    """

    where_the_key_material_begins = MAXIMUM_CANDIDATE_DIFF_BYTES - 100

    shown = patch_safe_to_show(
        read_as_the_store_reads_it(
            PADDING_CHARACTER * (where_the_key_material_begins - len(KEY_BLOCK_OPENS))
            + KEY_BLOCK_IN_WIDE_MATERIAL
        ),
        MAXIMUM_CANDIDATE_DIFF_BYTES,
    )

    assert WIDE_KEY_MATERIAL_CHARACTER not in shown.text
    assert "PRIVATE KEY" not in shown.text
    assert shown.redacted is True


def test_a_patch_that_ended_where_it_ended_is_shown_as_it_stands() -> None:
    patch = b"diff --git a/x b/x\n"

    shown = patch_safe_to_show(ReadPatch(patch, False), MAXIMUM_CANDIDATE_DIFF_BYTES)

    assert shown.text == patch.decode()
    assert shown.redacted is False


def test_a_patch_longer_than_a_reader_is_shown_is_cut_and_says_so() -> None:
    long_patch = b"+" * (MAXIMUM_CANDIDATE_DIFF_BYTES + 1)

    shown = patch_safe_to_show(
        ReadPatch(long_patch, False), MAXIMUM_CANDIDATE_DIFF_BYTES
    )

    assert len(shown.text.encode("utf-8")) == MAXIMUM_CANDIDATE_DIFF_BYTES
    assert shown.text.endswith(CANDIDATE_DIFF_TRUNCATION_MARKER)


def test_a_patch_the_store_stopped_reading_says_so_however_short_it_became() -> None:
    """Redaction shrinks a patch as readily as it grows it.

    A text back under a reader's own bound is therefore no evidence that the
    reading reached the end of the patch, and a reader told nothing would take
    the hunks that were never read for hunks that never existed.
    """

    stopped = ReadPatch(b"TOKEN = 'sk-ant-abcdefghijklmnopqrstuvwx'\n", True)

    shown = patch_safe_to_show(stopped, MAXIMUM_CANDIDATE_DIFF_BYTES)

    assert shown.text.endswith(CANDIDATE_DIFF_TRUNCATION_MARKER)
    assert REDACTION_MARKER in shown.text
    assert shown.redacted is True
