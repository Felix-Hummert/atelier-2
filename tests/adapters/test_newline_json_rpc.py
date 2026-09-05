"""What newline-delimited JSON-RPC 2.0 accepts, refuses, and spells.

Every refusal here is a value the conversation above has to be able to answer
for, so each test asks what the codec *says* about a frame rather than whether
it raised: a parser that threw would end an attempt where the protocol asks for
an error frame.
"""

from __future__ import annotations

import json
import tracemalloc

import pytest

from atelier2.adapters.newline_json_rpc import (
    _MAXIMUM_JSON_ESCAPE_WIDTH_BYTES,
    _MAXIMUM_TRANSIENT_FACTOR_OF_BOUND,
    JSON_RPC_VERSION,
    EncodedFrame,
    JsonObject,
    JsonRpcAnswer,
    JsonRpcError,
    JsonRpcFailure,
    JsonRpcFault,
    JsonRpcNotification,
    JsonRpcProtocolFault,
    JsonRpcRequest,
    JsonRpcResponse,
    JsonValue,
    NewlineJsonRpc,
    UnsendableFrame,
    rendered,
)

ROOM_FOR_ANY_FRAME_HERE = 4_096
_LARGE_ESCAPING_BOUND_BYTES = 200_000
_TINY_ESCAPING_BOUND_BYTES = 43
"""Smallest bound that still reaches the string leaf itself.

`{"jsonrpc":"2.0","id":7,"result":{"line":"` -- the opening through the
value's own quote -- is forty-two bytes on its own, plus the one reserved for
the frame separator: forty-three is the least that ever asks for a single
byte of the string this test is about, and anything smaller is refused by
the envelope alone.
"""
_SLICE_BOUNDARY_BOUND_BYTES = 2_000
_SLICE_BOUNDARY_CHARACTERS = (
    _SLICE_BOUNDARY_BOUND_BYTES // _MAXIMUM_JSON_ESCAPE_WIDTH_BYTES
)


def _codec(maximum_frame_bytes: int = ROOM_FOR_ANY_FRAME_HERE) -> NewlineJsonRpc:
    return NewlineJsonRpc(maximum_frame_bytes)


def _line(payload: JsonObject) -> bytes:
    return json.dumps(payload).encode("utf-8") + b"\n"


def _message(**fields: JsonValue) -> JsonObject:
    return {"jsonrpc": JSON_RPC_VERSION, **fields}


def _asked(codec: NewlineJsonRpc, method: str = "session/prompt") -> int:
    minted = codec.ask(method, {}).id
    assert type(minted) is int
    return minted


def _peak_bytes_encoding(
    message: JsonRpcResponse, maximum_bytes: int
) -> tuple[EncodedFrame | UnsendableFrame, int]:
    """This encode call's tracemalloc peak, alongside its own result.

    A first, untraced call warms interpreter-wide caches -- CPython's
    single-character string cache, notably -- that a cold process would
    otherwise charge to this measurement once and never again: what this
    returns is the codec's own steady-state cost, not a one-time runtime
    artifact that would make the peak depend on which test ran first. It
    proves the recurring cost of every call after the process is warm, which
    is what a long-lived conversation actually pays over and over; it cannot
    mask a defect that recurs on every call, such as a fixed-width slice or a
    whole-string escape, since warming up the defective path warms it up
    exactly as it recurs.
    """

    _codec().encode(message, maximum_bytes)
    tracemalloc.start()
    try:
        result = _codec().encode(message, maximum_bytes)
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


ALLOCATOR_GRANULARITY_BYTES = 4_096
"""One page of tolerance for a tracemalloc peak against its own ceiling.

CPython's small-object allocator hands out arenas and pools in page-sized
steps, so a peak measured this way can carry up to one page of allocator
noise unrelated to the bound itself -- most visible against a ceiling this
tight, where hundreds of bytes of that noise would otherwise read as a
codec-caused excess.
"""


def _maximum_transient_bytes(maximum_bytes: int) -> int:
    """This bound's own ceiling, plus the fixed cost of an empty string leaf
    and one page of allocator noise.

    The fixed part is measured against the same shape of message -- a
    `"line"` key present but empty -- rather than guessed or measured against
    a message with no string leaf at all: the generator and iterator
    machinery this codec sets up to escape a string leaf is real, constant
    overhead independent of the bound, and belongs in the ceiling, not in the
    proportional factor this codec's contract actually bounds.
    """

    _empty_result, fixed_overhead = _peak_bytes_encoding(
        JsonRpcResponse(1, {"line": ""}), maximum_bytes
    )
    return (
        _MAXIMUM_TRANSIENT_FACTOR_OF_BOUND * maximum_bytes
        + fixed_overhead
        + ALLOCATOR_GRANULARITY_BYTES
    )


