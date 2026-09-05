"""Newline-delimited JSON-RPC 2.0, bounded before a frame is believed.

**Why a codec of its own.** A duplex provider speaks one JSON object per line,
and everything a conversation does above that -- correlating an answer to the
question it answers, refusing a frame nobody may buffer, spelling a reply -- is
the same work whichever protocol runs on top. Keeping it here leaves the
protocol above free of parsing, and leaves this file free of any provider's
vocabulary: nothing in it knows what a method means.

**Why nothing is decoded before it is measured.** A frame is bytes until it has
been counted: a provider that never writes a newline, or writes one line larger
than this conversation may hold, must be refused while it is still a length --
decoding first is exactly the buffer this bound exists to refuse. Both the
completed frame and the exact unfinished remainder are held to it. Spelling
an outgoing frame is measured the same way: a string leaf is escaped in
slices of at most `max(1, maximum_bytes // 12)` code points and folded
straight into one growing buffer, so this codec's peak memory while
spelling one frame is payload-sized transient storage bounded by
`5 × maximum_bytes + fixed runtime/object overhead`, whatever the
string's real length is (below the escape width, `48 + maximum_bytes +
fixed overhead` instead -- see that constant).

**Why a broken frame is a value, not an exception.** Everything a provider can
get wrong -- unparseable bytes, a batch, an id that is not an id, an answer to
a question nobody asked -- is a state the conversation above must be able to
answer for, and a raised exception in the middle of a supervision loop is a
state nobody can answer for. Every such frame is therefore named and returned.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TypeGuard, assert_never

JSON_RPC_VERSION = "2.0"

PARSE_ERROR_CODE = -32700
INVALID_REQUEST_CODE = -32600
METHOD_NOT_FOUND_CODE = -32601
INVALID_PARAMS_CODE = -32602
INTERNAL_ERROR_CODE = -32603

type JsonValue = (
    None | bool | int | float | str | Sequence[JsonValue] | Mapping[str, JsonValue]
)
type JsonObject = Mapping[str, JsonValue]
type JsonRpcId = int | str

NO_PARAMS: JsonObject = {}
_COMPACT_SEPARATORS = (",", ":")
_FRAME_SEPARATOR = b"\n"
_MAXIMUM_JSON_ESCAPE_WIDTH_BYTES = 12
"""Widest one code point can spell as a `json.dumps(ensure_ascii=True)` escape.

A surrogate pair -- the escaping of one non-BMP code point -- spells as two
`\\uXXXX` units, twelve ASCII characters for that single code point. No
character escapes wider than this, and no code point costs more raw bytes as
a Python `str` either, so every buffer built from one code point -- raw or
escaped -- is bounded by this same width.
"""

_TRANSIENT_BUFFERS_PER_SLICE = 4
"""Buffers one escaping slice can hold at once, in `_spelled_string_pieces`.

The raw slice itself, `json.dumps`'s quoted escape of it, that escape's
`[1:-1]` copy without its quotes, and that copy's own encoded bytes -- none
wider than `_MAXIMUM_JSON_ESCAPE_WIDTH_BYTES` bytes per code point. The
quoted escape is freed before the encoded bytes are built, so at most three
of the four are ever alive together; four is the safe, conservative count
this codec's contract is stated against.
"""

_MAXIMUM_TRANSIENT_FACTOR_OF_BOUND = _TRANSIENT_BUFFERS_PER_SLICE + 1
"""The multiple of `maximum_bytes` this codec's payload-sized transient
storage peak ever reaches.

`_spelled_within` accumulates every already-spelled piece into one `bytearray`
that never grows past `maximum_bytes`, refusing before an append would cross
it: that buffer, together with its own bounded growth slack, is one
payload-width quantity, alongside the `_TRANSIENT_BUFFERS_PER_SLICE` buffers
of whichever slice is being escaped into it -- five in total, this factor, so
the peak is `5 × maximum_bytes + fixed runtime/object overhead`. The
one-time final `bytes(buffer)` copy at the end coexists only with that same
buffer, a strictly smaller peak the figure above already covers.

