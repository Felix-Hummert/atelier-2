"""What newline-delimited JSON-RPC 2.0 accepts, refuses, and spells.

Every refusal here is a value the conversation above has to be able to answer
for, so each test asks what the codec *says* about a frame rather than whether
it raised: a parser that threw would end an attempt where the protocol asks for
an error frame.
"""

from __future__ import annotations

import json

import pytest

from atelier2.adapters.newline_json_rpc import (
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


def test_the_unfinished_tail_is_kept_exactly_as_it_arrived() -> None:
    codec = _codec()

    codec.receive(_line(_message(method="one", params={})) + b'{"half":')

    assert codec.incomplete_frame() == b'{"half":'


def test_evidence_of_a_message_is_the_message_as_it_arrived() -> None:
    assert rendered({"sessionUpdate": "unheard_of"}) == '{"sessionUpdate":"unheard_of"}'
