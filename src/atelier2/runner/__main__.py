from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import socket
import ssl
import time
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from atelier2.adapters.runner_child import LandlockUnavailable, install_landlock_guard
from atelier2.adapters.runner_tls import (
    CORE_DNS_NAME,
    invocation_from_runner_uri,
    pin_tls_13,
    runner_uri_for_invocation,
    validate_peer_certificate,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.runner.identity_receiver import load_published_identity
from atelier2.runner.session import (
    CandidateScenario,
    child_allowlist,
    run_candidate_session,
)


def _binding(bootstrap: dict[str, str]) -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        AgentAttemptId(bootstrap["attempt_id"]),
        AgentExecutionRequestHash(bootstrap["request_hash"]),
        RunnerGenerationId(bootstrap["generation_id"]),
        RunnerManifestId(bootstrap["manifest_id"]),
    )


def _declared_scenario(bootstrap: dict[str, str]) -> CandidateScenario:
    return CandidateScenario(bootstrap["scenario"])


def _invocation_from_published_identity(
    identity: Path, binding: RunnerGenerationBinding
) -> RunnerInvocationId | None:
    """The invocation a prior lifetime's already-published identity fixed.

    `identity_receiver._write_identity` issues this container's mTLS identity
    exactly once, bound to one invocation's URI, and refuses to ever write a
    second time into the same destination. Once `ready` is present, this
    identity's own already-validated SAN URI is the one durable source of
    truth for which invocation to keep offering on every later reconnect --
    the terminal-evidence journal is a record of what that invocation
    *produced*, not of the invocation itself, and an empty journal after a
    crash says nothing about whether this identity was ever issued. Deriving
    from anything else here (an empty journal, a fresh mint) would make the
    mTLS load below reject bytes bound to a URI nobody asked for, stranding a
    legitimate reconnect in a permanent crash loop instead of ever reaching
    Core again.
    """
    if not (identity / "ready").is_file() or not (identity / "client.crt").is_file():
        return None
    certificate = x509.load_pem_x509_certificate((identity / "client.crt").read_bytes())
    uris = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)
    if len(uris) != 1:
        raise RuntimeError("runner-binding-san-mismatch")
    return invocation_from_runner_uri(uris[0], binding)


def _invocation_for_session(
    identity: Path, binding: RunnerGenerationBinding
) -> RunnerInvocationId:
    """This container's one invocation: an already-published identity's, or a fresh mint.

    Every durable fact from here to `RELEASED` -- the store's arm, the
    journal, the wire handshake -- is keyed to one invocation id, and the
    mTLS identity that authenticates every reconnect fixes that id
    permanently the moment it is first issued (see
    `_invocation_from_published_identity`). Minting a fresh one only ever
    happens the one time nothing has been published yet, which is also the
    one time nothing could possibly be durably armed for this generation yet
    either -- so a fresh mint here can never collide with an invocation Core
    already knows.
    """
    published = _invocation_from_published_identity(identity, binding)
    if published is not None:
        return published
    return RunnerInvocationId(secrets.token_urlsafe(32))


def _wait_for_file(path: Path, reason: str) -> None:
    deadline = time.monotonic() + 10
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not path.is_file():
        raise RuntimeError(reason)


def _wait_for_client_identity(identity: Path) -> None:
    ready = identity / "ready"
    _wait_for_file(
        ready, "external issuer did not complete the candidate identity handoff"
    )
    if (
        not (identity / "client.crt").is_file()
        or not (identity / "client.key").is_file()
    ):
        raise RuntimeError(
            "external issuer did not complete the candidate identity handoff"
        )


