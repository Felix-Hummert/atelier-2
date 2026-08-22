from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from atelier2.contracts.agent_attempts import RunnerManifestId
from atelier2.contracts.hashing import frame

_COMMIT = re.compile(r"[0-9a-f]{40}")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_CAPABILITIES = re.compile(r"[0-9a-f]{16}")
_FRAME_PREFIX = b"ATELIER2\x00"
_DOMAIN = b"runner-manifest/v1"
_FIELD_COUNT = 22
CANDIDATE_EFFECTIVE_UID = 10001
CANDIDATE_EFFECTIVE_GID = 10001
CANDIDATE_EFFECTIVE_CAPABILITIES = "0000000000000000"
CANDIDATE_PROCESS_LIMIT = 64
CANDIDATE_MEMORY_BYTES = 268_435_456
CANDIDATE_CPU_QUOTA = 100_000
CANDIDATE_CPU_PERIOD = 100_000
CANDIDATE_WORKSPACE_BYTES = 67_108_864
CANDIDATE_JOURNAL_BYTES = 1_048_576
CANDIDATE_TERM_GRACE = 1_000
CANDIDATE_REAP_DEADLINE = 5_000
CANDIDATE_ATTEMPT_SPAN = 60_000


@dataclass(frozen=True)
class RunnerManifestV1:
    """The complete carrier facts Core must select before it arms a Runner."""

    source_commit: str
    image_digest: str
    executor_revision: str
    executor_operational_identity: str
    provider_id: str
    auth_mode: str
    requested_capability: str
    required_landlock_abi: int
    effective_uid: int
    effective_gid: int
    effective_capabilities: str
    no_new_privileges: bool
    read_only_root: bool
    process_limit: int
    memory_bytes: int
    cpu_quota_microseconds: int
    workspace_bytes: int
    journal_bytes: int
    terminate_grace_milliseconds: int
    reap_deadline_milliseconds: int
    total_attempt_milliseconds: int

    def __post_init__(self) -> None:
        if _COMMIT.fullmatch(self.source_commit) is None:
            raise ValueError("runner manifest source commit must be a full SHA-1")
        if _IMAGE_DIGEST.fullmatch(self.image_digest) is None:
            raise ValueError("runner manifest image digest must be a SHA-256 digest")
        if _CAPABILITIES.fullmatch(self.effective_capabilities) is None:
            raise ValueError(
                "runner manifest effective capabilities must be 16 hex bytes"
            )
        for name in (
            "executor_revision",
            "executor_operational_identity",
            "provider_id",
            "auth_mode",
            "requested_capability",
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, str)
                or not value
                or len(value.encode("utf-8")) > 4096
            ):
                raise ValueError(f"runner manifest {name} must be bounded UTF-8")
        for name in (
            "required_landlock_abi",
            "effective_uid",
            "effective_gid",
            "process_limit",
            "memory_bytes",
            "cpu_quota_microseconds",
            "workspace_bytes",
            "journal_bytes",
            "terminate_grace_milliseconds",
            "reap_deadline_milliseconds",
            "total_attempt_milliseconds",
        ):
            value = getattr(self, name)
            if type(value) is not int or value <= 0 or value > 2**64 - 1:
                raise ValueError(f"runner manifest {name} must be a positive uint64")
        if (
            type(self.no_new_privileges) is not bool
            or type(self.read_only_root) is not bool
        ):
            raise ValueError("runner manifest hardening facts must be booleans")


def encode_runner_manifest(manifest: RunnerManifestV1) -> bytes:
    """Encode the selected facts under their one manifest identity domain."""
    return frame(
        "runner-manifest/v1",
        manifest.source_commit.encode("ascii"),
        manifest.image_digest.encode("ascii"),
        b"runner-session/v1",
        manifest.executor_revision.encode("utf-8"),
        manifest.executor_operational_identity.encode("utf-8"),
        manifest.provider_id.encode("utf-8"),
        manifest.auth_mode.encode("utf-8"),
        manifest.requested_capability.encode("utf-8"),
        struct.pack(">Q", manifest.required_landlock_abi),
        struct.pack(">Q", manifest.effective_uid),
        struct.pack(">Q", manifest.effective_gid),
        manifest.effective_capabilities.encode("ascii"),
        b"1" if manifest.no_new_privileges else b"0",
        b"1" if manifest.read_only_root else b"0",
        struct.pack(">Q", manifest.process_limit),
        struct.pack(">Q", manifest.memory_bytes),
        struct.pack(">Q", manifest.cpu_quota_microseconds),
        struct.pack(">Q", manifest.workspace_bytes),
        struct.pack(">Q", manifest.journal_bytes),
        struct.pack(">Q", manifest.terminate_grace_milliseconds),
        struct.pack(">Q", manifest.reap_deadline_milliseconds),
        struct.pack(">Q", manifest.total_attempt_milliseconds),
    )


