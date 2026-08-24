"""The one Core-transport seam `application` may not import directly (`#540` C-3.4).

`atelier2.application.execute_agent_attempt_on_runner` drives one Attempt's
Runner session to RELEASED, but the TLS listener bind, the pinned accept, and
the frame-drive loop are all `atelier2.adapters.runner_core_transport` -- an
adapter, and `application` may not import one. This module is the caller-
supplied seam that closes the gap: given the peer material a launcher already
produced for this Attempt (`atelier2.ports.runner_leases.RunnerPeerMaterial`)
and the durable generation binding Core selected, it derives the one
invocation that peer leaf names, validates the leaf against the declared CA,
and drives its frames to RELEASED on the real transport -- exactly what
`tests/witness/runner_candidate_core.py`'s `main` does by hand, reusable
rather than repeated by every Core composition.
"""

from __future__ import annotations

import ssl
from collections.abc import Callable
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from atelier2.adapters.runner_core_transport import (
    RunnerPeerPin,
    RunnerSessionAdvancer,
    accept_and_drive_session,
    bind_session_listener,
)
from atelier2.adapters.runner_tls import (
    CORE_SESSION_PORT,
    invocation_from_runner_uri,
    runner_uri_for_invocation,
    sole_peer_uri,
    validate_peer_certificate,
)
from atelier2.contracts.agent_attempts import (
    RunnerGenerationBinding,
    RunnerInvocationId,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame
from atelier2.ports.runner_leases import RunnerPeerMaterial


@dataclass(frozen=True, slots=True)
class RunnerLeaseSessionListener:
    """One Attempt's Core-side session listener.

    Bind `CORE_SESSION_PORT`, accept exactly the pinned Runner this Attempt's
    launcher-issued peer leaf names, and drive its frames to RELEASED -- or
    raise, if none connects within `accept_timeout_seconds`. `server_context`
    already carries Core's own certificate chain and its trust of `#540`
    C-3.3's console CA; this listener adds only the one-Attempt peer pin no
    other Attempt may satisfy.
    """

    server_context: ssl.SSLContext
    ca_certificate: bytes
    accept_timeout_seconds: float
    # A composer's own reconnect-bound choice, not a bare literal this seam
    # picks for it: how many pinned Runner connections one Attempt's lease may
    # spend before `accept_and_drive_session` gives up.
    maximum_connection_attempts: int

    def drive_to_released(
        self,
        binding: RunnerGenerationBinding,
        peer: RunnerPeerMaterial,
        session_for_first_connection: Callable[
            [RunnerInvocationId], RunnerSessionAdvancer
        ],
        on_started: Callable[[RunnerSessionAdvancer], RunnerSessionFrame | None]
        | None = None,
    ) -> None:
        peer_certificate = x509.load_pem_x509_certificate(peer.client_certificate)
        invocation = invocation_from_runner_uri(
            sole_peer_uri(peer_certificate), binding
        )
        expected_uri = runner_uri_for_invocation(binding, invocation)
        validate_peer_certificate(
            peer.client_certificate,
            self.ca_certificate,
            expected_dns_name=None,
            expected_uri=expected_uri,
            expected_eku=ExtendedKeyUsageOID.CLIENT_AUTH,
        )
        pin = RunnerPeerPin(
            self.ca_certificate,
            expected_uri,
            ExtendedKeyUsageOID.CLIENT_AUTH,
            peer_certificate.public_bytes(serialization.Encoding.DER),
        )
        with bind_session_listener(
            CORE_SESSION_PORT, self.accept_timeout_seconds
        ) as server:
            accept_and_drive_session(
                server,
                self.server_context,
                pin,
                lambda: session_for_first_connection(invocation),
                self.maximum_connection_attempts,
                on_started,
            )
