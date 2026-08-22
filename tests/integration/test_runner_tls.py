from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from atelier2.adapters.runner_tls import (
    CORE_DNS_NAME,
    CertificatePeerError,
    core_uri_for_certificate,
    validate_peer_certificate,
)


def _certificate(
    *,
    dns_name: str = CORE_DNS_NAME,
    eku: x509.ObjectIdentifier = ExtendedKeyUsageOID.SERVER_AUTH,
) -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "candidate-ca")])
    now = datetime.now(UTC)
    ca = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    uri = core_uri_for_certificate(leaf_key.public_key())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_name)]))
        .issuer_name(ca.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName(dns_name), x509.UniformResourceIdentifier(uri)]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (
        ca.public_bytes(serialization.Encoding.PEM),
        leaf.public_bytes(serialization.Encoding.PEM),
    )


def test_tls_peer_validation_requires_exact_ca_dns_uri_eku_and_fingerprint() -> None:
    ca, leaf = _certificate()
    certificate = x509.load_pem_x509_certificate(leaf)
    uri = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)[0]

    validate_peer_certificate(
        leaf,
        ca,
        expected_dns_name=CORE_DNS_NAME,
        expected_uri=uri,
        expected_eku=ExtendedKeyUsageOID.SERVER_AUTH,
    )


def test_tls_peer_validation_refuses_a_wrong_eku_before_a_session_operation() -> None:
    ca, leaf = _certificate(eku=ExtendedKeyUsageOID.CLIENT_AUTH)
    certificate = x509.load_pem_x509_certificate(leaf)
    uri = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)[0]

    with pytest.raises(CertificatePeerError, match="runner-peer-eku-mismatch"):
        validate_peer_certificate(
            leaf,
            ca,
            expected_dns_name=CORE_DNS_NAME,
            expected_uri=uri,
            expected_eku=ExtendedKeyUsageOID.SERVER_AUTH,
        )


def test_tls_peer_validation_refuses_a_wrong_dns_san_before_a_session_operation() -> (
    None
):
    ca, leaf = _certificate(dns_name="wrong.example")
    certificate = x509.load_pem_x509_certificate(leaf)
    uri = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)[0]

    with pytest.raises(CertificatePeerError, match="runner-binding-san-mismatch"):
        validate_peer_certificate(
            leaf,
            ca,
            expected_dns_name=CORE_DNS_NAME,
            expected_uri=uri,
            expected_eku=ExtendedKeyUsageOID.SERVER_AUTH,
        )


def test_tls_peer_validation_refuses_a_wrong_ca_before_a_session_operation() -> None:
    _ca, leaf = _certificate()
    other_ca, _other_leaf = _certificate()
    certificate = x509.load_pem_x509_certificate(leaf)
    uri = certificate.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)[0]

    with pytest.raises(CertificatePeerError, match="runner-peer-unverified"):
        validate_peer_certificate(
            leaf,
            other_ca,
            expected_dns_name=CORE_DNS_NAME,
            expected_uri=uri,
            expected_eku=ExtendedKeyUsageOID.SERVER_AUTH,
        )
