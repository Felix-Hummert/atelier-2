from __future__ import annotations

import struct

import pytest

from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.runner_session_codec import (
    MAXIMUM_RUNNER_SESSION_BODY_BYTES,
    MAXIMUM_RUNNER_SESSION_WIRE_FRAME_BYTES,
    RunnerSessionCodecError,
    decode_runner_session_frame,
    encode_runner_session_frame,
)
from atelier2.contracts.runner_sessions import (
    RUNNER_SESSION_REFUSAL_CODES,
    RunnerSessionFrame,
    RunnerSessionMessage,
)


def _frame(
    message: RunnerSessionMessage = RunnerSessionMessage.INVOCATION_OFFER,
) -> RunnerSessionFrame:
    return RunnerSessionFrame(
        message=message,
        sequence=1,
        binding=RunnerGenerationBinding(
            AgentAttemptId("a" * 64),
            AgentExecutionRequestHash("b" * 64),
            RunnerGenerationId("A" * 43),
            RunnerManifestId("c" * 64),
        ),
        invocation_id=RunnerInvocationId("B" * 43),
        payload=(),
    )


def test_session_frame_round_trips_as_one_canonical_wire_record() -> None:
    encoded = encode_runner_session_frame(_frame())

    assert decode_runner_session_frame(encoded) == _frame()
    assert len(encoded) <= MAXIMUM_RUNNER_SESSION_WIRE_FRAME_BYTES


@pytest.mark.parametrize(
    "wire",
    (
        b"\x00\x00\x00\x00",
        struct.pack(">I", MAXIMUM_RUNNER_SESSION_BODY_BYTES + 1),
        b"\x00\x00\x00\x01x",
    ),
)
@pytest.mark.proves("runner-session-bounded")
def test_session_decoder_refuses_invalid_length_before_a_frame_is_constructed(
    wire: bytes,
) -> None:
    with pytest.raises(RunnerSessionCodecError):
        decode_runner_session_frame(wire)


def test_session_refuses_an_extra_payload_field_for_a_closed_message() -> None:
    with pytest.raises(ValueError, match="payload"):
        RunnerSessionFrame(
            message=RunnerSessionMessage.INVOCATION_OFFER,
            sequence=1,
            binding=_frame().binding,
            invocation_id=_frame().invocation_id,
            payload=(b"not-permitted",),
        )


def test_session_decoder_names_oversized_and_truncated_refusals() -> None:
    with pytest.raises(RunnerSessionCodecError, match="runner-session-oversized"):
        decode_runner_session_frame(b"\x00\x00\x00\x00")
    with pytest.raises(RunnerSessionCodecError, match="runner-session-truncated"):
        decode_runner_session_frame(b"\x00")


def test_session_decoder_refuses_an_unknown_message_tag() -> None:
    encoded = bytearray(encode_runner_session_frame(_frame()))
    index = encoded.index(b"INVOCATION_OFFER")
    encoded[index : index + 16] = b"NOT_A_SESSION_MS"
    with pytest.raises(RunnerSessionCodecError, match="runner-session-message-unknown"):
        decode_runner_session_frame(bytes(encoded))


def test_cancel_frame_round_trips_with_none_replacement() -> None:
    frame = RunnerSessionFrame(
        RunnerSessionMessage.CANCEL,
        3,
        _frame().binding,
        _frame().invocation_id,
        (
            b"runner-candidate/one",
            b"runner-candidate-cancel",
            (2).to_bytes(8, "big"),
            b"NONE",
        ),
    )

    assert decode_runner_session_frame(encode_runner_session_frame(frame)) == frame


def test_refusal_vocabulary_is_closed() -> None:
    assert "runner-session-message-unknown" in RUNNER_SESSION_REFUSAL_CODES
