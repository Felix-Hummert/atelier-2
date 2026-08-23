"""Production owner of the Core-side socket transport every Core composition drives.

TLS listener construction, the accept/reconnect bound, frame codec I/O over the
wire, peer-leaf validation, and driving one connection's frames until RELEASED
all live here once, so no Core composition -- witness or production -- repeats
this transport logic (`#540` C-1). Every timing and reconnect bound a caller
needs is injected rather than owned here, and the one behavioral hook a caller
may need at STARTED (`on_started`) is injected too, so this module stays free
of any caller's own scenario vocabulary.
"""

from __future__ import annotations

import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ObjectIdentifier

from atelier2.adapters.runner_tls import validate_peer_certificate
from atelier2.contracts.runner_session_codec import (
    decode_runner_session_frame,
    encode_runner_session_frame,
    runner_session_body_length,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame, RunnerSessionMessage


class RunnerSessionChannel(Protocol):
    """The narrow byte boundary Core's frame codec drives -- an accepted
    `ssl.SSLSocket` in production, any object with the same shape in a test."""

    def recv(self, buffer_size: int, /) -> bytes: ...

    def sendall(self, data: bytes, /) -> None: ...


class RunnerSessionAdvancer(Protocol):
    """The narrow ordering-fence surface this transport drives -- the real
    `atelier2.application.run_runner_session.CoreRunnerSession` in production,
    satisfied structurally rather than by import so this module names no
    session implementation of its own. `cancel` is never called by this
    transport itself; it is here only so an injected `on_started` hook may
    reach it on the one `session` this loop hands back to it."""

    def accept(self, frame: RunnerSessionFrame) -> RunnerSessionFrame | None: ...

    def accept_terminal_record(
        self, frame: RunnerSessionFrame
    ) -> RunnerSessionFrame | None: ...

    def cancel(self) -> RunnerSessionFrame: ...


def read_frame(connection: RunnerSessionChannel) -> RunnerSessionFrame:
    prefix = _read_exact(connection, 4)
    length = runner_session_body_length(prefix)
    return decode_runner_session_frame(prefix + _read_exact(connection, length))


def _read_exact(connection: RunnerSessionChannel, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = connection.recv(remaining)
        if not chunk:
            raise ConnectionError("runner closed its one session")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def write_frame(connection: RunnerSessionChannel, frame: RunnerSessionFrame) -> None:
    connection.sendall(encode_runner_session_frame(frame))


def bind_session_listener(port: int, accept_timeout_seconds: float) -> socket.socket:
    """The one TCP listener a Core composition accepts its Runner's TLS
    connections on, bounded so an unreachable or crashed-and-never-restarted
    Runner fails this composition loudly instead of hanging its caller --
    a launcher's container wait on it, in the witness's case -- forever."""
    server = socket.create_server(("0.0.0.0", port), reuse_port=False)
    server.settimeout(accept_timeout_seconds)
    return server


@dataclass(frozen=True, slots=True)
class RunnerPeerPin:
    """The exact Runner leaf a Core composition's accept loop must see, every
    reconnect: the same authenticated identity the issuer handed to this one
    invocation's one Runner, never a fresh or foreign leaf."""

    ca_pem: bytes
    expected_uri: str
    expected_eku: ObjectIdentifier
    expected_leaf_der: bytes


def accept_pinned_runner(
    server: socket.socket, context: ssl.SSLContext, pin: RunnerPeerPin
) -> ssl.SSLSocket:
    """Accept the next connection, or raise if its peer is not the pinned leaf."""
    raw_connection, _peer_address = server.accept()
    connection = context.wrap_socket(raw_connection, server_side=True)
    try:
        presented = connection.getpeercert(binary_form=True)
        if presented is None:
            raise RuntimeError("TLS client did not present an authenticated leaf")
        presented_pem = x509.load_der_x509_certificate(presented).public_bytes(
            serialization.Encoding.PEM
        )
        validate_peer_certificate(
            presented_pem,
            pin.ca_pem,
            expected_dns_name=None,
            expected_uri=pin.expected_uri,
            expected_eku=pin.expected_eku,
        )
        if presented != pin.expected_leaf_der:
            raise RuntimeError("Runner peer leaf differs from the issuer handoff")
    except BaseException:
        connection.close()
        raise
    return connection


def drive_until_released_or_dropped(
    connection: RunnerSessionChannel,
    session: RunnerSessionAdvancer,
    on_started: Callable[[RunnerSessionAdvancer], RunnerSessionFrame | None]
    | None = None,
) -> bool:
    """Drive frames on one connection until RELEASED, or the peer disconnects.

    `session` is the one long-lived, in-memory ordering fence for this
    invocation; it survives a dropped connection unmodified, so a resumed
    Runner replaying every already-accepted sequence is answered from
    `session`'s own idempotent cache instead of re-advancing the durable store.

    `on_started`, when given, is asked for one frame to send back the moment a
    STARTED frame arrives -- the one behavioral hook a caller may need here
    that this transport loop itself never decides.
    """
    while True:
        try:
            frame = read_frame(connection)
        except (ConnectionError, ssl.SSLError):
            # A hard crash never sends a TLS close_notify, so the OS closing
            # that fd out from under the peer can surface here as a plain
            # closed connection or as an abrupt SSL EOF depending on platform
            # and timing -- both mean the same thing: this connection is
            # gone, and a resumed Runner may still reconnect.
            return False
        response = (
            session.accept_terminal_record(frame)
            if frame.message is RunnerSessionMessage.TERMINAL_RECORD
            else session.accept(frame)
        )
        if response is not None:
            write_frame(connection, response)
        if on_started is not None and frame.message is RunnerSessionMessage.STARTED:
            triggered = on_started(session)
            if triggered is not None:
                write_frame(connection, triggered)
        if frame.message is RunnerSessionMessage.RELEASED:
            return True


def accept_and_drive_session(
    server: socket.socket,
    context: ssl.SSLContext,
    pin: RunnerPeerPin,
    session_for_first_connection: Callable[[], RunnerSessionAdvancer],
    maximum_connection_attempts: int,
    on_started: Callable[[RunnerSessionAdvancer], RunnerSessionFrame | None]
    | None = None,
) -> None:
    """Accept up to `maximum_connection_attempts` pinned Runner connections and
    drive each on the one session this invocation ever constructs, until
    RELEASED.

    `session_for_first_connection` is called exactly once, against the first
    accepted connection -- neither the Runner's identity nor this invocation
    changes on a resumed reconnect, and the session it returns is the one
    long-lived ordering fence every later connection keeps driving.
    """
    session: RunnerSessionAdvancer | None = None
    released = False
    for _ in range(maximum_connection_attempts):
        try:
            connection = accept_pinned_runner(server, context, pin)
        except TimeoutError as error:
            raise RuntimeError(
                "Core did not see the Runner connect within the accept bound"
            ) from error
        with connection:
            if session is None:
                session = session_for_first_connection()
            released = drive_until_released_or_dropped(connection, session, on_started)
        if released:
            break
    if not released:
        raise RuntimeError("runner did not reach RELEASED within the reconnect bound")
