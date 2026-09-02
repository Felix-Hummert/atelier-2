"""What an agent did between the job and the answer, kept where it can be read.

**Why this exists.** An attempt used to leave two facts behind: the job it was
given and the bytes it answered with. Everything in between -- which door the
agent opened, with which arguments, what came back, how many turns it took --
lived inside the executor's lease and was gone when the process ended. Nobody,
operator or reviewing agent, could tell a good answer from a lucky one, and
"the run says it worked" was the only available evidence. The concern that kept
it unstored is real (a raw stream can carry a credential the agent happened to
read) and it is answered by bounding and redacting what is kept, not by keeping
nothing (`#666`, operator ruling of 25.08.).

**Why the events are the provider's, without being a provider's.** A transcript
is what any agent does, not what one CLI prints: a tool call with its
arguments, the result that came back, an assistant turn, and what the call
cost. Each adapter maps its own wire format onto exactly these, so a reader
learns one vocabulary and a provider that publishes no structured stream keeps
an honest nothing instead of a shape invented for it.

**Why nothing here can be kept unbounded or unread.** A transcript is kept as
one content-addressed artifact, so it is read whole and must cost what an
artifact may cost. Two bounds do that, and both are visible in the result: a
step longer than a reader can use is cut with its marker in place, and a
transcript with more steps than the document may hold loses its oldest ones and
says how many. What is newest is what a failure is read backwards from, so that
is what survives.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import assert_never

from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.contracts.secret_redaction import redact_credentials
from atelier2.contracts.when import RecordedAt

MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES = MAXIMUM_ARTIFACT_BYTES
"""What one attempt's whole transcript may cost, serialized.

It is the artifact bound because a transcript is kept as one artifact and read
whole -- larger could not be stored at all, and smaller would refuse evidence
the store already accepts from every other producer.
"""

MAXIMUM_TRANSCRIPT_STEP_CHARACTERS = 8_192
"""What one text of one step may carry before it is cut.

A step is read, so this is the width at which reading stops being possible:
past it a tool result is bulk material rather than a step in a story, and the
answer the run acted on is kept separately under its own hash regardless. It is
also what keeps the document bound from being spent by one step -- at this
width around a hundred full-size steps fit, so a tool-using attempt at its turn
cap keeps every step it took.
"""

TRANSCRIPT_STEP_CUT_MARKER = " [cut]"
"""What stands at the end of a step this transcript could not carry whole."""


class TranscriptEventKind(StrEnum):
    """The persisted name of every event a transcript document may carry."""

    TOOL_CALLED = "tool-called"
    TOOL_RETURNED = "tool-returned"
    ASSISTANT_TURN = "assistant-turn"
    USAGE = "usage"
    PROVIDER_TERMINAL_REFUSAL = "provider-terminal-refusal"
    UNRECOGNISED_PROVIDER_OUTPUT = "unrecognised-provider-output"
    TRANSCRIPT_TRUNCATED = "transcript-truncated"


class TranscriptMomentOrigin(StrEnum):
    """Where the time a transcript event names came from.

    This vocabulary has one member today. Keeping it on the moment, rather than
    implying the source from a field name, lets a provider-supplied instant join
    later without changing the event shape a reader already understands.
    """

    RECORDED = "recorded"


@dataclass(frozen=True, slots=True)
class TranscriptRecordedMoment:
    """The time this event entered the attempt transcript."""

    recorded_at: RecordedAt
    origin: TranscriptMomentOrigin

    def __post_init__(self) -> None:
        if not isinstance(self.recorded_at, RecordedAt):
            raise TypeError("a transcript event moment must be a recorded instant")
        if not isinstance(self.origin, TranscriptMomentOrigin):
            raise TypeError("a transcript event moment must name its origin")


@dataclass(frozen=True, slots=True)
class TranscriptBeforeMoments:
    """The event came from a v1 transcript, before transcript moments existed."""


type TranscriptEventMoment = TranscriptRecordedMoment | TranscriptBeforeMoments


@dataclass(frozen=True, slots=True)
class ToolCalled:
    """The agent asked for a door to be opened, and with what."""

    name: str
    arguments: str
    redacted: bool = False
    moment: TranscriptEventMoment = field(default_factory=TranscriptBeforeMoments)


@dataclass(frozen=True, slots=True)
class ToolReturned:
    """What that door answered."""

    name: str
    result: str
    redacted: bool = False
    moment: TranscriptEventMoment = field(default_factory=TranscriptBeforeMoments)


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    """What the agent said in its own words on the way to its answer."""

    text: str
    redacted: bool = False
    moment: TranscriptEventMoment = field(default_factory=TranscriptBeforeMoments)


@dataclass(frozen=True, slots=True)
class Usage:
    """What the attempt spent, as the provider counted it."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    moment: TranscriptEventMoment = field(default_factory=TranscriptBeforeMoments)

    def __post_init__(self) -> None:
        counts = (
            self.input_tokens,
            self.output_tokens,
            self.cache_read_input_tokens,
            self.cache_creation_input_tokens,
        )
        if any(type(count) is not int for count in counts):
            raise TypeError("agent usage counts tokens as integers")
        if any(count < 0 for count in counts):
            raise ValueError("agent usage cannot count fewer than no tokens")


