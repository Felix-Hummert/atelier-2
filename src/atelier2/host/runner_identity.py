"""The host's certificate authority for one Attempt's identity material.

Private keys minted here never leave the host. The authority's own key stays in
its state directory, which no container is ever given; a Runner's client key is
written into a per-Attempt directory, delivered into that Attempt's identity
volume through a receiver container that reads it from a pipe, and unlinked
immediately afterwards. What a container ends up holding is the material this
authority decided to hand one invocation, never the authority itself (ADR 0009
sec. 2).

Each identity is valid for exactly as long as the thing it identifies lives
(`#540` C-3.3 D2): the installation's authority for a year, the console's own
leaf for a quarter and renewed by an operator command, and a Runner's leaf for
the attempt span the manifest Core selected declares -- the same span the
Runner's own session deadline runs on, so a leaf can never outlive the one
invocation it was minted for.
"""

from __future__ import annotations

import os
import stat
import struct
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from atelier2.adapters.runner_tls import (
    CORE_DNS_NAME,
    core_uri_for_certificate,
    runner_uri_for_invocation,
)
from atelier2.contracts.agent_attempts import (
    RunnerGenerationBinding,
    RunnerInvocationId,
)

_AUTHORITY_COMMON_NAME = "atelier2 runner candidate CA"
_RUNNER_COMMON_NAME = "runner-candidate"
_KEY_SIZE = 2048
_PUBLIC_EXPONENT = 65537
# How long each identity this authority mints stands. The authority is an
# installation's own root and is renewed as rarely as it is replaced; the
# console's leaf is renewed while the console keeps running, so it is short
# enough for a compromise to expire and long enough that renewal is a calendar
# task rather than an outage. A Runner's leaf gets no constant at all -- it is
# minted for the attempt span its manifest declares.
_AUTHORITY_VALIDITY = timedelta(days=365)
_CONSOLE_LEAF_VALIDITY = timedelta(days=90)
_CLOCK_SKEW = timedelta(minutes=1)
_IDENTITY_MODE = 0o700
_PRIVATE_MODE = 0o600
_PUBLIC_MODE = 0o644
IDENTITY_NAMES = ("client.crt", "client.key", "ca.crt")
_IDENTITY_MODES = (_PUBLIC_MODE, _PRIVATE_MODE, _PUBLIC_MODE)
_IDENTITY_FIELD_BYTES = 8_192
_PRIVATE_KEY_NAMES = frozenset({"ca.key", "core.key", "client.key"})