def test_a_frame_split_across_chunks_is_read_once_it_is_whole() -> None:
    codec = _codec()
    opening = _line(_message(method="session/update", params={"say": "half"}))

    while_incomplete = codec.receive(opening[:10])
    once_complete = codec.receive(opening[10:])

    assert while_incomplete == ()
    assert once_complete == (JsonRpcNotification("session/update", {"say": "half"}),)


def test_several_frames_in_one_chunk_are_read_in_the_order_they_arrived() -> None:
    codec = _codec()
    first = _line(_message(method="one", params={}))
    second = _line(_message(method="two", params={}))

    read = codec.receive(first + second)

    assert read == (JsonRpcNotification("one", {}), JsonRpcNotification("two", {}))


def test_a_frame_wider_than_its_bound_is_refused_before_it_is_decoded() -> None:
    """Unparseable bytes past the bound answer with the bound, never with a
    parse error: what refuses them is their length, which is read first."""
    codec = _codec(maximum_frame_bytes=16)

    read = codec.receive(b"{ this is not json at all }\n")

    assert read == (JsonRpcProtocolFault(JsonRpcFault.OVERSIZE_FRAME),)


def test_an_unfinished_remainder_wider_than_its_bound_is_refused_unfinished() -> None:
    codec = _codec(maximum_frame_bytes=16)

    read = codec.receive(b"{" + b"x" * 32)

    assert read == (JsonRpcProtocolFault(JsonRpcFault.OVERSIZE_FRAME),)


def test_nothing_is_read_after_the_framing_was_lost() -> None:
    codec = _codec(maximum_frame_bytes=16)
    codec.receive(b"x" * 32 + b"\n")

    assert codec.receive(_line(_message(method="session/update", params={}))) == ()
    assert codec.incomplete_frame() == b""


@pytest.mark.parametrize(
    ("frame", "fault"),
    [
        (b"{not json\n", JsonRpcFault.UNPARSEABLE),
        (b"\n", JsonRpcFault.UNPARSEABLE),
        (b"\xff\xfe\n", JsonRpcFault.UNPARSEABLE),
        (b'[{"jsonrpc":"2.0","method":"one"}]\n', JsonRpcFault.NOT_A_MESSAGE),
        (b'{"method":"one"}\n', JsonRpcFault.NOT_A_MESSAGE),
        (b'{"jsonrpc":"1.0","method":"one"}\n', JsonRpcFault.NOT_A_MESSAGE),
        (b'{"jsonrpc":"2.0","id":1.5,"method":"one"}\n', JsonRpcFault.NOT_A_MESSAGE),
        (b'{"jsonrpc":"2.0","id":true,"method":"one"}\n', JsonRpcFault.NOT_A_MESSAGE),
        (
            b'{"jsonrpc":"2.0","method":"one","params":[1]}\n',
            JsonRpcFault.NOT_A_MESSAGE,
        ),
        (b'{"jsonrpc":"2.0","id":9}\n', JsonRpcFault.UNEXPECTED_RESPONSE),
    ],
)
def test_a_frame_this_protocol_does_not_admit_is_named_rather_than_raised(
    frame: bytes, fault: JsonRpcFault
) -> None:
    assert _codec().receive(frame) == (JsonRpcProtocolFault(fault),)


def test_a_call_is_a_request_when_it_carries_an_id_and_a_notification_when_not() -> (
    None
):
    codec = _codec()

    read = codec.receive(
        _line(_message(id="a", method="one", params={"x": 1}))
        + _line(_message(method="one", params={"x": 1}))
    )

    assert read == (
        JsonRpcRequest("a", "one", {"x": 1}),
        JsonRpcNotification("one", {"x": 1}),
    )


def test_an_answer_is_named_by_the_method_this_codec_asked() -> None:
    codec = _codec()
    identifier = _asked(codec, "initialize")

    read = codec.receive(_line(_message(id=identifier, result={"ok": True})))

    assert read == (JsonRpcAnswer("initialize", {"ok": True}),)


