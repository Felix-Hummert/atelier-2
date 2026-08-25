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
from atelier2.contracts.hashing import frame
from atelier2.contracts.runner_sessions import RunnerSessionFrame, RunnerSessionMessage

MAXIMUM_RUNNER_SESSION_BODY_BYTES = 1_078_291
MAXIMUM_RUNNER_SESSION_WIRE_FRAME_BYTES = 1_078_295
# Position of the resolved auth reference in the PREPARE payload.
# `encode_runner_prepare_payload` lays out two more fields after it -- the
# declared output schema and the pinned turn limit (#672) -- so this is no
# longer the payload's last field, only the last one every PREPARE carries
# unconditionally.
PREPARE_AUTH_REFERENCE_FIELD = 18
_FRAME_PREFIX = b"ATELIER2\x00"

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


class RunnerSessionCodecError(ValueError):
    """The peer supplied no canonical, bounded session frame."""


def runner_session_body_length(prefix: bytes) -> int:
    """Refuse a zero or over-limit length before the body is allocated."""
    if len(prefix) != 4:
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
    if len(wire) < 4:
        raise RunnerSessionCodecError("runner-session-truncated")
    body_length = runner_session_body_length(wire[:4])
    if len(wire) != body_length + 4:
        raise RunnerSessionCodecError("runner-session-truncated")
    fields = _decode_frame_body(wire[4:])
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
    if not body.startswith(_FRAME_PREFIX):
        raise RunnerSessionCodecError("runner-session-noncanonical")
    cursor = len(_FRAME_PREFIX)
    if len(body) < cursor + 4:
        raise RunnerSessionCodecError("runner-session-truncated")
    domain_length = struct.unpack(">I", body[cursor : cursor + 4])[0]
    cursor += 4
    domain_end = cursor + domain_length
    if domain_end > len(body):
        raise RunnerSessionCodecError("runner-session-noncanonical")
    domain = body[cursor:domain_end]
    if domain == _RETIRED_FRAME_DOMAIN_V1:
        raise RunnerSessionCodecError("runner-session-incompatible-revision")
    if domain != RUNNER_SESSION_FRAME_DOMAIN:
        raise RunnerSessionCodecError("runner-session-noncanonical")
    cursor = domain_end
    fields: list[bytes] = []
    while cursor < len(body):
        if len(body) - cursor < 8:
            raise RunnerSessionCodecError("runner-session-truncated")
        field_length = struct.unpack(">Q", body[cursor : cursor + 8])[0]
        cursor += 8
        if field_length > MAXIMUM_RUNNER_SESSION_BODY_BYTES:
            raise RunnerSessionCodecError("runner-session-oversized")
        field_end = cursor + field_length
        if field_end > len(body):
            raise RunnerSessionCodecError("runner-session-truncated")
        fields.append(body[cursor:field_end])
        cursor = field_end
    return tuple(fields)


def _ascii(value: bytes) -> str:
    return value.decode("ascii")