@dataclass(frozen=True, slots=True)
class ProviderTerminalRefusal:
    """The provider read this call and refused it before any inference ran.

    Distinct from `UnrecognisedProviderOutput`: that step is for a line this
    vocabulary does not know, and this one is for a line it knows exactly --
    the provider's own terminal `result` naming `is_error` true. A refusal like
    this ends the call with the exit code and standard error of a process that
    behaved exactly as designed, which explained nothing at all (`#1029`).
    `api_error_status` and `terminal_reason` are kept as empty text rather than
    omitted where the provider's line did not carry them, so every step of this
    kind has the same three readable fields regardless of which provider
    release wrote it.
    """

    terminal_reason: str
    api_error_status: str
    text: str
    redacted: bool = False
    moment: TranscriptEventMoment = field(default_factory=TranscriptBeforeMoments)


@dataclass(frozen=True, slots=True)
class UnrecognisedProviderOutput:
    """The provider wrote this, and no step above describes it.

    Two things arrive here, and both are evidence rather than noise: a stream
    entry whose shape this adapter's vocabulary does not know, and what a call
    that ended badly printed instead of a stream at all -- the diagnosis of an
    exit nobody could explain (`#733`). Keeping it is the point. A transcript
    that dropped whatever it could not classify would answer "the agent did
    nothing" for exactly the episodes somebody is reading it about, and the one
    line naming the cause is usually the line no vocabulary predicted.
    """

    text: str
    redacted: bool = False
    moment: TranscriptEventMoment = field(default_factory=TranscriptBeforeMoments)


@dataclass(frozen=True, slots=True)
class TranscriptTruncated:
    """The oldest steps did not fit, and this is how many of them there were."""

    dropped_events: int
    moment: TranscriptEventMoment = field(default_factory=TranscriptBeforeMoments)

    def __post_init__(self) -> None:
        if type(self.dropped_events) is not int or self.dropped_events < 1:
            raise ValueError("a truncation marker stands for at least one lost step")


type TranscriptEvent = (
    ToolCalled
    | ToolReturned
    | AssistantTurn
    | Usage
    | ProviderTerminalRefusal
    | UnrecognisedProviderOutput
    | TranscriptTruncated
)

_DOCUMENT_KIND_V1 = "attempt-transcript/v1"
_DOCUMENT_KIND_V2 = "attempt-transcript/v2"
_KIND_FIELD = "event"
_DOCUMENT_SUFFIX = b"]}"
_EVENT_SEPARATOR = b","


def _document_prefix(document_kind: str) -> bytes:
    return f'{{"kind":"{document_kind}","events":['.encode()