def test_answers_out_of_order_each_name_their_own_question() -> None:
    codec = _codec()
    first = _asked(codec, "initialize")
    second = _asked(codec, "session/new")

    read = codec.receive(
        _line(_message(id=second, result={"sessionId": "s"}))
        + _line(_message(id=first, result={}))
    )

    assert read == (
        JsonRpcAnswer("session/new", {"sessionId": "s"}),
        JsonRpcAnswer("initialize", {}),
    )


def test_a_second_answer_to_one_question_is_a_protocol_fault() -> None:
    codec = _codec()
    identifier = _asked(codec)
    answer = _line(_message(id=identifier, result={}))

    read = codec.receive(answer + answer)

    assert read == (
        JsonRpcAnswer("session/prompt", {}),
        JsonRpcProtocolFault(JsonRpcFault.UNEXPECTED_RESPONSE),
    )


@pytest.mark.parametrize(
    "answer",
    [
        {"result": {}, "error": {"code": -32603, "message": "no"}},
        {},
        {"result": 5},
        {"error": {"code": "not a code", "message": "no"}},
    ],
)
def test_an_answer_that_is_neither_one_result_nor_one_refusal_is_named_as_such(
    answer: JsonObject,
) -> None:
    """A response says exactly one thing about the question it answers: reading
    a result beside an error would take the half a sender never meant."""
    codec = _codec()
    identifier = _asked(codec)

    read = codec.receive(_line(_message(id=identifier, **answer)))

    assert read == (JsonRpcProtocolFault(JsonRpcFault.MALFORMED_RESPONSE),)


def test_a_call_this_protocol_refuses_keeps_the_id_its_answer_is_owed_under() -> None:
    """The refusal of a request is addressed to that request: an id read off the
    frame is the only thing that can address it."""
    read = _codec().receive(
        b'{"jsonrpc":"2.0","id":7,"method":"one","params":[1]}\n'
        b'{"jsonrpc":"1.0","id":"a","method":"one"}\n'
    )

    assert read == (
        JsonRpcProtocolFault(JsonRpcFault.NOT_A_MESSAGE, 7),
        JsonRpcProtocolFault(JsonRpcFault.NOT_A_MESSAGE, "a"),
    )


def test_a_refusal_of_our_own_question_carries_its_method_code_and_message() -> None:
    codec = _codec()
    identifier = _asked(codec, "initialize")

    read = codec.receive(
        _line(_message(id=identifier, error={"code": -32603, "message": "no"}))
    )

    assert read == (JsonRpcFailure("initialize", -32603, "no"),)


def test_each_question_gets_an_id_of_its_own_in_this_direction() -> None:
    codec = _codec()

    assert (_asked(codec), _asked(codec)) == (1, 2)


def test_a_message_is_spelled_as_exactly_one_line() -> None:
    encoded = _codec().encode(JsonRpcResponse(7, {"content": "x"}), 128)

    assert isinstance(encoded, EncodedFrame)
    assert encoded.data.endswith(b"\n")
    assert json.loads(encoded.data) == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"content": "x"},
    }


@pytest.mark.parametrize(
    "message",
    [
        JsonRpcRequest(1, "initialize", {}),
        JsonRpcNotification("session/cancel", {"sessionId": "s"}),
        JsonRpcResponse(1, {}),
        JsonRpcError(None, -32700, "unreadable"),
    ],
)
def test_a_message_that_does_not_fit_its_bound_is_refused_with_its_envelope(
    message: JsonRpcRequest | JsonRpcNotification | JsonRpcResponse | JsonRpcError,
) -> None:
    """The bound is measured against the finished line, so the envelope and the
    escaping count: a payload that fits alone still does not fit as a frame."""
    codec = _codec()
    spelled = codec.encode(message, 4_096)
    assert isinstance(spelled, EncodedFrame)

    assert codec.encode(message, len(spelled.data) - 1) == UnsendableFrame()


def test_a_message_whose_escaping_passes_the_bound_is_refused_before_it_is_held() -> (
    None
):
    """Escaping is what a frame really costs: a payload that fits raw can still
    be a line nobody may buffer, and a codec that only learns that after
    spelling it whole has already held what the bound exists to refuse. One
    large string is the adversarial case: an encoder that escapes a string
    leaf whole, rather than in bounded slices, still allocates its full
    escaped form before this bound can see it: a non-BMP character escapes at
    this codec's own widest width, so 100,000 of them (1,200,000 escaped
    bytes) materialised in one piece peaks well past
    `_MAXIMUM_TRANSIENT_FACTOR_OF_BOUND` times this bound, while escaping them
    in slices this bound itself derives does not."""
    raw_characters = 100_000
    written = "\U0001f600" * raw_characters
    bound_far_smaller_than_the_escaped_string = _LARGE_ESCAPING_BOUND_BYTES

    refused, peak = _peak_bytes_encoding(
        JsonRpcResponse(7, {"line": written}), bound_far_smaller_than_the_escaped_string
    )

    assert refused == UnsendableFrame()
    assert peak <= _maximum_transient_bytes(bound_far_smaller_than_the_escaped_string)