This factor is not a universal claim: below `_MAXIMUM_JSON_ESCAPE_WIDTH_BYTES`
bytes, `_string_slice_characters` clamps to one code point rather than a
bound-proportional count, so the `_TRANSIENT_BUFFERS_PER_SLICE` escaping
buffers are a fixed `_TRANSIENT_BUFFERS_PER_SLICE *
_MAXIMUM_JSON_ESCAPE_WIDTH_BYTES` (48) bytes -- but the accumulating buffer is
still bounded by `maximum_bytes` on its own, so the peak there is
`48 + maximum_bytes + fixed overhead`, never this factor times it. Every
sentence elsewhere naming this factor carries this same exception.
"""


def _string_slice_characters(maximum_bytes: int) -> int:
    """How many code points one escaping slice may hold under this bound.

    Below `_MAXIMUM_JSON_ESCAPE_WIDTH_BYTES` bytes this clamps to one code
    point rather than falling to zero: one code point's escape can still cost
    the full escape width however small `maximum_bytes` is, so there is no
    smaller bound-proportional slice to fall back to.
    """

    return max(1, maximum_bytes // _MAXIMUM_JSON_ESCAPE_WIDTH_BYTES)


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    """A call that expects exactly one answer addressed to its id."""

    id: JsonRpcId
    method: str
    params: JsonObject


@dataclass(frozen=True, slots=True)
class JsonRpcNotification:
    """A call that is never answered, in either direction."""

    method: str
    params: JsonObject


@dataclass(frozen=True, slots=True)
class JsonRpcResponse:
    """This side's answer to one request the other side addressed to it."""

    id: JsonRpcId
    result: JsonObject


@dataclass(frozen=True, slots=True)
class JsonRpcError:
    """This side's refusal of one request, or of a frame whose id it never read."""

    id: JsonRpcId | None
    code: int
    message: str


@dataclass(frozen=True, slots=True)
class JsonRpcAnswer:
    """What came back for one request this codec minted, named by what it asked.

    A response carries an id and never a method, so only the side that minted
    the id can say which question it answers. Saying it here is what keeps the
    protocol above from holding a second map of the same ids.
    """

    method: str
    result: JsonObject


@dataclass(frozen=True, slots=True)
class JsonRpcFailure:
    """The refusal that came back for one request this codec minted."""

    method: str
    code: int
    message: str


class JsonRpcFault(StrEnum):
    """A frame this codec will not believe, in the reading that refuses it.

    A call and an answer are refused apart because only one of them is owed an
    answer: an error frame addressed to a response would be this side inventing
    a question the other side never asked.
    """

    UNPARSEABLE = "unparseable"
    NOT_A_MESSAGE = "not-a-message"
    UNEXPECTED_RESPONSE = "unexpected-response"
    MALFORMED_RESPONSE = "malformed-response"
    OVERSIZE_FRAME = "oversize-frame"


@dataclass(frozen=True, slots=True)
class JsonRpcProtocolFault:
    """One frame the codec refused, why it could not be read, and whose it was.

    The id is the refused frame's own, kept wherever the frame was still
    readable as a call: an error answers exactly one request, and a request is
    addressed by nothing else.
    """

    fault: JsonRpcFault
    id: JsonRpcId | None = None


@dataclass(frozen=True, slots=True)
class EncodedFrame:
    """One message spelled as the exact bytes that carry it, newline included."""

    data: bytes


@dataclass(frozen=True, slots=True)
class UnsendableFrame:
    """This message cannot be spelled inside the bound it would have to fit."""


type IncomingFrame = (
    JsonRpcRequest
    | JsonRpcNotification
    | JsonRpcAnswer
    | JsonRpcFailure
    | JsonRpcProtocolFault
)
type OutgoingMessage = (
    JsonRpcRequest | JsonRpcNotification | JsonRpcResponse | JsonRpcError
)


def _an_id(value: object) -> TypeGuard[JsonRpcId]:
    """Text or a whole number, which is every id this protocol admits."""

    return type(value) is str or type(value) is int


def _refused_call(payload: JsonObject) -> JsonRpcProtocolFault:
    """This frame refused as a message, addressed by the id it owes an answer to."""

    identifier = payload.get("id")
    if isinstance(payload.get("method"), str) and _an_id(identifier):
        return JsonRpcProtocolFault(JsonRpcFault.NOT_A_MESSAGE, identifier)
    return JsonRpcProtocolFault(JsonRpcFault.NOT_A_MESSAGE)


def _payload(message: OutgoingMessage) -> JsonObject:
    match message:
        case JsonRpcRequest(identifier, method, params):
            return {
                "jsonrpc": JSON_RPC_VERSION,
                "id": identifier,
                "method": method,
                "params": params,
            }
        case JsonRpcNotification(method, params):
            return {"jsonrpc": JSON_RPC_VERSION, "method": method, "params": params}
        case JsonRpcResponse(identifier, result):
            return {"jsonrpc": JSON_RPC_VERSION, "id": identifier, "result": result}
        case JsonRpcError(identifier, code, message_text):
            return {
                "jsonrpc": JSON_RPC_VERSION,
                "id": identifier,
                "error": {"code": code, "message": message_text},
            }
        case _ as unreachable:
            assert_never(unreachable)


