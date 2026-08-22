from __future__ import annotations

import argparse
import os
import secrets
import stat
import struct
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from atelier2.adapters.runner_tls import validate_peer_certificate

_MAXIMUM_FIELD_BYTES = 8_192
_MAXIMUM_RECORD_BYTES = 24_896
_NAMES = ("client.crt", "client.key", "ca.crt")
_MODES = (0o644, 0o600, 0o644)


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="atelier2-runner-identity-receiver")
    parser.add_argument("--destination", type=Path, required=True)
    parsed = parser.parse_args(arguments)
    record = _read_record()
    leaf, authority = _decode(record)
    _validate(leaf, authority)
    _write_identity(parsed.destination, record)
    return 0


def _read_record() -> tuple[bytes, bytes, bytes]:
    encoded = sys.stdin.buffer.read(_MAXIMUM_RECORD_BYTES + 1)
    if not 1 <= len(encoded) <= _MAXIMUM_RECORD_BYTES:
        raise ValueError("runner identity record size differs")
    fields: list[bytes] = []
    cursor = 0
    for _ in _NAMES:
        if cursor + 4 > len(encoded):
            raise ValueError("runner identity record is truncated")
        size = struct.unpack(">I", encoded[cursor : cursor + 4])[0]
        cursor += 4
        if not 1 <= size <= _MAXIMUM_FIELD_BYTES or cursor + size > len(encoded):
            raise ValueError("runner identity record field differs")
        fields.append(encoded[cursor : cursor + size])
        cursor += size
    if cursor != len(encoded) or len(fields) != 3:
        raise ValueError("runner identity record is noncanonical")
    return fields[0], fields[1], fields[2]


def _decode(record: tuple[bytes, bytes, bytes]) -> tuple[x509.Certificate, bytes]:
    leaf_pem, key_pem, authority = record
    leaf = x509.load_pem_x509_certificate(leaf_pem)
    key = serialization.load_pem_private_key(key_pem, password=None)
    public_key = getattr(key, "public_key", None)
    if public_key is None:
        raise ValueError("runner client key is unreadable")
    leaf_spki = leaf.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    key_spki = public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    if leaf_spki != key_spki:
        raise ValueError("runner client key differs from leaf")
    return leaf, authority


def _validate(leaf: x509.Certificate, authority: bytes) -> None:
    uris = leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.UniformResourceIdentifier)
    if len(uris) != 1:
        raise ValueError("runner-binding-san-mismatch")
    validate_peer_certificate(
        leaf.public_bytes(serialization.Encoding.PEM),
        authority,
        expected_dns_name=None,
        expected_uri=uris[0],
        expected_eku=ExtendedKeyUsageOID.CLIENT_AUTH,
    )


def load_published_identity(
    destination: Path, *, expected_uri: str, expected_ca: bytes
) -> tuple[bytes, bytes, bytes]:
    """Reread the volume identity through a directory FD before any socket."""
    directory = os.open(
        destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        directory_info = os.fstat(directory)
        if (
            directory_info.st_uid != 10001
            or directory_info.st_gid != 10001
            or not stat.S_ISDIR(directory_info.st_mode)
            or stat.S_IMODE(directory_info.st_mode) != 0o700
        ):
            raise ValueError("runner identity directory owner or mode differs")
        present = set(os.listdir(directory))
        if present != {"ready", *_NAMES}:
            raise ValueError("runner identity destination entries differ")
        payloads: dict[str, bytes] = {}
        for name, mode in zip(("ready", *_NAMES), (0o600, *_MODES), strict=True):
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            try:
                info = os.fstat(descriptor)
                if (
                    info.st_uid != 10001
                    or info.st_gid != 10001
                    or info.st_nlink != 1
                    or not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != mode
                ):
                    raise ValueError("runner identity file owner or mode differs")
                if name == "ready":
                    if info.st_size != 0:
                        raise ValueError("runner identity ready record differs")
                    continue
                if not 1 <= info.st_size <= _MAXIMUM_FIELD_BYTES:
                    raise ValueError("runner identity record field differs")
                payload = os.read(descriptor, info.st_size)
                if len(payload) != info.st_size:
                    raise ValueError("runner identity record field differs")
                payloads[name] = payload
            finally:
                os.close(descriptor)
        record = payloads["client.crt"], payloads["client.key"], payloads["ca.crt"]
        if record[2] != expected_ca:
            raise ValueError("Runner volume CA differs from bootstrap CA")
        leaf, authority = _decode(record)
        _validate(leaf, authority)
        validate_peer_certificate(
            leaf.public_bytes(serialization.Encoding.PEM),
            authority,
            expected_dns_name=None,
            expected_uri=expected_uri,
            expected_eku=ExtendedKeyUsageOID.CLIENT_AUTH,
        )
        return record
    finally:
        os.close(directory)


def _write_identity(destination: Path, record: tuple[bytes, bytes, bytes]) -> None:
    directory = os.open(
        destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    created: list[str] = []
    try:
        if os.listdir(directory):
            raise ValueError("runner identity destination is not empty")
        for name, payload, mode in zip(_NAMES, record, _MODES, strict=True):
            temporary = f".tmp-{secrets.token_hex(8)}"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=directory,
            )
            created.append(temporary)
            try:
                os.write(descriptor, payload)
                os.fchmod(descriptor, mode)
                info = os.fstat(descriptor)
                if (
                    info.st_uid != 10001
                    or info.st_gid != 10001
                    or not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) != mode
                ):
                    raise ValueError("runner identity file owner or mode differs")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
            created.remove(temporary)
            created.append(name)
        os.fsync(directory)
        ready = os.open(
            "ready",
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory,
        )
        created.append("ready")
        try:
            os.fsync(ready)
        finally:
            os.close(ready)
        os.fsync(directory)
    except BaseException:
        for name in created:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
        raise
    finally:
        os.close(directory)


if __name__ == "__main__":
    raise SystemExit(main())
