from __future__ import annotations

import hashlib
import json
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, ObjectIdentifier

from atelier2.contracts.agent_attempts import (
    RunnerGenerationBinding,
    RunnerInvocationId,
)

CORE_DNS_NAME = "core.runner-candidate.internal"
# The one address a Runner reaches Core at. Core binds it, the Runner connects
# to it, and the launcher's per-Attempt network policy opens exactly it -- three
# readers of one fact rather than three literals that could drift apart.
CORE_SESSION_PORT = 8443
_RUNNER_URI_PREFIX = ("urn", "atelier2", "runner", "v1")


class CertificatePeerError(ValueError):
    """A peer did not present the exact candidate identity Core selected."""


type SupportedPublicKey = (
    rsa.RSAPublicKey
    | ec.EllipticCurvePublicKey
    | ed25519.Ed25519PublicKey
    | ed448.Ed448PublicKey
)


def core_uri_for_certificate(public_key: SupportedPublicKey) -> str:
    encoded = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return f"urn:atelier2:core:v1:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class CorePeerDocument:
    """The one address, certificate identity, and port a Runner reaches Core
    at -- an Attempt's Core composition writes this once, and its Runner reads
    it once, so neither side ever re-derives what the other already fixed.

    `port` is carried per document rather than assumed: today's witness and
    launcher both still bind `CORE_SESSION_PORT`, but the document is what a
    Runner actually dials, so a future per-Attempt port needs no wire change.
    """

    dns_name: str
    uri: str
    fingerprint: str
    port: int


def encode_core_peer_document(document: CorePeerDocument) -> bytes:
    return json.dumps(
        {
            "dns_name": document.dns_name,
            "uri": document.uri,
            "fingerprint": document.fingerprint,
            "port": document.port,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def decode_core_peer_document(raw: bytes) -> CorePeerDocument:
    parsed = json.loads(raw)
    return CorePeerDocument(
        str(parsed["dns_name"]),
        str(parsed["uri"]),
        str(parsed["fingerprint"]),
        int(parsed["port"]),
    )


def runner_uri_for_invocation(
    binding: RunnerGenerationBinding, invocation: RunnerInvocationId
) -> str:
    return (
        "urn:atelier2:runner:v1:"
        f"{binding.attempt_id.value}:{binding.request_hash.value}:"
        f"{binding.generation_id.value}:{invocation.value}:{binding.manifest_id.value}"
    )


def invocation_from_runner_uri(
    uri: str, binding: RunnerGenerationBinding
) -> RunnerInvocationId:
    parts = uri.split(":")
    if len(parts) != 9 or tuple(parts[:4]) != _RUNNER_URI_PREFIX:
        raise CertificatePeerError("runner-binding-san-mismatch")
    attempt, request, generation, invocation, manifest = parts[4:]
    try:
        parsed = RunnerInvocationId(invocation)
    except ValueError as error:
        raise CertificatePeerError("runner-binding-san-mismatch") from error
    if (
        attempt != binding.attempt_id.value
        or request != binding.request_hash.value
        or generation != binding.generation_id.value
        or manifest != binding.manifest_id.value
        or runner_uri_for_invocation(binding, parsed) != uri
    ):
        raise CertificatePeerError("runner-binding-san-mismatch")
    return parsed


def sole_peer_uri(certificate: x509.Certificate) -> str:
    """The one URI SAN a peer identity certificate must carry, or refuse.

    Every runner/core peer identity encodes its exact one binding as a single
    URI general name; a certificate presenting zero or more than one is a
    peer this process cannot address unambiguously.
    """
    uris = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)
    if len(uris) != 1:
        raise CertificatePeerError("runner-binding-san-mismatch")
    return uris[0]


def expected_peer_san(
    *, expected_dns_name: str | None, expected_uri: str
) -> tuple[x509.GeneralName, ...]:
    uri = x509.UniformResourceIdentifier(expected_uri)
    if expected_dns_name is None:
        return (uri,)
    return (x509.DNSName(expected_dns_name), uri)


def pin_tls_13(context: ssl.SSLContext) -> None:
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3


def validate_peer_certificate(
    certificate_pem: bytes,
    ca_pem: bytes,
    *,
    expected_dns_name: str | None,
    expected_uri: str,
    expected_eku: ObjectIdentifier,
) -> x509.Certificate:
    """Validate the exact CA, validity, SAN set and sole peer-use purpose."""
    try:
        certificate = x509.load_pem_x509_certificate(certificate_pem)
        authority = x509.load_pem_x509_certificate(ca_pem)
    except ValueError as error:
        raise CertificatePeerError("runner-peer-unverified") from error
    now = datetime.now(UTC)
    if not certificate.not_valid_before_utc <= now <= certificate.not_valid_after_utc:
        raise CertificatePeerError("runner-peer-unverified")
    _verify_issuer_signature(certificate, authority)
    try:
        san = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
        ekus = tuple(
            certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        )
    except x509.ExtensionNotFound as error:
        raise CertificatePeerError("runner-peer-unverified") from error
    if tuple(san) != expected_peer_san(
        expected_dns_name=expected_dns_name, expected_uri=expected_uri
    ):
        raise CertificatePeerError("runner-binding-san-mismatch")
    if ekus != (expected_eku,):
        raise CertificatePeerError("runner-peer-eku-mismatch")
    return certificate


def _verify_issuer_signature(
    certificate: x509.Certificate, authority: x509.Certificate
) -> None:
    key = authority.public_key()
    signature_hash = certificate.signature_hash_algorithm
    if signature_hash is None:
        raise CertificatePeerError("runner-peer-unverified")
    try:
        if isinstance(key, rsa.RSAPublicKey):
            key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                padding.PKCS1v15(),
                signature_hash,
            )
        elif isinstance(key, ec.EllipticCurvePublicKey):
            key.verify(
                certificate.signature,
                certificate.tbs_certificate_bytes,
                ec.ECDSA(signature_hash),
            )
        elif isinstance(key, ed25519.Ed25519PublicKey | ed448.Ed448PublicKey):
            key.verify(certificate.signature, certificate.tbs_certificate_bytes)
        else:
            raise CertificatePeerError("runner-peer-unverified")
    except InvalidSignature as error:
        raise CertificatePeerError("runner-peer-unverified") from error


def validate_core_peer_leaf(
    presented_der: bytes, ca_pem: bytes, document: CorePeerDocument
) -> x509.Certificate:
    """Validate a presented Core leaf against everything `document` pinned:
    the exact CA, SAN set, EKU, and the fingerprint of the exact bytes
    presented -- the fence a Runner holds against Core, symmetric to the one
    Core's accept loop holds against its Runner (`RunnerPeerPin`)."""
    presented_pem = x509.load_der_x509_certificate(presented_der).public_bytes(
        serialization.Encoding.PEM
    )
    certificate = validate_peer_certificate(
        presented_pem,
        ca_pem,
        expected_dns_name=document.dns_name,
        expected_uri=document.uri,
        expected_eku=ExtendedKeyUsageOID.SERVER_AUTH,
    )
    if hashlib.sha256(presented_der).hexdigest() != document.fingerprint:
        raise CertificatePeerError("runner-peer-unverified")
    return certificate
