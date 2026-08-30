from __future__ import annotations

import struct

from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.hashing import (
    FrameDomainMismatch,
    FrameTruncated,
    frame,
    unframe,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame, RunnerSessionMessage
from atelier2.contracts.runner_terminal_evidence_codec import (
    MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES,
)

RUNNER_SESSION_BODY_LENGTH_PREFIX_BYTES = 4
"""Width of the big-endian uint32 every wire frame prefixes its body with."""

TERMINAL_RECORD_ENVELOPE_BYTES = 404
"""What a TERMINAL_RECORD body spends on everything except the record itself.

One fixed number, because `RunnerSessionFrame` pins every identity a session
frame carries: 64-hex attempt, request and manifest hashes, and exactly 43
base64url characters of generation and invocation token. It is pinned against
the production encoder in `tests/domain/test_runner_session_codec.py` instead
of being recomputed here, so the frame layout keeps its one owner.
"""

MAXIMUM_RUNNER_SESSION_BODY_BYTES = (
    MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES + TERMINAL_RECORD_ENVELOPE_BYTES
)
"""The largest session body, derived from the record it has to carry (#900).

`TERMINAL_RECORD` hands over the journal's canonical record verbatim as its one
payload field, so a body bound below the record bound lets the Runner durably
store evidence it can never deliver. That gap does not show up when the record
is written; it shows up on the rarest run as `runner-session-oversized`, which
reads like a transport fault and is a contract fault. Deriving this bound from
the record bound is what stops the two numbers drifting apart again -- raising
the record bound now raises this one with it.
"""

MAXIMUM_RUNNER_SESSION_WIRE_FRAME_BYTES = (
    MAXIMUM_RUNNER_SESSION_BODY_BYTES + RUNNER_SESSION_BODY_LENGTH_PREFIX_BYTES
)
"""The body and its length prefix: the largest frame a peer may put on the wire."""
# Position of the resolved auth reference in the PREPARE payload.
# `encode_runner_prepare_payload` lays out two more fields after it -- the
# declared output schema and the pinned turn limit (#672) -- so this is no
# longer the payload's last field, only the last one every PREPARE carries
# unconditionally.
PREPARE_AUTH_REFERENCE_FIELD = 18

# The whole session protocol's own identity, not just PREPARE's field count:
# #672 widened PREPARE from 19 to 21 fields, so a wire frame's domain now
# names the revision that shape belongs to. Public (not `_`-prefixed) because
# `contracts/runner_manifests.py`'s attested session-protocol field reuses
# this exact constant rather than carrying its own copy -- a second literal
# is exactly what let the manifest's copy go stale when #672 first bumped
# this one, caught only on review. `_RETIRED_FRAME_DOMAIN_V1` is the domain
# every pre-#672 peer still encodes -- a real, well-formed frame this decoder
# no longer serves, not corrupt bytes. Naming it here, rather than folding it
# into "unrecognized domain", is what lets decode answer a stale peer with an
# explicit revision refusal instead of the generic malformed one (ADR 0009
# amendment, #672).
RUNNER_SESSION_FRAME_DOMAIN = b"runner-session/v2"
_RETIRED_FRAME_DOMAIN_V1 = b"runner-session/v1"
_RETIRED_FRAME_HEADER_V1 = frame(_RETIRED_FRAME_DOMAIN_V1.decode("ascii"))


class RunnerSessionCodecError(ValueError):
    """The peer supplied no canonical, bounded session frame."""


def runner_session_body_length(prefix: bytes) -> int:
    """Refuse a zero or over-limit length before the body is allocated."""
    if len(prefix) != RUNNER_SESSION_BODY_LENGTH_PREFIX_BYTES:
        raise RunnerSessionCodecError("runner-session-truncated")
    body_length = struct.unpack(">I", prefix)[0]
    if body_length == 0 or body_length > MAXIMUM_RUNNER_SESSION_BODY_BYTES:
        raise RunnerSessionCodecError("runner-session-oversized")
    return body_length


def encode_runner_session_frame(session: RunnerSessionFrame) -> bytes:
    body = frame(RUNNER_SESSION_FRAME_DOMAIN.decode("ascii"), *session.fields())
    if len(body) > MAXIMUM_RUNNER_SESSION_BODY_BYTES:
        raise RunnerSessionCodecError("runner-session-oversized")
    return struct.pack(">I", len(body)) + body


def decode_runner_session_frame(wire: bytes) -> RunnerSessionFrame:
    prefix_bytes = RUNNER_SESSION_BODY_LENGTH_PREFIX_BYTES
    if len(wire) < prefix_bytes:
        raise RunnerSessionCodecError("runner-session-truncated")
    body_length = runner_session_body_length(wire[:prefix_bytes])
    if len(wire) != body_length + prefix_bytes:
        raise RunnerSessionCodecError("runner-session-truncated")
    fields = _decode_frame_body(wire[prefix_bytes:])
    if len(fields) < 8:
        raise RunnerSessionCodecError("runner-session-truncated")
    try:
        message = RunnerSessionMessage(_ascii(fields[0]))
    except (UnicodeDecodeError, ValueError) as error:
        raise RunnerSessionCodecError("runner-session-message-unknown") from error
    try:
        if fields[1] != b"1" or len(fields[2]) != 8:
            raise RunnerSessionCodecError("runner-session-noncanonical")
        session = RunnerSessionFrame(
            message=message,
            sequence=struct.unpack(">Q", fields[2])[0],
            binding=RunnerGenerationBinding(
                AgentAttemptId(_ascii(fields[3])),
                AgentExecutionRequestHash(_ascii(fields[4])),
                RunnerGenerationId(_ascii(fields[5])),
                RunnerManifestId(_ascii(fields[7])),
            ),
            invocation_id=RunnerInvocationId(_ascii(fields[6])),
            payload=tuple(fields[8:]),
        )
    except RunnerSessionCodecError:
        raise
    except (TypeError, UnicodeDecodeError, ValueError) as error:
        raise RunnerSessionCodecError("runner-session-noncanonical") from error
    if encode_runner_session_frame(session) != wire:
        raise RunnerSessionCodecError("runner-session-noncanonical")
    return session


def _decode_frame_body(body: bytes) -> tuple[bytes, ...]:
    """Read the session frame's fields, naming every defect in session terms."""

    if body.startswith(_RETIRED_FRAME_HEADER_V1):
        raise RunnerSessionCodecError("runner-session-incompatible-revision")
    try:
        return unframe(body, RUNNER_SESSION_FRAME_DOMAIN.decode("ascii"))
    except FrameTruncated as error:
        declared = error.declared_field_length
        raise RunnerSessionCodecError(
            "runner-session-oversized"
            if declared is not None and declared > MAXIMUM_RUNNER_SESSION_BODY_BYTES
            else "runner-session-truncated"
        ) from error
    except FrameDomainMismatch as error:
        raise RunnerSessionCodecError("runner-session-noncanonical") from error


def _ascii(value: bytes) -> str:
    return value.decode("ascii")
