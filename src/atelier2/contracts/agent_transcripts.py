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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import assert_never

from atelier2.contracts.artifacts import MAXIMUM_ARTIFACT_BYTES
from atelier2.contracts.secret_redaction import redact_credentials

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
    TRANSCRIPT_TRUNCATED = "transcript-truncated"


@dataclass(frozen=True, slots=True)
class ToolCalled:
    """The agent asked for a door to be opened, and with what."""

    name: str
    arguments: str
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class ToolReturned:
    """What that door answered."""

    name: str
    result: str
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    """What the agent said in its own words on the way to its answer."""

    text: str
    redacted: bool = False


@dataclass(frozen=True, slots=True)
class Usage:
    """What the attempt spent, as the provider counted it."""

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

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
class TranscriptTruncated:
    """The oldest steps did not fit, and this is how many of them there were."""

    dropped_events: int

    def __post_init__(self) -> None:
        if type(self.dropped_events) is not int or self.dropped_events < 1:
            raise ValueError("a truncation marker stands for at least one lost step")


type TranscriptEvent = (
    ToolCalled | ToolReturned | AssistantTurn | Usage | TranscriptTruncated
)

_DOCUMENT_KIND = "attempt-transcript/v1"
_KIND_FIELD = "event"
_DOCUMENT_PREFIX = f'{{"kind":"{_DOCUMENT_KIND}","events":['.encode()
_DOCUMENT_SUFFIX = b"]}"
_EVENT_SEPARATOR = b","


def _event_document(event: TranscriptEvent) -> dict[str, object]:
    match event:
        case ToolCalled(name, arguments, redacted):
            return {
                _KIND_FIELD: TranscriptEventKind.TOOL_CALLED.value,
                "name": name,
                "arguments": arguments,
                "redacted": redacted,
            }
        case ToolReturned(name, result, redacted):
            return {
                _KIND_FIELD: TranscriptEventKind.TOOL_RETURNED.value,
                "name": name,
                "result": result,
                "redacted": redacted,
            }
        case AssistantTurn(text, redacted):
            return {
                _KIND_FIELD: TranscriptEventKind.ASSISTANT_TURN.value,
                "text": text,
                "redacted": redacted,
            }
        case Usage(input_tokens, output_tokens, cache_read, cache_creation):
            return {
                _KIND_FIELD: TranscriptEventKind.USAGE.value,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            }
        case TranscriptTruncated(dropped_events):
            return {
                _KIND_FIELD: TranscriptEventKind.TRANSCRIPT_TRUNCATED.value,
                "dropped_events": dropped_events,
            }
        case _ as unreachable:
            assert_never(unreachable)


def _event_fragment(event: TranscriptEvent) -> bytes:
    return json.dumps(
        _event_document(event), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _document_of(fragments: tuple[bytes, ...]) -> bytes:
    return _DOCUMENT_PREFIX + _EVENT_SEPARATOR.join(fragments) + _DOCUMENT_SUFFIX


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
    match event:
        case ToolCalled(name, arguments, _):
            (kept_name, kept_arguments), redacted = _readable(name, arguments)
            return ToolCalled(kept_name, kept_arguments, redacted)
        case ToolReturned(name, result, _):
            (kept_name, kept_result), redacted = _readable(name, result)
            return ToolReturned(kept_name, kept_result, redacted)
        case AssistantTurn(text, _):
            (kept_text,), redacted = _readable(text)
            return AssistantTurn(kept_text, redacted)
        case Usage() | TranscriptTruncated():
            return event
        case _ as unreachable:
            assert_never(unreachable)


def _within_the_document_bound(
    events: tuple[TranscriptEvent, ...],
) -> tuple[TranscriptEvent, ...]:
    """These events, oldest ones dropped until the document fits, with the count.

    Sizes are measured once and the drop is arithmetic on them, so a transcript
    of many small steps is bounded in one pass rather than by re-serializing it
    on every drop.
    """

    if not events:
        return events
    sizes = [len(_event_fragment(event)) for event in events]
    overhead = len(_DOCUMENT_PREFIX) + len(_DOCUMENT_SUFFIX)
    separators = max(len(events) - 1, 0)
    remaining = sum(sizes) + separators
    for dropped in range(len(events)):
        marker = None if dropped == 0 else TranscriptTruncated(dropped)
        marker_size = 0 if marker is None else len(_event_fragment(marker)) + 1
        if overhead + marker_size + remaining <= MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES:
            kept = events[dropped:]
            return kept if marker is None else (marker, *kept)
        remaining -= sizes[dropped] + 1
    raise ValueError("no single transcript step fits the transcript document bound")


@dataclass(frozen=True)
class AttemptTranscript:
    """One attempt's readable, bounded steps, and the exact bytes they are kept as."""

    events: tuple[TranscriptEvent, ...]
    document: bytes = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.events, tuple):
            raise TypeError("a transcript's steps are an exact ordered tuple")
        if not self.events:
            raise ValueError("a transcript with no steps is no transcript")
        document = _document_of(tuple(map(_event_fragment, self.events)))
        if len(document) > MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES:
            raise ValueError(
                f"transcript document exceeds {MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES} bytes"
            )
        object.__setattr__(self, "document", document)

    @classmethod
    def of(cls, events: Iterable[TranscriptEvent]) -> AttemptTranscript:
        """The transcript these raw provider events are allowed to become.

        Every step is made readable before anything measures it, and the whole
        is then brought under the document bound. This is the only way in: an
        adapter hands over what it decoded, and what comes back is already what
        the store may keep.
        """

        return cls(_within_the_document_bound(tuple(map(_kept, tuple(events)))))
