"""External, disposable CA hook for the #301-A witness; it is never in an image."""

from __future__ import annotations

import argparse
import json
import struct
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from atelier2.adapters.free_runner_executor import FreeRunnerExecutorFactory
from atelier2.adapters.runner_child import REQUIRED_LANDLOCK_ABI
from atelier2.adapters.runner_tls import CORE_DNS_NAME, core_uri_for_certificate
from atelier2.contracts.agents import AgentExecutionCapability, AuthMode
from atelier2.contracts.runner_manifests import (
    CANDIDATE_CPU_PERIOD,
    CANDIDATE_JOURNAL_BYTES,
    CANDIDATE_WORKSPACE_BYTES,
    candidate_runner_manifest,
    decode_runner_manifest,
    encode_runner_manifest,
    runner_manifest_id,
)


def _private(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(0o600)


def _public(path: Path, certificate: x509.Certificate) -> None:
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    path.chmod(0o644)


def _authority(state: Path) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key_path, certificate_path = state / "ca.key", state / "ca.crt"
    if key_path.exists():
        return cast(
            rsa.RSAPrivateKey,
            serialization.load_pem_private_key(key_path.read_bytes(), password=None),
        ), x509.load_pem_x509_certificate(certificate_path.read_bytes())
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "atelier2 runner candidate CA")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=10))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    _private(key_path, key)
    _public(certificate_path, certificate)
    return key, certificate


def _leaf(
    authority_key: rsa.RSAPrivateKey,
    authority: x509.Certificate,
    common_name: str,
    sans: list[x509.GeneralName],
    eku: x509.ObjectIdentifier,
) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=5))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        .sign(authority_key, hashes.SHA256())
    )
    return key, certificate


def issue_core(state: Path, identity: Path) -> int:
    authority_key, authority = _authority(state)
    # Key creation must precede the SPKI-bound URI. Build the final leaf once with it.
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CORE_DNS_NAME)])
        )
        .issuer_name(authority.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=5))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(CORE_DNS_NAME),
                    x509.UniformResourceIdentifier(
                        core_uri_for_certificate(key.public_key())
                    ),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(authority_key, hashes.SHA256())
    )
    identity.mkdir(mode=0o700, parents=True, exist_ok=True)
    _private(identity / "core.key", key)
    _public(identity / "core.crt", certificate)
    _public(identity / "ca.crt", authority)
    return 0