def test_a_message_is_spelled_byte_identically_to_the_whole_document_encoder() -> None:
    """Escaping a string in bounded slices must write exactly what escaping the
    whole string in one call would have written: the bound changes how much is
    held at once, never what the frame says."""
    nested_shapes: list[JsonObject] = [
        {},
        {"a": 1, "b": [1, 2.5, True, False, None]},
        {"nested": {"list": [{"k": "v"}, [1, [2, 3]]]}},
        {"unicode": "héllo wörld ☃ \U0001f600", "control": "\n\t\x00\x1f"},
    ]

    for result in nested_shapes:
        encoded = _codec().encode(JsonRpcResponse(1, result), 1_000_000)
        assert isinstance(encoded, EncodedFrame)

        expected_payload: JsonObject = {
            "jsonrpc": JSON_RPC_VERSION,
            "id": 1,
            "result": result,
        }
        expected = (
            json.dumps(
                expected_payload, separators=(",", ":"), ensure_ascii=True
            ).encode()
            + b"\n"
        )
        assert encoded.data == expected


@pytest.mark.parametrize(
    "special_character",
    [
        pytest.param("\x00", id="control-character"),
        pytest.param("\U0001f600", id="non-bmp-character"),
        pytest.param("\ud800", id="lone-surrogate"),
    ],
)
@pytest.mark.parametrize(
    "length",
    [
        _SLICE_BOUNDARY_CHARACTERS - 1,
        _SLICE_BOUNDARY_CHARACTERS,
        _SLICE_BOUNDARY_CHARACTERS + 1,
    ],
    ids=["one-under-the-slice", "exactly-one-slice", "one-over-the-slice"],
)
def test_a_string_crossing_the_derived_slice_boundary_still_matches_json_dumps(
    length: int, special_character: str
) -> None:
    """The escaping slice length is derived from the frame's own bound, so a
    string whose length lands one under, exactly on, or one over that derived
    boundary must still spell exactly what `json.dumps` would have written,
    whichever slice its last character falls into."""
    text = "a" * (length - 1) + special_character
    result: JsonObject = {"line": text}

    encoded = _codec().encode(JsonRpcResponse(1, result), _SLICE_BOUNDARY_BOUND_BYTES)

    assert isinstance(encoded, EncodedFrame)
    expected_payload: JsonObject = {
        "jsonrpc": JSON_RPC_VERSION,
        "id": 1,
        "result": result,
    }
    expected = (
        json.dumps(expected_payload, separators=(",", ":"), ensure_ascii=True).encode()
        + b"\n"
    )
    assert encoded.data == expected


def test_a_tiny_bound_refuses_a_large_non_bmp_string_without_holding_it() -> None:
    """A non-BMP character is the widest single-code-point escape this codec
    ever writes, so it is the adversarial case for the slice a tiny bound
    derives: an encoder that still escaped a whole slice before measuring it
    would allocate far past a bound this small. This bound is the smallest
    that still reaches the string at all -- anything smaller is refused by
    the envelope alone, proven separately -- so it derives a slice of only a
    few code points; a fixed 1,024-character slice would still fail this
    assertion."""
    raw_characters = 100_000
    written = "\U0001f600" * raw_characters
    tiny_bound = _TINY_ESCAPING_BOUND_BYTES

    refused, peak = _peak_bytes_encoding(
        JsonRpcResponse(7, {"line": written}), tiny_bound
    )

    assert refused == UnsendableFrame()
    assert peak <= _maximum_transient_bytes(tiny_bound)


def test_the_unfinished_tail_is_kept_exactly_as_it_arrived() -> None:
    codec = _codec()

    codec.receive(_line(_message(method="one", params={})) + b'{"half":')

    assert codec.incomplete_frame() == b'{"half":'


def test_evidence_of_a_message_is_the_message_as_it_arrived() -> None:
    assert rendered({"sessionUpdate": "unheard_of"}) == '{"sessionUpdate":"unheard_of"}'