def _write_private(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path.chmod(_PRIVATE_MODE)


def _write_public(path: Path, certificate: x509.Certificate) -> None:
    path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    path.chmod(_PUBLIC_MODE)


def _generate_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(
        public_exponent=_PUBLIC_EXPONENT, key_size=_KEY_SIZE
    )


class RunnerIdentityAuthority:
    """The one certificate authority an installation's Attempts are issued from.

    It is constructed around a state directory rather than around a loaded key,
    so the key is read only for the moment an identity is actually minted and
    the authority can be handed to a launcher that never touches it.
    """

    def __init__(self, state_directory: Path) -> None:
        self._state_directory = state_directory

    def _authority(self) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
        key_path = self._state_directory / "ca.key"
        certificate_path = self._state_directory / "ca.crt"
        if key_path.exists():
            return cast(
                rsa.RSAPrivateKey,
                serialization.load_pem_private_key(key_path.read_bytes(), None),
            ), x509.load_pem_x509_certificate(certificate_path.read_bytes())
        self._state_directory.mkdir(mode=_IDENTITY_MODE, parents=True, exist_ok=True)
        key = _generate_key()
        now = datetime.now(UTC)
        subject = x509.Name(
            [x509.NameAttribute(NameOID.COMMON_NAME, _AUTHORITY_COMMON_NAME)]
        )
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW)
            .not_valid_after(now + _AUTHORITY_VALIDITY)
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(key, hashes.SHA256())
        )
        _write_private(key_path, key)
        _write_public(certificate_path, certificate)
        return key, certificate

    def issue_core_identity(self, destination: Path) -> None:
        """Mint the server identity Core serves its one session port under.

        The key is created before the certificate because the URI this leaf
        carries is bound to that key's public half, so there is exactly one
        leaf and no discarded intermediate.

        Renewal is this same call: the console reads its identity from a
        directory, and issuing into it again replaces the leaf the console
        presents from its next session on.
        """
        authority_key, authority = self._authority()
        key = _generate_key()
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CORE_DNS_NAME)])
            )
            .issuer_name(authority.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW)
            .not_valid_after(now + _CONSOLE_LEAF_VALIDITY)
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
        destination.mkdir(mode=_IDENTITY_MODE, parents=True, exist_ok=True)
        _write_private(destination / "core.key", key)
        _write_public(destination / "core.crt", certificate)
        _write_public(destination / "ca.crt", authority)

    def issue_runner_identity(
        self,
        binding: RunnerGenerationBinding,
        invocation: RunnerInvocationId,
        destination: Path,
        peer_destination: Path,
        attempt_span: timedelta,
    ) -> None:
        """Mint the client identity exactly one invocation may present.

        The leaf's URI binds this Attempt's generation to this invocation, so a
        Runner that presents it can be nothing else. It is written into an
        empty directory held open by descriptor for the whole write, so no
        symlink or replacement can steer the key somewhere else between the
        check and the write. Core's copy of the same leaf is what Core pins the
        session against.

        It stands for the attempt span the manifest Core selected declares --
        the same span the Runner's own session runs its deadline on -- plus the
        skew both clocks are read with. An invocation that is over therefore
        holds a key that no longer opens anything.
        """
        authority_key, authority = self._authority()
        key = _generate_key()
        now = datetime.now(UTC)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(
                x509.Name(
                    [x509.NameAttribute(NameOID.COMMON_NAME, _RUNNER_COMMON_NAME)]
                )
            )
            .issuer_name(authority.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _CLOCK_SKEW)
            .not_valid_after(now + attempt_span + _CLOCK_SKEW)
            .add_extension(
                x509.SubjectAlternativeName(
                    [
                        x509.UniformResourceIdentifier(
                            runner_uri_for_invocation(binding, invocation)
                        )
                    ]
                ),
                critical=False,
            )
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
            )
            .sign(authority_key, hashes.SHA256())
        )
        destination.mkdir(mode=_IDENTITY_MODE, parents=True, exist_ok=True)
        directory = os.open(
            destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            before = os.fstat(directory)
            if os.listdir(directory):
                raise ValueError("issuer identity destination is not empty")
            _write_private(destination / "client.key", key)
            _write_public(destination / "client.crt", certificate)
            _write_public(destination / "ca.crt", authority)
            after = os.fstat(directory)
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                raise ValueError("issuer identity directory identity changed")
        finally:
            os.close(directory)
        peer_destination.mkdir(mode=_IDENTITY_MODE, parents=True, exist_ok=True)
        _write_public(peer_destination / "client.crt", certificate)


def console_identity_expiry(identity_directory: Path) -> datetime:
    """When the leaf the console serves its Attempt sessions under stops standing.

    Read out of the certificate the console actually presents rather than
    recomputed from when it was issued, so a directory holding an older leaf
    than the authority last minted answers for the older one.
    """
    certificate = x509.load_pem_x509_certificate(
        (identity_directory / "core.crt").read_bytes()
    )
    return certificate.not_valid_after_utc


def receiver_record(directory: Path) -> bytes:
    """The length-prefixed record the Attempt's identity receiver reads.

    Every field is read through descriptors held on the directory this checked,
    and the directory is proven to be the same one afterwards, so what the
    receiver is handed is what was inspected. Anything that is not exactly the
    three expected files, with exactly the expected modes, one link each and a
    size inside the receiver's own bound, refuses instead of being sent.
    """
    dir_fd = os.open(
        directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        before = os.fstat(dir_fd)
        if (
            not stat.S_ISDIR(before.st_mode)
            or stat.S_IMODE(before.st_mode) != _IDENTITY_MODE
        ):
            raise ValueError("issuer identity directory mode differs")
        if set(os.listdir(dir_fd)) != set(IDENTITY_NAMES):
            raise ValueError("issuer identity directory entries differ")
        fields: list[bytes] = []
        for name, mode in zip(IDENTITY_NAMES, _IDENTITY_MODES, strict=True):
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd
            )
            try:
                info = os.fstat(descriptor)
                if (
                    info.st_nlink != 1
                    or not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != mode
                ):
                    raise ValueError("issuer identity file type or mode differs")
                if not 1 <= info.st_size <= _IDENTITY_FIELD_BYTES:
                    raise ValueError("issuer identity field exceeds receiver bound")
                payload = os.read(descriptor, info.st_size)
                if len(payload) != info.st_size:
                    raise ValueError("issuer identity field differs")
                fields.append(payload)
            finally:
                os.close(descriptor)
        after = os.fstat(dir_fd)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError("issuer identity directory identity changed")
        return b"".join(struct.pack(">I", len(field)) + field for field in fields)
    finally:
        os.close(dir_fd)


def unlink_private_keys(paths: Sequence[Path]) -> None:
    """Remove private key material through descriptors held on its directories.

    Every parent directory is opened first and kept open, so each unlink names
    an entry inside a directory that cannot have been swapped underneath in the
    meantime, and only the private names this authority mints may be removed at
    all.
    """
    held: list[tuple[int, str]] = []
    try:
        for path in paths:
            if path.name not in _PRIVATE_KEY_NAMES:
                raise ValueError("refusing to unlink a non-private name")
            held.append(
                (
                    os.open(
                        path.parent,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    ),
                    path.name,
                )
            )
        for directory, name in held:
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("private key is not a regular file")
            os.unlink(name, dir_fd=directory)
    finally:
        for directory, _name in held:
            os.close(directory)
