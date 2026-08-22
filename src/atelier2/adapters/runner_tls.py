from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed448, ed25519, padding, rsa
from cryptography.x509.oid import ObjectIdentifier

CORE_DNS_NAME = "core.runner-candidate.internal"


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
        dns_names = tuple(san.get_values_for_type(x509.DNSName))
        uris = tuple(san.get_values_for_type(x509.UniformResourceIdentifier))
        ekus = tuple(
            certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        )
    except x509.ExtensionNotFound as error:
        raise CertificatePeerError("runner-peer-unverified") from error
    expected_dns_names = () if expected_dns_name is None else (expected_dns_name,)
    if dns_names != expected_dns_names or uris != (expected_uri,):
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
