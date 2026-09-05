"""The patch the atelier reads of a candidate, and writes into its node's report.

**Why this exists.** A reviewer that reads only a builder's prose reviews the
builder, not the change (#1235). The patch between the pinned tree and the
candidate is a fact the runtime holds and the builder cannot be trusted for, so
the runtime writes it into the value the build node completes with -- under the
property the node's own published output schema declares for it. Nothing else
about `single-json-output/v1` changes: one declared output, one value, one
schema judging it.

**Whose word it is.** The property is the atelier's, never the builder's: a
report that already carries one has it replaced, and a run with no patch to show
carries no property at all rather than whatever the provider wrote there.

**Why it is bounded here.** The value travels the produced-value route, whose
bound is `MAXIMUM_AGENT_OUTPUT_BYTES_V2` -- and it is judged against that bound
twice, at the write and again when it is handed to the node that reads it. A
diff long enough to push the report past it would turn a green build into a
broken hand-off, so the diff is cut to what is left over and says, in the text
itself, that it was cut. Every way out of here is measured against that bound,
and the one report that leaves no room even for the sentence saying so is
refused rather than handed on silently missing the property its node declared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from atelier2.contracts.secret_redaction import (
    RedactedText,
    redact_an_unclosed_credential,
    redact_credentials,
)

CANDIDATE_DIFF_PROPERTY = "candidate_diff"
"""The one property name the runtime writes its patch under."""

CANDIDATE_DIFF_TRUNCATION_MARKER = (
    "\n[the atelier cut this diff here; the rest of the patch is not shown]\n"
)
"""What a cut diff says about itself, so no reader takes a part for the whole."""


@dataclass(frozen=True, slots=True)
class ReadPatch:
    """A patch as a store read it, and whether the store's bound stopped it short.

    The second half is a fact nothing about the bytes can recover: a reading
    that stopped at its bound and one that ended where the patch ended look the
    same, and redaction shrinks a text as readily as it grows it, so a patch
    back under a reader's own bound is no evidence that nothing was left out.
    """

    read: bytes
    stopped_at_the_bound: bool


def patch_safe_to_show(patch: ReadPatch, shown_bytes: int) -> RedactedText:
    """One reading of a patch a reader may be given: scrubbed whole, then cut.

    Credentials go first and the cut second, because cutting first leaves the
    half before the cut standing as the token it is -- the shape the redactor
    recognises no longer stands whole in what it is handed. The store reads
    further than `shown_bytes` for exactly that reason.

    A cut text is then read once more for an opening whose close no cut text can
    be trusted to carry: a look-ahead is measured in bytes and a block's own
    material in characters, so a block written wide enough closes past every
    reading of it, and the redactor recognising whole blocks never sees one.
    Bytes kept past such an opening are key material, and go.

    Whatever was left out, here or already at the store's own bound, is said in
    the text itself: a patch stopping mid-hunk with nothing to show for it reads
    as a change that ended there.
    """

    redacted = redact_credentials(patch.read.decode("utf-8", "replace"))
    shown = redacted.text.encode("utf-8")
    if not patch.stopped_at_the_bound and len(shown) <= shown_bytes:
        return redacted
    room = shown_bytes - len(CANDIDATE_DIFF_TRUNCATION_MARKER.encode("utf-8"))
    kept = redact_an_unclosed_credential(shown[:room].decode("utf-8", "replace"))
    return RedactedText(
        kept.text + CANDIDATE_DIFF_TRUNCATION_MARKER,
        redacted.redacted or kept.redacted,
    )


def schema_declares_candidate_diff(document: bytes) -> bool:
    """Whether this node's own published output schema makes room for the patch.

    The document, not the parsed schema: the profile owner
    (`contracts/schemas_v3.py`) is the only place this product evaluates JSON
    Schema, and asking whether an author declared one property is reading their
    document rather than evaluating it.
    """

    declared = json.loads(document)
    if not isinstance(declared, dict):
        return False
    properties = declared.get("properties")
    return isinstance(properties, dict) and CANDIDATE_DIFF_PROPERTY in properties


class CandidateReportDoesNotFit(Exception):
    """This node's own value has no room for the patch its schema declares.

    Raised rather than answered around, because the two ways out of a value
    that will not fit are the two this product refuses: dropping the property
    tells the node reading it that the change was empty, and handing back an
    oversized value writes a durable record the same bound refuses one step
    later. The caller ends the attempt on this instead.
    """


def report_carrying_candidate_diff(
    report: bytes, diff: str | None, maximum_bytes: int
) -> bytes:
    """One build node's value: what the provider answered, plus the atelier's patch.

    `report` is the answer the node's declared schema has already admitted, so a
    value that is not an object has no named property to carry and is handed back
    exactly as it arrived. `diff` absent means there is no patch to show, and the
    report then carries none -- including none the provider invented.

    The result never exceeds `maximum_bytes`: each pass measures the encoded
    value, gives the diff back exactly the room it overspent, and marks the cut,
    down to the marker standing alone where that is all the room there was. A
    report filling the bound so completely that even the marker cannot follow it
    raises, because a value that says nothing about a patch it was asked to
    carry is the silence this exists to remove.
    """

    decoded = json.loads(report)
    if not isinstance(decoded, dict):
        return report
    without_diff = {
        name: value
        for name, value in decoded.items()
        if name != CANDIDATE_DIFF_PROPERTY
    }
    if diff is None:
        return _within(_encoded(without_diff), maximum_bytes)
    kept = diff
    while True:
        value = _encoded({**without_diff, CANDIDATE_DIFF_PROPERTY: kept})
        if len(value) <= maximum_bytes:
            return value
        if kept == CANDIDATE_DIFF_TRUNCATION_MARKER:
            raise CandidateReportDoesNotFit(
                f"this node answered {len(_encoded(without_diff))} bytes of its "
                f"own, and the {maximum_bytes} one produced value carries leaves "
                f"no room beside them even to say the patch was cut ({len(value)} "
                "bytes would be needed)"
            )
        room = len(kept) - (len(value) - maximum_bytes)
        # What one character of a patch costs the encoded value is not one byte
        # -- a quote or a newline is escaped -- so the room measured here is an
        # over-estimate that the next pass measures again. Never below the
        # marker: a cut nobody is told about reads as a patch that ended there.
        kept = (
            diff[: max(0, room - len(CANDIDATE_DIFF_TRUNCATION_MARKER))]
            + CANDIDATE_DIFF_TRUNCATION_MARKER
        )


def _within(value: bytes, maximum_bytes: int) -> bytes:
    """The one value, or the refusal that it does not fit the route it travels."""

    if len(value) > maximum_bytes:
        raise CandidateReportDoesNotFit(
            f"this node's own answer encodes to {len(value)} bytes, past the "
            f"{maximum_bytes} one produced value carries"
        )
    return value


def _encoded(value: dict[str, object]) -> bytes:
    """The one encoding this owner writes, so measuring it means measuring it.

    Compact and separator-free: a re-encoding that inserted the whitespace
    `json.dumps` writes by default could grow an answer already sitting at the
    bound past it, which would make every measurement here a measurement of
    something else than what is written.
    """

    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
