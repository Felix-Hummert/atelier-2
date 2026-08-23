from __future__ import annotations

import importlib.util
import json
import ssl
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from atelier2.adapters.runner_tls import (
    CORE_DNS_NAME,
    CertificatePeerError,
    core_uri_for_certificate,
    pin_tls_13,
    validate_peer_certificate,
)
from atelier2.contracts.runner_manifests import (
    CANDIDATE_CPU_PERIOD,
    CANDIDATE_WORKSPACE_BYTES,
    RunnerPathRight,
    candidate_runner_manifest,
    encode_runner_manifest,
)


def _certificate(
    *,
    dns_name: str = CORE_DNS_NAME,
    eku: x509.ObjectIdentifier = ExtendedKeyUsageOID.SERVER_AUTH,
    extra_names: tuple[x509.GeneralName, ...] = (),
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
                [
                    x509.DNSName(dns_name),
                    x509.UniformResourceIdentifier(uri),
                    *extra_names,
                ]
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


def test_tls_peer_validation_refuses_an_extra_san_general_name() -> None:
    ca, leaf = _certificate(extra_names=(x509.RFC822Name("runner@example.test"),))
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


def test_tls_context_is_pinned_to_tls_1_3() -> None:
    context = ssl.create_default_context()
    pin_tls_13(context)
    assert context.minimum_version == ssl.TLSVersion.TLSv1_3
    assert context.maximum_version == ssl.TLSVersion.TLSv1_3


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


def test_issuer_identity_read_refuses_extra_entries(tmp_path: Path) -> None:
    identity = tmp_path / "issuer-output"
    identity.mkdir(mode=0o700)
    (identity / "client.crt").write_bytes(b"not-a-cert")
    (identity / "client.crt").chmod(0o644)
    (identity / "client.key").write_bytes(b"not-a-key")
    (identity / "client.key").chmod(0o600)
    (identity / "ca.crt").write_bytes(b"not-a-ca")
    (identity / "ca.crt").chmod(0o644)
    (identity / "extra").write_bytes(b"no")
    result = subprocess.run(
        (
            sys.executable,
            "tests/witness/runner_candidate_issuer.py",
            "receiver-record",
            "--identity",
            str(identity),
        ),
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


def test_issuer_identity_read_refuses_wrong_key_mode(tmp_path: Path) -> None:
    identity = tmp_path / "issuer-output"
    identity.mkdir(mode=0o700)
    (identity / "client.crt").write_bytes(b"not-a-cert")
    (identity / "client.crt").chmod(0o644)
    (identity / "client.key").write_bytes(b"not-a-key")
    (identity / "client.key").chmod(0o644)
    (identity / "ca.crt").write_bytes(b"not-a-ca")
    (identity / "ca.crt").chmod(0o644)
    result = subprocess.run(
        (
            sys.executable,
            "tests/witness/runner_candidate_issuer.py",
            "receiver-record",
            "--identity",
            str(identity),
        ),
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0


def test_unlink_private_keys_uses_held_directory_fds(tmp_path: Path) -> None:
    issuer = tmp_path / "issuer"
    identity = tmp_path / "core-identity"
    output = tmp_path / "issuer-output"
    for directory, name in (
        (issuer, "ca.key"),
        (identity, "core.key"),
        (output, "client.key"),
    ):
        directory.mkdir(mode=0o700)
        (directory / name).write_bytes(b"secret")
        (directory / name).chmod(0o600)
        (directory / name.replace(".key", ".crt")).write_bytes(b"public")
        (directory / name.replace(".key", ".crt")).chmod(0o644)
    result = subprocess.run(
        (
            sys.executable,
            "tests/witness/runner_candidate_issuer.py",
            "unlink-private",
            "--key",
            str(issuer / "ca.key"),
            "--key",
            str(identity / "core.key"),
            "--key",
            str(output / "client.key"),
        ),
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert not (issuer / "ca.key").exists()
    assert not (identity / "core.key").exists()
    assert not (output / "client.key").exists()
    assert (issuer / "ca.crt").is_file()
    assert (identity / "core.crt").is_file()
    assert (output / "client.crt").is_file()


_ISSUER = Path(__file__).resolve().parents[1] / "witness" / "runner_candidate_issuer.py"


def _candidate_manifest():
    return candidate_runner_manifest(
        source_commit="a" * 40,
        image_digest="sha256:" + "b" * 64,
        required_landlock_abi=1,
        executor_revision="fake-free/v1",
        executor_operational_identity="free-runner-candidate",
        provider_id="fake-free",
        auth_mode="api_key",
        requested_capability="headless",
    )


def _issuer_module():
    spec = importlib.util.spec_from_file_location("runner_candidate_issuer", _ISSUER)
    if spec is None or spec.loader is None:
        raise RuntimeError("candidate issuer is unreadable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_IDENTITY_MOUNT = {
    "Destination": "/run/atelier2-identity",
    "RW": False,
    "Type": "volume",
}
_JOURNAL_MOUNT = {"Destination": "/journal", "RW": True, "Type": "volume"}


def _credential_mount(manifest, **overrides: object) -> dict[str, object]:
    """The one read-only host bind ADR 0009 sec. 2's amendment admits."""
    return {
        "Destination": manifest.provider_credential_directory.as_posix(),
        "RW": False,
        "Type": "bind",
        **overrides,
    }


def _inspect_document(
    manifest,
    security: list[str],
    tmpfs: dict[str, str] | None = None,
    mounts: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    return {
        "Image": manifest.image_digest,
        "Config": {"User": f"{manifest.effective_uid}:{manifest.effective_gid}"},
        "HostConfig": {
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": security,
            "PidsLimit": manifest.process_limit,
            "Memory": manifest.memory_bytes,
            "CpuQuota": manifest.cpu_quota_microseconds,
            "CpuPeriod": CANDIDATE_CPU_PERIOD,
            "Tmpfs": {
                "/workspace": f"rw,noexec,nosuid,size={CANDIDATE_WORKSPACE_BYTES}",
                **(tmpfs if tmpfs is not None else _writable_surface(manifest)),
            },
        },
        "Mounts": [
            _IDENTITY_MOUNT,
            _JOURNAL_MOUNT,
            *(mounts if mounts is not None else [_credential_mount(manifest)]),
        ],
    }


def _writable_surface(manifest) -> dict[str, str]:
    """The mount options a launcher must give every writable manifest grant.

    The size comes from the manifest rather than a literal here, because the
    manifest is what the fence compares against -- a second number would only
    be able to disagree with it.
    """
    return {
        grant.path.as_posix(): f"rw,noexec,nosuid,size={manifest.scratch_bytes}"
        for grant in manifest.child_path_grants
        if grant.right is RunnerPathRight.READ_WRITE
    }


def _softened_writable_surface(dropped_flag: str) -> dict[str, str]:
    return {
        path: ",".join(flag for flag in options.split(",") if flag != dropped_flag)
        for path, options in _writable_surface(_candidate_manifest()).items()
    }


_SOFTENED_WITHOUT_NOEXEC = _softened_writable_surface("noexec")
_SOFTENED_WITHOUT_NOSUID = _softened_writable_surface("nosuid")
_CREDENTIAL_READ_ONLY = _credential_mount(_candidate_manifest())
_CREDENTIAL_WRITABLE = _credential_mount(_candidate_manifest(), RW=True)
_SECOND_HOST_BIND = {
    "Destination": "/var/lib/atelier2-elsewhere",
    "RW": False,
    "Type": "bind",
}


@pytest.mark.parametrize(
    "security",
    (
        ["no-new-privileges"],
        ["no-new-privileges:false"],
        ["label=no-new-privileges:true"],
    ),
)
def test_attest_inspect_requires_exact_no_new_privileges_true(
    tmp_path: Path, security: list[str]
) -> None:
    manifest = _candidate_manifest()
    inspect_path = tmp_path / "inspect.json"
    inspect_path.write_text(
        json.dumps(_inspect_document(manifest, security)), encoding="utf-8"
    )
    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(encode_runner_manifest(manifest))

    with pytest.raises(ValueError, match="runner-attestation-mismatch"):
        _issuer_module().attest_runner_inspect(
            inspect_path, manifest_path, tmp_path / "out"
        )


@pytest.mark.parametrize(
    "tmpfs",
    (
        pytest.param({}, id="writable-surface-not-mounted"),
        pytest.param(
            _SOFTENED_WITHOUT_NOEXEC, id="writable-surface-would-allow-execution"
        ),
        pytest.param(
            _SOFTENED_WITHOUT_NOSUID, id="writable-surface-would-allow-set-user-id"
        ),
    ),
)
def test_attest_inspect_requires_every_writable_grant_to_be_a_noexec_tmpfs(
    tmp_path: Path, tmpfs: dict[str, str]
) -> None:
    """The manifest attests the writable paths; the launcher proves their flags."""
    manifest = _candidate_manifest()
    inspect_path = tmp_path / "inspect.json"
    inspect_path.write_text(
        json.dumps(_inspect_document(manifest, ["no-new-privileges:true"], tmpfs)),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(encode_runner_manifest(manifest))

    with pytest.raises(ValueError, match="runner-attestation-mismatch"):
        _issuer_module().attest_runner_inspect(
            inspect_path, manifest_path, tmp_path / "out"
        )


def test_attest_inspect_accepts_exact_no_new_privileges_true(tmp_path: Path) -> None:
    manifest = _candidate_manifest()
    inspect_path = tmp_path / "inspect.json"
    inspect_path.write_text(
        json.dumps(_inspect_document(manifest, ["no-new-privileges:true"])),
        encoding="utf-8",
    )
    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(encode_runner_manifest(manifest))
    output = tmp_path / "out"

    assert (
        _issuer_module().attest_runner_inspect(inspect_path, manifest_path, output) == 0
    )
    assert output.read_text(encoding="ascii").strip()


@pytest.mark.parametrize(
    "mounts",
    (
        pytest.param([_JOURNAL_MOUNT], id="identity-mount-missing"),
        pytest.param(
            [{**_IDENTITY_MOUNT, "RW": True}, _JOURNAL_MOUNT],
            id="identity-mount-writable",
        ),
        pytest.param(
            [{**_IDENTITY_MOUNT, "Type": "bind"}, _JOURNAL_MOUNT],
            id="identity-mount-not-a-volume",
        ),
        pytest.param([_IDENTITY_MOUNT], id="journal-mount-missing"),
        pytest.param(
            [_IDENTITY_MOUNT, {**_JOURNAL_MOUNT, "RW": False}],
            id="journal-mount-read-only",
        ),
        pytest.param(
            [_IDENTITY_MOUNT, {**_JOURNAL_MOUNT, "Type": "tmpfs"}],
            id="journal-mount-not-a-volume",
        ),
    ),
)
def test_attest_inspect_refuses_a_wrong_identity_or_journal_mount(
    tmp_path: Path, mounts: list[dict[str, object]]
) -> None:
    """A tmpfs-backed `/journal` -- the shape `#15-B5` moved away from -- is
    refused the same way a wrong identity mount always was."""
    manifest = _candidate_manifest()
    document = _inspect_document(manifest, ["no-new-privileges:true"])
    document["Mounts"] = mounts
    inspect_path = tmp_path / "inspect.json"
    inspect_path.write_text(json.dumps(document), encoding="utf-8")
    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(encode_runner_manifest(manifest))

    with pytest.raises(ValueError, match="runner-attestation-mismatch"):
        _issuer_module().attest_runner_inspect(
            inspect_path, manifest_path, tmp_path / "out"
        )


@pytest.mark.parametrize(
    "mounts",
    (
        pytest.param([], id="credential-bind-missing"),
        pytest.param([_CREDENTIAL_WRITABLE], id="credential-bind-would-be-writable"),
        pytest.param(
            [_CREDENTIAL_READ_ONLY, _SECOND_HOST_BIND],
            id="a-second-host-path-was-bound-in",
        ),
    ),
)
def test_attest_inspect_admits_only_the_read_only_credential_host_bind(
    tmp_path: Path, mounts: list[dict[str, object]]
) -> None:
    """ADR 0009 sec. 2 admits one host bind beyond the identity material.

    Read-write access to the operator's own credential directory stays
    forbidden, and no second host path is admitted at all, so the fence has to
    refuse both a widened right and an extra surface.
    """
    manifest = _candidate_manifest()
    document = _inspect_document(manifest, ["no-new-privileges:true"], mounts=mounts)
    inspect_path = tmp_path / "inspect.json"
    inspect_path.write_text(json.dumps(document), encoding="utf-8")
    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(encode_runner_manifest(manifest))

    with pytest.raises(ValueError, match="runner-attestation-mismatch"):
        _issuer_module().attest_runner_inspect(
            inspect_path, manifest_path, tmp_path / "out"
        )
