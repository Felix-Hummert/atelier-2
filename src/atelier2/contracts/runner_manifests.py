from __future__ import annotations

import re
import struct
from dataclasses import dataclass

from atelier2.contracts.agent_attempts import RunnerManifestId
from atelier2.contracts.hashing import frame

_COMMIT = re.compile(r"[0-9a-f]{40}")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_CAPABILITIES = re.compile(r"[0-9a-f]{16}")


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


def runner_manifest_id(manifest: RunnerManifestV1) -> RunnerManifestId:
    return RunnerManifestId.of(encode_runner_manifest(manifest))