def _event_document(event: TranscriptEvent, document_kind: str) -> dict[str, object]:
    document: dict[str, object]
    match event:
        case ToolCalled(name, arguments, redacted):
            document = {
                _KIND_FIELD: TranscriptEventKind.TOOL_CALLED.value,
                "name": name,
                "arguments": arguments,
                "redacted": redacted,
            }
        case ToolReturned(name, result, redacted):
            document = {
                _KIND_FIELD: TranscriptEventKind.TOOL_RETURNED.value,
                "name": name,
                "result": result,
                "redacted": redacted,
            }
        case AssistantTurn(text, redacted):
            document = {
                _KIND_FIELD: TranscriptEventKind.ASSISTANT_TURN.value,
                "text": text,
                "redacted": redacted,
            }
        case Usage(input_tokens, output_tokens, cache_read, cache_creation):
            document = {
                _KIND_FIELD: TranscriptEventKind.USAGE.value,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            }
        case ProviderTerminalRefusal(terminal_reason, api_error_status, text, redacted):
            document = {
                _KIND_FIELD: TranscriptEventKind.PROVIDER_TERMINAL_REFUSAL.value,
                "terminal_reason": terminal_reason,
                "api_error_status": api_error_status,
                "text": text,
                "redacted": redacted,
            }
        case UnrecognisedProviderOutput(text, redacted):
            document = {
                _KIND_FIELD: TranscriptEventKind.UNRECOGNISED_PROVIDER_OUTPUT.value,
                "text": text,
                "redacted": redacted,
            }
        case TranscriptTruncated(dropped_events):
            document = {
                _KIND_FIELD: TranscriptEventKind.TRANSCRIPT_TRUNCATED.value,
                "dropped_events": dropped_events,
            }
        case _ as unreachable:
            assert_never(unreachable)
    match document_kind, event.moment:
        case _DOCUMENT_KIND_V1, TranscriptBeforeMoments():
            return document
        case _DOCUMENT_KIND_V2, TranscriptRecordedMoment(recorded_at, origin):
            document["moment"] = {
                "recorded_at": recorded_at.value,
                "origin": origin.value,
            }
            return document
        case _DOCUMENT_KIND_V1, TranscriptRecordedMoment():
            raise ValueError("a v1 transcript event cannot carry a moment")
        case _DOCUMENT_KIND_V2, TranscriptBeforeMoments():
            raise ValueError("a v2 transcript event must carry a moment")
        case _ as unreachable:
            assert_never(unreachable)


def _require_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"transcript event {field} is not text")
    return value