def _spelled_string_pieces(text: str, slice_characters: int) -> Iterator[str]:
    """One string leaf, escaped in slices no wider than `slice_characters`.

    Escaping is context-free per character, so slicing on character
    boundaries and escaping each slice alone reproduces exactly what escaping
    the whole string would have written -- without ever holding the whole
    string's escaped form at once. Each slice below holds
    `_TRANSIENT_BUFFERS_PER_SLICE` buffers at once; see that constant for the
    transient bound it proves.
    """

    yield '"'
    for start in range(0, len(text), slice_characters):
        slice_ = text[start : start + slice_characters]
        yield json.dumps(slice_, ensure_ascii=True)[1:-1]
    yield '"'


def _spelled_pieces(value: JsonValue, slice_characters: int) -> Iterator[str]:
    """This value's own JSON spelling, one bounded piece at a time.

    Byte-identical to `json.dumps(value, separators=(",", ":"),
    ensure_ascii=True)`, but no piece here can grow past `slice_characters`
    code points before escaping: a bounded reader never has to hold an
    unbounded write.
    """

    match value:
        case None:
            yield "null"
        case bool():
            yield "true" if value else "false"
        case int() | float():
            yield json.dumps(value)
        case str():
            yield from _spelled_string_pieces(value, slice_characters)
        case Mapping():
            yield "{"
            for index, (key, item) in enumerate(value.items()):
                if index:
                    yield ","
                yield from _spelled_string_pieces(key, slice_characters)
                yield ":"
                yield from _spelled_pieces(item, slice_characters)
            yield "}"
        case Sequence():
            yield "["
            for index, item in enumerate(value):
                if index:
                    yield ","
                yield from _spelled_pieces(item, slice_characters)
            yield "]"
        case _ as unreachable:
            assert_never(unreachable)


def _spelled_within(payload: JsonObject, maximum_bytes: int) -> bytes | None:
    """This payload's own bytes plus its frame separator, or nothing once they
    would pass the bound.

    Counted as each bounded piece is spelled rather than after: escaping can
    multiply a text several times over, so a payload measured before it is
    spelled is not measured at all -- and spelling it whole to find out is
    exactly the buffer the bound exists to refuse. A piece with more
    characters than there is room left cannot fit however it encodes, so it is
    refused unencoded, before it would ever be appended. Every already-spelled
    piece is folded into one `bytearray` as soon as it is measured, rather
    than kept apart in a list: a list's own per-piece pointer and object
    overhead is not bounded by this frame's own byte bound the way one
    buffer's contents are, and JSON fragmented into many small pieces could
    otherwise cost far more than `maximum_bytes` in bookkeeping alone. Each
    string leaf is sliced to
    `max(1, maximum_bytes // _MAXIMUM_JSON_ESCAPE_WIDTH_BYTES)` code points
    first, so even a string this payload never gets to spell in full holds
    only payload-sized transient storage, bounded by
    `5 × maximum_bytes + fixed runtime/object overhead` (below the escape
    width, `48 + maximum_bytes + fixed overhead` instead) -- see that
    constant for the buffers counted into it.
    """

    frame_bytes = maximum_bytes - len(_FRAME_SEPARATOR)
    slice_characters = _string_slice_characters(maximum_bytes)
    buffer = bytearray()
    for piece in _spelled_pieces(payload, slice_characters):
        if len(buffer) + len(piece) > frame_bytes:
            return None
        buffer += piece.encode()
    buffer += _FRAME_SEPARATOR
    return bytes(buffer)