def decode_runner_manifest(encoded: bytes) -> RunnerManifestV1:
    """Decode one canonical manifest and refuse any other byte sequence."""
    fields = _decode_manifest_fields(encoded)
    if len(fields) != _FIELD_COUNT or fields[2] != b"runner-session/v1":
        raise ValueError("runner-manifest-mismatch")
    packed = (fields[8], fields[9], fields[10], *fields[14:22])
    if any(len(field) != 8 for field in packed):
        raise ValueError("runner-manifest-mismatch")
    integers = (
        struct.unpack(">Q", fields[8])[0],
        struct.unpack(">Q", fields[9])[0],
        struct.unpack(">Q", fields[10])[0],
        struct.unpack(">Q", fields[14])[0],
        struct.unpack(">Q", fields[15])[0],
        struct.unpack(">Q", fields[16])[0],
        struct.unpack(">Q", fields[17])[0],
        struct.unpack(">Q", fields[18])[0],
        struct.unpack(">Q", fields[19])[0],
        struct.unpack(">Q", fields[20])[0],
        struct.unpack(">Q", fields[21])[0],
    )
    try:
        manifest = RunnerManifestV1(
            fields[0].decode("ascii"),
            fields[1].decode("ascii"),
            fields[3].decode("utf-8"),
            fields[4].decode("utf-8"),
            fields[5].decode("utf-8"),
            fields[6].decode("utf-8"),
            fields[7].decode("utf-8"),
            integers[0],
            integers[1],
            integers[2],
            fields[11].decode("ascii"),
            fields[12] == b"1",
            fields[13] == b"1",
            integers[3],
            integers[4],
            integers[5],
            integers[6],
            integers[7],
            integers[8],
            integers[9],
            integers[10],
        )
    except (TypeError, UnicodeDecodeError, ValueError) as error:
        raise ValueError("runner-manifest-mismatch") from error
    if fields[12] not in {b"0", b"1"} or fields[13] not in {b"0", b"1"}:
        raise ValueError("runner-manifest-mismatch")
    if encode_runner_manifest(manifest) != encoded:
        raise ValueError("runner-manifest-mismatch")
    return manifest


def runner_manifest_id(manifest: RunnerManifestV1) -> RunnerManifestId:
    return RunnerManifestId.of(encode_runner_manifest(manifest))


def candidate_runner_manifest(
    *,
    source_commit: str,
    image_digest: str,
    required_landlock_abi: int,
    executor_revision: str,
    executor_operational_identity: str,
    provider_id: str,
    auth_mode: str,
    requested_capability: str,
) -> RunnerManifestV1:
    """The exact A candidate facts the launcher attests and Core selects."""
    return RunnerManifestV1(
        source_commit,
        image_digest,
        executor_revision,
        executor_operational_identity,
        provider_id,
        auth_mode,
        requested_capability,
        required_landlock_abi,
        CANDIDATE_EFFECTIVE_UID,
        CANDIDATE_EFFECTIVE_GID,
        CANDIDATE_EFFECTIVE_CAPABILITIES,
        True,
        True,
        CANDIDATE_PROCESS_LIMIT,
        CANDIDATE_MEMORY_BYTES,
        CANDIDATE_CPU_QUOTA,
        CANDIDATE_WORKSPACE_BYTES,
        CANDIDATE_JOURNAL_BYTES,
        CANDIDATE_TERM_GRACE,
        CANDIDATE_REAP_DEADLINE,
        CANDIDATE_ATTEMPT_SPAN,
    )


def _decode_manifest_fields(encoded: bytes) -> tuple[bytes, ...]:
    if not encoded.startswith(_FRAME_PREFIX):
        raise ValueError("runner-manifest-mismatch")
    cursor = len(_FRAME_PREFIX)
    if len(encoded) < cursor + 4:
        raise ValueError("runner-manifest-mismatch")
    domain_length = struct.unpack(">I", encoded[cursor : cursor + 4])[0]
    cursor += 4
    domain_end = cursor + domain_length
    if domain_end > len(encoded) or encoded[cursor:domain_end] != _DOMAIN:
        raise ValueError("runner-manifest-mismatch")
    cursor = domain_end
    fields: list[bytes] = []
    while cursor < len(encoded):
        if len(encoded) - cursor < 8:
            raise ValueError("runner-manifest-mismatch")
        field_length = struct.unpack(">Q", encoded[cursor : cursor + 8])[0]
        cursor += 8
        field_end = cursor + field_length
        if field_end > len(encoded):
            raise ValueError("runner-manifest-mismatch")
        fields.append(encoded[cursor:field_end])
        cursor = field_end
    return tuple(fields)