def _require_flag(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise TypeError(f"transcript event {field} is not a boolean")
    return value


def _require_count(value: object, field: str) -> int:
    if type(value) is not int:
        raise TypeError(f"transcript event {field} is not an integer")
    return value


def _event_moment_from_document(
    payload: dict[object, object], document_kind: str
) -> TranscriptEventMoment:
    if document_kind == _DOCUMENT_KIND_V1:
        return TranscriptBeforeMoments()
    moment_payload = payload.get("moment")
    if not isinstance(moment_payload, dict):
        raise TypeError("a transcript event moment is an object")
    try:
        origin = TranscriptMomentOrigin(moment_payload.get("origin"))
    except ValueError:
        raise ValueError("unknown transcript event moment origin") from None
    return TranscriptRecordedMoment(
        RecordedAt(
            _require_text(moment_payload.get("recorded_at"), "moment recorded_at")
        ),
        origin,
    )


def _event_from_document(payload: object, document_kind: str) -> TranscriptEvent:
    if not isinstance(payload, dict):
        raise TypeError("a transcript event is an object")
    kind_value = payload.get(_KIND_FIELD)
    try:
        kind = TranscriptEventKind(kind_value)
    except ValueError:
        raise ValueError(f"unknown transcript event {kind_value!r}") from None
    match kind:
        case TranscriptEventKind.TOOL_CALLED:
            event: TranscriptEvent = ToolCalled(
                _require_text(payload.get("name"), "name"),
                _require_text(payload.get("arguments"), "arguments"),
                _require_flag(payload.get("redacted"), "redacted"),
            )
        case TranscriptEventKind.TOOL_RETURNED:
            event = ToolReturned(
                _require_text(payload.get("name"), "name"),
                _require_text(payload.get("result"), "result"),
                _require_flag(payload.get("redacted"), "redacted"),
            )
        case TranscriptEventKind.ASSISTANT_TURN:
            event = AssistantTurn(
                _require_text(payload.get("text"), "text"),
                _require_flag(payload.get("redacted"), "redacted"),
            )
        case TranscriptEventKind.USAGE:
            event = Usage(
                _require_count(payload.get("input_tokens"), "input_tokens"),
                _require_count(payload.get("output_tokens"), "output_tokens"),
                _require_count(
                    payload.get("cache_read_input_tokens"),
                    "cache_read_input_tokens",
                ),
                _require_count(
                    payload.get("cache_creation_input_tokens"),
                    "cache_creation_input_tokens",
                ),
            )
        case TranscriptEventKind.PROVIDER_TERMINAL_REFUSAL:
            event = ProviderTerminalRefusal(
                _require_text(payload.get("terminal_reason"), "terminal_reason"),
                _require_text(payload.get("api_error_status"), "api_error_status"),
                _require_text(payload.get("text"), "text"),
                _require_flag(payload.get("redacted"), "redacted"),
            )
        case TranscriptEventKind.UNRECOGNISED_PROVIDER_OUTPUT:
            event = UnrecognisedProviderOutput(
                _require_text(payload.get("text"), "text"),
                _require_flag(payload.get("redacted"), "redacted"),
            )
        case TranscriptEventKind.TRANSCRIPT_TRUNCATED:
            event = TranscriptTruncated(
                _require_count(payload.get("dropped_events"), "dropped_events")
            )
        case _ as unreachable:
            assert_never(unreachable)
    event = replace(event, moment=_event_moment_from_document(payload, document_kind))
    if _event_document(event, document_kind) != payload:
        raise ValueError("transcript event fields disagree")
    return event


def _event_fragment(event: TranscriptEvent, document_kind: str) -> bytes:
    return json.dumps(
        _event_document(event, document_kind), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _document_of(fragments: tuple[bytes, ...], document_kind: str) -> bytes:
    return (
        _document_prefix(document_kind)
        + _EVENT_SEPARATOR.join(fragments)
        + _DOCUMENT_SUFFIX
    )


def _cut(text: str) -> str:
    if len(text) <= MAXIMUM_TRANSCRIPT_STEP_CHARACTERS:
        return text
    kept = MAXIMUM_TRANSCRIPT_STEP_CHARACTERS - len(TRANSCRIPT_STEP_CUT_MARKER)
    return text[:kept] + TRANSCRIPT_STEP_CUT_MARKER


def _readable(*texts: str) -> tuple[tuple[str, ...], bool]:
    """Every text of one step, and whether keeping any of them changed it.

    Redaction runs before the cut so a credential straddling the cut is still
    recognised as one -- cutting first could leave a half nothing matches. Every
    text of a step goes through here, the tool's own name included: a step whose
    one field stayed unbounded could not be brought under the document bound by
    dropping other steps.
    """

    redacted = tuple(map(redact_credentials, texts))
    return (
        tuple(_cut(one.text) for one in redacted),
        any(one.redacted for one in redacted),
    )


def _kept(event: TranscriptEvent) -> TranscriptEvent:
    """This step as the document may hold it, whatever it arrived as.

    Running twice answers what running once answered. A step that was already
    made readable matches no credential shape the second time, so the flag is
    carried forward rather than recomputed -- otherwise normalising an already
    normalised step would quietly un-say that something had been replaced.
    """

    match event:
        case ToolCalled(name, arguments, marked, moment):
            (kept_name, kept_arguments), redacted = _readable(name, arguments)
            return ToolCalled(kept_name, kept_arguments, redacted or marked, moment)
        case ToolReturned(name, result, marked, moment):
            (kept_name, kept_result), redacted = _readable(name, result)
            return ToolReturned(kept_name, kept_result, redacted or marked, moment)
        case AssistantTurn(text, marked, moment):
            (kept_text,), redacted = _readable(text)
            return AssistantTurn(kept_text, redacted or marked, moment)
        case ProviderTerminalRefusal(
            terminal_reason, api_error_status, text, marked, moment
        ):
            (kept_reason, kept_status, kept_text), redacted = _readable(
                terminal_reason, api_error_status, text
            )
            return ProviderTerminalRefusal(
                kept_reason, kept_status, kept_text, redacted or marked, moment
            )
        case UnrecognisedProviderOutput(text, marked, moment):
            (kept_text,), redacted = _readable(text)
            return UnrecognisedProviderOutput(kept_text, redacted or marked, moment)
        case Usage() | TranscriptTruncated():
            return event
        case _ as unreachable:
            assert_never(unreachable)


def _within_the_document_bound(
    events: tuple[TranscriptEvent, ...],
    document_kind: str,
) -> tuple[TranscriptEvent, ...]:
    """These events, oldest ones dropped until the document fits, with the count.

    Sizes are measured once and the drop is arithmetic on them, so a transcript
    of many small steps is bounded in one pass rather than by re-serializing it
    on every drop.
    """

    if not events:
        return events
    sizes = [len(_event_fragment(event, document_kind)) for event in events]
    overhead = len(_document_prefix(document_kind)) + len(_DOCUMENT_SUFFIX)
    separators = max(len(events) - 1, 0)
    remaining = sum(sizes) + separators
    for dropped in range(len(events)):
        marker = (
            None
            if dropped == 0
            else TranscriptTruncated(dropped, events[dropped].moment)
        )
        marker_size = (
            0 if marker is None else len(_event_fragment(marker, document_kind)) + 1
        )
        if overhead + marker_size + remaining <= MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES:
            kept = events[dropped:]
            return kept if marker is None else (marker, *kept)
        remaining -= sizes[dropped] + 1
    raise ValueError("no single transcript step fits the transcript document bound")


def _document_kind(events: tuple[TranscriptEvent, ...]) -> str:
    moments = tuple(event.moment for event in events)
    if all(isinstance(moment, TranscriptBeforeMoments) for moment in moments):
        return _DOCUMENT_KIND_V1
    if all(isinstance(moment, TranscriptRecordedMoment) for moment in moments):
        return _DOCUMENT_KIND_V2
    raise ValueError("a transcript cannot mix events before and with moments")


@dataclass(frozen=True)
class AttemptTranscript:
    """One attempt's readable, bounded steps, and the exact bytes they are kept as.

    **There is no way in that skips the redactor.** Constructing one *is* making
    the steps safe: whatever arrives is scanned for credential shapes, cut to a
    readable width and brought under the document bound before anything is
    measured, so `AttemptTranscript(events)` and `AttemptTranscript.of(events)`
    are the same transcript. An earlier revision made that the classmethod's job
    and left the constructor beside it, which meant every caller had to remember
    which door was the safe one -- and a caller that forgot published a
    provider's raw bytes. A redactor a caller can walk past is not a redactor.

    What this means for a reader: `events` is what the document holds, not what
    was handed in, and comparing two transcripts compares what would be stored.
    """

    events: tuple[TranscriptEvent, ...]
    document: bytes = field(init=False)
    _read_from_v1_document: bool = field(init=False, default=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple):
            raise TypeError("a transcript's steps are an exact ordered tuple")
        if not self.events:
            raise ValueError("a transcript with no steps is no transcript")
        document_kind = _document_kind(self.events)
        kept = _within_the_document_bound(tuple(map(_kept, self.events)), document_kind)
        document = _document_of(
            tuple(_event_fragment(event, document_kind) for event in kept),
            document_kind,
        )
        if len(document) > MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES:
            raise ValueError(
                f"transcript document exceeds {MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES} bytes"
            )
        object.__setattr__(self, "events", kept)
        object.__setattr__(self, "document", document)

    @classmethod
    def of(cls, events: Iterable[TranscriptEvent]) -> AttemptTranscript:
        """The transcript these raw provider events are allowed to become.

        The name an adapter reads at its call site. It adds nothing the
        constructor does not already do -- that is the point -- and it accepts
        any iterable, because what an adapter has decoded is rarely a tuple yet.
        """

        return cls(tuple(events))

    def with_recorded_moment(self, recorded_at: RecordedAt) -> AttemptTranscript:
        """Record a decoded transcript now, without rewriting a stored v1 record."""

        if not isinstance(recorded_at, RecordedAt):
            raise TypeError("a transcript recording moment must be a recorded instant")
        if self._read_from_v1_document:
            raise ValueError("a v1 transcript cannot be given a recording moment")
        moment = TranscriptRecordedMoment(recorded_at, TranscriptMomentOrigin.RECORDED)
        return AttemptTranscript(
            tuple(replace(event, moment=moment) for event in self.events)
        )

    @classmethod
    def from_document(cls, document: bytes) -> AttemptTranscript:
        """The transcript these exact stored bytes already are, or a loud refusal.

        Reconstruction goes through the constructor, so a document that skipped
        redaction, cutting, or the document bound cannot be read back as if it
        had been kept. Extra keys, an unknown kind, missing fields, bad types,
        empty events, or a canonical encoding that is not the stored bytes are
        all the same refusal: this is not a transcript this runtime can serve.
        """

        if type(document) is not bytes:
            raise TypeError("a transcript document is exact bytes")
        try:
            decoded = json.loads(document)
        except ValueError as broken:
            raise ValueError("transcript document is not JSON") from broken
        try:
            if not isinstance(decoded, dict):
                raise TypeError("a transcript document is an object")
            if set(decoded) != {"kind", "events"}:
                raise ValueError(
                    "a transcript document names kind and events and nothing else"
                )
            document_kind = decoded["kind"]
            if document_kind not in {_DOCUMENT_KIND_V1, _DOCUMENT_KIND_V2}:
                raise ValueError(f"unknown transcript document kind {document_kind!r}")
            events_payload = decoded["events"]
            if not isinstance(events_payload, list):
                raise TypeError("a transcript's events are a list")
            reconstructed = cls(
                tuple(
                    _event_from_document(payload, document_kind)
                    for payload in events_payload
                )
            )
        except TypeError as broken:
            raise ValueError(
                "transcript document fields have the wrong types"
            ) from broken
        if reconstructed.document != document:
            raise ValueError(
                "transcript document is not the canonical encoding of its events"
            )
        if document_kind == _DOCUMENT_KIND_V1:
            object.__setattr__(reconstructed, "_read_from_v1_document", True)
        return reconstructed