@dataclass
class NewlineJsonRpc:
    """One conversation's framing: bytes in, named messages out, ids of its own.

    It holds the unfinished tail of what has arrived, the ids it has minted and
    not yet seen answered, and nothing else. Once framing is lost -- a line
    longer than a frame may be -- it reads nothing further: a stream whose
    boundaries are gone cannot be resynchronised, and guessing where the next
    one starts is how a parser invents messages nobody sent.
    """

    maximum_frame_bytes: int
    _tail: bytes = field(default=b"", init=False)
    _awaited: dict[JsonRpcId, str] = field(default_factory=dict, init=False)
    _next_request_id: int = field(default=1, init=False)
    _framing_lost: bool = field(default=False, init=False)

    def ask(self, method: str, params: JsonObject) -> JsonRpcRequest:
        """Mint the next request, whose answer this codec will then expect."""

        identifier = self._next_request_id
        self._next_request_id += 1
        self._awaited[identifier] = method
        return JsonRpcRequest(identifier, method, params)

    def receive(self, chunk: bytes) -> tuple[IncomingFrame, ...]:
        """Read exactly these bytes, and name every frame they completed."""

        if self._framing_lost:
            return ()
        frames: list[IncomingFrame] = []
        buffered = self._tail + chunk
        while (newline := buffered.find(_FRAME_SEPARATOR)) >= 0:
            frame, buffered = buffered[:newline], buffered[newline + 1 :]
            if len(frame) > self.maximum_frame_bytes:
                return self._framing_is_lost(frames)
            frames.append(self._decoded(frame))
        if len(buffered) > self.maximum_frame_bytes:
            return self._framing_is_lost(frames)
        self._tail = buffered
        return tuple(frames)

    def incomplete_frame(self) -> bytes:
        """Whatever stands after the last newline, which no message completed."""

        return self._tail

    def encode(
        self, message: OutgoingMessage, maximum_bytes: int
    ) -> EncodedFrame | UnsendableFrame:
        """Spell this message as one line, or refuse it against its own bound.

        The bound is the caller's because a reply, a request and a prepared
        cancellation are each held to a different one, and only the caller
        knows which of them it is spelling. What is measured is the finished
        line: the escaping and the envelope are part of what has to be written.
        A caller carrying a payload of unknown width refuses it against the
        same bound before it composes the message, because the encoder never
        holds more than payload-sized transient storage, bounded by
        `5 × maximum_bytes + fixed runtime/object overhead` however long
        that string is (below the escape width, `48 + maximum_bytes + fixed
        overhead` instead -- see that constant).
        """

        data = _spelled_within(_payload(message), maximum_bytes)
        if data is None:
            return UnsendableFrame()
        return EncodedFrame(data)

    def _framing_is_lost(
        self, frames: list[IncomingFrame]
    ) -> tuple[IncomingFrame, ...]:
        self._framing_lost = True
        self._tail = b""
        frames.append(JsonRpcProtocolFault(JsonRpcFault.OVERSIZE_FRAME))
        return tuple(frames)

    def _decoded(self, frame: bytes) -> IncomingFrame:
        try:
            payload = json.loads(frame)
        except ValueError:
            return JsonRpcProtocolFault(JsonRpcFault.UNPARSEABLE)
        if not isinstance(payload, dict):
            return JsonRpcProtocolFault(JsonRpcFault.NOT_A_MESSAGE)
        if payload.get("jsonrpc") != JSON_RPC_VERSION:
            return _refused_call(payload)
        method = payload.get("method")
        if isinstance(method, str):
            return self._called(payload, method)
        return self._responded(payload)

    def _called(self, payload: JsonObject, method: str) -> IncomingFrame:
        params = payload.get("params", NO_PARAMS)
        if not isinstance(params, Mapping):
            return _refused_call(payload)
        if "id" not in payload:
            return JsonRpcNotification(method, params)
        identifier = payload["id"]
        if not _an_id(identifier):
            return JsonRpcProtocolFault(JsonRpcFault.NOT_A_MESSAGE)
        return JsonRpcRequest(identifier, method, params)

    def _responded(self, payload: JsonObject) -> IncomingFrame:
        identifier = payload.get("id")
        if not _an_id(identifier) or identifier not in self._awaited:
            return JsonRpcProtocolFault(JsonRpcFault.UNEXPECTED_RESPONSE)
        if ("result" in payload) == ("error" in payload):
            return JsonRpcProtocolFault(JsonRpcFault.MALFORMED_RESPONSE)
        result = payload.get("result")
        if isinstance(result, Mapping):
            return JsonRpcAnswer(self._awaited.pop(identifier), result)
        error = payload.get("error")
        if isinstance(error, Mapping):
            return self._refused(identifier, error)
        return JsonRpcProtocolFault(JsonRpcFault.MALFORMED_RESPONSE)

    def _refused(self, identifier: JsonRpcId, error: JsonObject) -> IncomingFrame:
        code = error.get("code")
        message = error.get("message")
        if type(code) is not int or not isinstance(message, str):
            return JsonRpcProtocolFault(JsonRpcFault.MALFORMED_RESPONSE)
        return JsonRpcFailure(self._awaited.pop(identifier), code, message)


def rendered(payload: JsonObject) -> str:
    """One message spelled as text, for a reader rather than for a provider.

    What no vocabulary could classify is still evidence, and evidence has to be
    readable: the object is written back exactly as it arrived, and whoever
    keeps it owns its width and its redaction.
    """

    return json.dumps(payload, separators=_COMPACT_SEPARATORS)
