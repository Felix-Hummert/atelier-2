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
from atelier2.contracts.hashing import frame as hashing_frame
from atelier2.contracts.runner_session_codec import (
    MAXIMUM_RUNNER_SESSION_BODY_BYTES,
    MAXIMUM_RUNNER_SESSION_WIRE_FRAME_BYTES,
    RUNNER_SESSION_BODY_LENGTH_PREFIX_BYTES,
    TERMINAL_RECORD_ENVELOPE_BYTES,
    RunnerSessionCodecError,
    decode_runner_session_frame,
    encode_runner_session_frame,
    runner_session_body_length,
)
from atelier2.contracts.runner_sessions import (
    RUNNER_SESSION_REFUSAL_CODES,
    RunnerSessionFrame,
    RunnerSessionMessage,
)
from atelier2.contracts.runner_terminal_evidence_codec import (
    MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES,
)


def _frame(
    message: RunnerSessionMessage = RunnerSessionMessage.INVOCATION_OFFER,
    payload: tuple[bytes, ...] = (),
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
        payload=payload,
    )


def _terminal_record_frame(record: bytes) -> bytes:
    return encode_runner_session_frame(
        _frame(RunnerSessionMessage.TERMINAL_RECORD, (record,))
    )


def test_a_terminal_record_body_spends_one_fixed_width_on_its_envelope() -> None:
    """`RunnerSessionFrame` pins every identity it carries, so what a
    TERMINAL_RECORD costs beside its record is one number and not a range."""
    carrying_nothing = _terminal_record_frame(b"")

    envelope = len(carrying_nothing) - RUNNER_SESSION_BODY_LENGTH_PREFIX_BYTES

    assert envelope == TERMINAL_RECORD_ENVELOPE_BYTES


def test_a_session_body_carries_the_largest_record_the_journal_may_hold() -> None:
    """The record bound and the session bound answer one question between them:
    a record the journal admits must reach Core. The largest such record fills
    a TERMINAL_RECORD body exactly -- an envelope that grew, or a record bound
    raised without its transport, breaks this rather than the rarest run."""
    largest = _terminal_record_frame(
        b"x" * MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES
    )

    assert len(largest) - RUNNER_SESSION_BODY_LENGTH_PREFIX_BYTES == (
        MAXIMUM_RUNNER_SESSION_BODY_BYTES
    )
    assert len(largest) == MAXIMUM_RUNNER_SESSION_WIRE_FRAME_BYTES


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


def test_session_decoder_names_a_retired_wire_revision_by_its_own_code() -> None:
    """A frame built under the pre-#672 `runner-session/v1` domain is a real,
    well-formed frame this decoder no longer serves -- decode must answer it
    by its own name, not fold it into the generic malformed-bytes refusal a
    truly corrupt frame gets (`test_session_decoder_refuses_an_unknown_message_tag`)."""
    retired_body = hashing_frame("runner-session/v1", *_frame().fields())
    retired_wire = struct.pack(">I", len(retired_body)) + retired_body

    with pytest.raises(
        RunnerSessionCodecError, match="runner-session-incompatible-revision"
    ):
        decode_runner_session_frame(retired_wire)


@pytest.mark.proves("runner-session-bounded")
def test_session_length_prefix_is_refused_before_the_body_is_read() -> None:
    with pytest.raises(RunnerSessionCodecError, match="runner-session-oversized"):
        runner_session_body_length(b"\x00\x00\x00\x00")
    with pytest.raises(RunnerSessionCodecError, match="runner-session-oversized"):
        runner_session_body_length(
            struct.pack(">I", MAXIMUM_RUNNER_SESSION_BODY_BYTES + 1)
        )
    with pytest.raises(RunnerSessionCodecError, match="runner-session-truncated"):
        runner_session_body_length(b"\x00")