def _load_verified_client_identity(
    context: ssl.SSLContext, certificate: bytes, key: bytes, staging: Path
) -> None:
    """Load the exact bytes the identity reread validated, not the path again.

    A path-based load_cert_chain would reopen the volume files after
    validation, leaving a window in which different bytes could be picked up.
    An unlinked O_TMPFILE inode carries the validated bytes themselves;
    /proc/self/fd is the only way ssl can be handed pathless PEM material,
    and the descriptor is closed here before any child exists.
    """
    descriptor = os.open(staging, os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        payload = certificate + b"\n" + key
        if os.write(descriptor, payload) != len(payload):
            raise RuntimeError("runner identity staging write was short")
        context.load_cert_chain(f"/proc/self/fd/{descriptor}")
    finally:
        os.close(descriptor)


def _run_candidate_session(
    handoff: Path, identity: Path, journal_directory: Path, invocation_offer: Path
) -> int:
    for name in ("bootstrap.json", "core-peer.json", "core.crt", "ca.crt", "manifest"):
        _wait_for_file(
            handoff / name, "launcher did not copy the public bootstrap inbound"
        )
    bootstrap = json.loads(
        handoff.joinpath("bootstrap.json").read_text(encoding="utf-8")
    )
    binding = _binding(bootstrap)
    scenario = _declared_scenario(bootstrap)
    invocation = _invocation_for_session(identity, binding)
    invocation_offer.write_text(
        json.dumps(
            {"invocation_id": invocation.value}, sort_keys=True, separators=(",", ":")
        ),
        encoding="utf-8",
    )
    _wait_for_client_identity(identity)
    expected_runner_uri = runner_uri_for_invocation(binding, invocation)
    peer = json.loads(handoff.joinpath("core-peer.json").read_text(encoding="utf-8"))
    core_certificate = handoff.joinpath("core.crt").read_bytes()
    ca_certificate = handoff.joinpath("ca.crt").read_bytes()
    validate_peer_certificate(
        core_certificate,
        ca_certificate,
        expected_dns_name=CORE_DNS_NAME,
        expected_uri=peer["uri"],
        expected_eku=ExtendedKeyUsageOID.SERVER_AUTH,
    )
    client_certificate, client_key, volume_ca = load_published_identity(
        identity, expected_uri=expected_runner_uri, expected_ca=ca_certificate
    )
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH, cadata=volume_ca.decode("ascii")
    )
    pin_tls_13(context)
    _load_verified_client_identity(
        context, client_certificate, client_key, journal_directory
    )
    # The built-in hostname check would only repeat the DNS half of the manual
    # post-handshake fence below: validate_peer_certificate pins SAN DNS name,
    # SPKI-bound URI, and EKU against the bootstrap CA, and the fingerprint
    # comparison pins the exact presented leaf.
    context.check_hostname = False
    with (
        socket.create_connection((CORE_DNS_NAME, 8443), 5) as raw,
        context.wrap_socket(raw, server_hostname=CORE_DNS_NAME) as connection,
    ):
        presented = connection.getpeercert(binary_form=True)
        if presented is None:
            raise RuntimeError("Core did not present an authenticated leaf")
        presented_pem = x509.load_der_x509_certificate(presented).public_bytes(
            serialization.Encoding.PEM
        )
        validate_peer_certificate(
            presented_pem,
            ca_certificate,
            expected_dns_name=CORE_DNS_NAME,
            expected_uri=peer["uri"],
            expected_eku=ExtendedKeyUsageOID.SERVER_AUTH,
        )
        if hashlib.sha256(presented).hexdigest() != peer["fingerprint"]:
            raise RuntimeError("runner-peer-unverified")
        run_candidate_session(
            connection,
            binding,
            invocation,
            scenario,
            handoff.joinpath("manifest"),
            identity,
            journal_directory,
        )
    return 0


def main(arguments: list[str] | None = None) -> int:
    """Run only the candidate Runner preflight until its Core handoff is provided."""
    parser = argparse.ArgumentParser(prog="atelier2-runner")
    parser.add_argument("--landlock-probe", action="store_true")
    parser.add_argument("--candidate-handoff", type=Path)
    parser.add_argument("--candidate-identity", type=Path)
    parser.add_argument("--candidate-journal", type=Path)
    parser.add_argument("--candidate-invocation-offer", type=Path)
    parsed = parser.parse_args(arguments)
    if parsed.candidate_handoff is not None:
        if (
            parsed.candidate_identity is None
            or parsed.candidate_journal is None
            or parsed.candidate_invocation_offer is None
        ):
            parser.error("candidate identity and journal are required together")
        return _run_candidate_session(
            parsed.candidate_handoff,
            parsed.candidate_identity,
            parsed.candidate_journal,
            parsed.candidate_invocation_offer,
        )
    if not parsed.landlock_probe:
        parser.error("the candidate Runner needs an explicit handoff mode")
    try:
        install_landlock_guard(child_allowlist())
    except LandlockUnavailable as error:
        parser.error(f"Landlock is unavailable: {error}")
    return 0