def issue_runner(
    state: Path,
    bootstrap: Path,
    invocation_offer: Path,
    runner_identity: Path,
    core_peer: Path,
) -> int:
    authority_key, authority = _authority(state)
    binding = json.loads(bootstrap.read_text(encoding="utf-8"))
    invocation = json.loads(invocation_offer.read_text(encoding="utf-8"))[
        "invocation_id"
    ]
    uri = "urn:atelier2:runner:v1:{attempt_id}:{request_hash}:{generation_id}:{invocation}:{manifest_id}".format(
        **binding, invocation=invocation
    )
    key, certificate = _leaf(
        authority_key,
        authority,
        "runner-candidate",
        [x509.UniformResourceIdentifier(uri)],
        ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    _private(runner_identity / "client.key", key)
    _public(runner_identity / "client.crt", certificate)
    _public(runner_identity / "ca.crt", authority)
    core_peer.mkdir(mode=0o700, parents=True, exist_ok=True)
    _public(core_peer / "client.crt", certificate)
    return 0


def write_candidate_manifest(
    output: Path, source_commit: str, image_digest: str
) -> int:
    factory = FreeRunnerExecutorFactory()
    manifest = candidate_runner_manifest(
        source_commit=source_commit,
        image_digest=image_digest,
        required_landlock_abi=REQUIRED_LANDLOCK_ABI,
        executor_revision=factory.key.executor_revision.value,
        executor_operational_identity=factory.operational_identity.value,
        provider_id=factory.key.provider_id.value,
        auth_mode=AuthMode.API_KEY.value,
        requested_capability=AgentExecutionCapability.HEADLESS.value,
    )
    encoded = encode_runner_manifest(manifest)
    output.mkdir(parents=True, exist_ok=True)
    (output / "manifest").write_bytes(encoded)
    (output / "manifest-id").write_text(
        runner_manifest_id(manifest).value + "\n", encoding="ascii"
    )
    return 0


def _tmpfs_size(options: str) -> int:
    for part in options.split(","):
        if part.startswith("size="):
            raw = part.removeprefix("size=")
            if raw[-1:] in {"m", "M"}:
                return int(raw[:-1]) * 1024 * 1024
            if raw[-1:] in {"k", "K"}:
                return int(raw[:-1]) * 1024
            return int(raw)
    raise ValueError("runner-attestation-mismatch")


def attest_runner_inspect(inspect_path: Path, manifest_path: Path, output: Path) -> int:
    document = json.loads(inspect_path.read_text(encoding="utf-8"))
    if isinstance(document, list):
        document = document[0]
    host = document["HostConfig"]
    config = document["Config"]
    manifest = decode_runner_manifest(manifest_path.read_bytes())
    tmpfs = host.get("Tmpfs") or {}
    security = host.get("SecurityOpt") or []
    cap_drop = host.get("CapDrop") or []
    user = config.get("User") or ""
    expected_user = f"{manifest.effective_uid}:{manifest.effective_gid}"
    nnp = any(
        option == "no-new-privileges:true" or option.startswith("no-new-privileges")
        for option in security
    )
    if (
        document.get("Image") != manifest.image_digest
        or user != expected_user
        or host.get("ReadonlyRootfs") is not True
        or "ALL" not in cap_drop
        or not nnp
        or int(host.get("PidsLimit") or 0) != manifest.process_limit
        or int(host.get("Memory") or 0) != manifest.memory_bytes
        or int(host.get("CpuQuota") or 0) != manifest.cpu_quota_microseconds
        or int(host.get("CpuPeriod") or 0) != CANDIDATE_CPU_PERIOD
        or _tmpfs_size(tmpfs.get("/workspace", "")) != CANDIDATE_WORKSPACE_BYTES
        or _tmpfs_size(tmpfs.get("/journal", "")) != CANDIDATE_JOURNAL_BYTES
    ):
        raise ValueError("runner-attestation-mismatch")
    output.write_text(runner_manifest_id(manifest).value + "\n", encoding="ascii")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    core = commands.add_parser("core")
    core.add_argument("--state", type=Path, required=True)
    core.add_argument("--identity", type=Path, required=True)
    runner = commands.add_parser("runner")
    runner.add_argument("--state", type=Path, required=True)
    runner.add_argument("--bootstrap", type=Path, required=True)
    runner.add_argument("--invocation-offer", type=Path, required=True)
    runner.add_argument("--runner-identity", type=Path, required=True)
    runner.add_argument("--core-peer", type=Path, required=True)
    record = commands.add_parser("receiver-record")
    record.add_argument("--identity", type=Path, required=True)
    manifest = commands.add_parser("manifest")
    manifest.add_argument("--source-commit", required=True)
    manifest.add_argument("--image-digest", required=True)
    manifest.add_argument("--output", type=Path, required=True)
    inspect = commands.add_parser("attest-inspect")
    inspect.add_argument("--inspect", type=Path, required=True)
    inspect.add_argument("--manifest", type=Path, required=True)
    inspect.add_argument("--output", type=Path, required=True)
    parsed = parser.parse_args()
    if parsed.command == "core":
        return issue_core(parsed.state, parsed.identity)
    if parsed.command == "manifest":
        return write_candidate_manifest(
            parsed.output, parsed.source_commit, parsed.image_digest
        )
    if parsed.command == "attest-inspect":
        return attest_runner_inspect(parsed.inspect, parsed.manifest, parsed.output)
    if parsed.command == "runner":
        return issue_runner(
            parsed.state,
            parsed.bootstrap,
            parsed.invocation_offer,
            parsed.runner_identity,
            parsed.core_peer,
        )
    fields = tuple(
        parsed.identity.joinpath(name).read_bytes()
        for name in ("client.crt", "client.key", "ca.crt")
    )
    if any(not field or len(field) > 8_192 for field in fields):
        raise ValueError("issuer identity field exceeds receiver bound")
    import sys

    sys.stdout.buffer.write(
        b"".join(struct.pack(">I", len(field)) + field for field in fields)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
