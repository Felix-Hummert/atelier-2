from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

from atelier2.contracts.agent_attempts import RunnerManifestId
from atelier2.contracts.hashing import frame
from atelier2.contracts.runner_session_codec import RUNNER_SESSION_FRAME_DOMAIN

_COMMIT = re.compile(r"[0-9a-f]{40}")
_IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_CAPABILITIES = re.compile(r"[0-9a-f]{16}")
_DOTTED_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})(\.(0|[1-9][0-9]{0,8})){2}")
_FRAME_PREFIX = b"ATELIER2\x00"
_DOMAIN = b"runner-manifest/v1"
_FIXED_FIELD_COUNT = 25
_MAXIMUM_PATH_BYTES = 4096
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
CANDIDATE_SCRATCH_BYTES = 67_108_864


class RunnerPathRight(StrEnum):
    """What a Runner's provider child may do beneath one attested path.

    Execution is its own right rather than a property of being readable,
    because the two differ exactly where it matters: the image root holds the
    interpreter and the provider CLI and must be executable, while a surface
    that carries a provider's own configuration -- plugins, hooks, shell
    snippets a real credential directory is full of -- must be readable and
    never runnable. A mount option cannot carry that distinction here, because
    the one host surface ADR 0009 sec. 2 admits is a bind mount and the
    launcher cannot mount it `noexec`, so the right does.
    """

    READ_AND_EXECUTE = "read-and-execute"
    READ_ONLY = "read-only"
    READ_WRITE = "read-write"


@dataclass(frozen=True)
class RunnerPathGrant:
    """One attested path of a provider child's filesystem surface, with its right."""

    path: PurePosixPath
    right: RunnerPathRight

    def __post_init__(self) -> None:
        if not isinstance(self.path, PurePosixPath) or not self.path.is_absolute():
            raise ValueError("a runner path grant must name an absolute POSIX path")
        if ".." in self.path.parts:
            raise ValueError("a runner path grant must name a normalized path")
        if len(self.path.as_posix().encode("utf-8")) > _MAXIMUM_PATH_BYTES:
            raise ValueError("a runner path grant must name a bounded path")


CANDIDATE_SCRATCH_DIRECTORY = PurePosixPath("/tmp")
CANDIDATE_CREDENTIAL_DIRECTORY = PurePosixPath("/run/atelier2-provider-config")
# The A candidate image's complete provider-child surface. Only the image root
# is executable. Exactly one entry is writable -- a `noexec,nosuid` tmpfs the
# launcher mounts and its own inspect attestation re-reads -- so executable code
# stays in the read-only image root and the scratch surface can hold data only.
# The credential directory is read-only, and specifically not executable: ADR
# 0009 sec. 2's 2026-08-22 amendment decided the provider's credential directory
# is bind-mounted read-only (a write-capable per-Attempt copy waits on its own
# operator ruling), and a real one carries plugins, hooks and shell snippets
# that this child must be able to read and must never be able to run.
CANDIDATE_CHILD_PATH_GRANTS = (
    RunnerPathGrant(PurePosixPath("/dev"), RunnerPathRight.READ_ONLY),
    RunnerPathGrant(PurePosixPath("/lib"), RunnerPathRight.READ_AND_EXECUTE),
    RunnerPathGrant(PurePosixPath("/lib64"), RunnerPathRight.READ_AND_EXECUTE),
    RunnerPathGrant(PurePosixPath("/opt"), RunnerPathRight.READ_AND_EXECUTE),
    RunnerPathGrant(PurePosixPath("/proc"), RunnerPathRight.READ_ONLY),
    RunnerPathGrant(CANDIDATE_CREDENTIAL_DIRECTORY, RunnerPathRight.READ_ONLY),
    RunnerPathGrant(CANDIDATE_SCRATCH_DIRECTORY, RunnerPathRight.READ_WRITE),
    RunnerPathGrant(PurePosixPath("/usr"), RunnerPathRight.READ_AND_EXECUTE),
    RunnerPathGrant(PurePosixPath("/workspace"), RunnerPathRight.READ_ONLY),
)


@dataclass(frozen=True)
class MeasuredProviderCli:
    """The provider CLI version a Runner really ran `--version` for before READY."""

    version: tuple[int, int, int]

    def __post_init__(self) -> None:
        if len(self.version) != 3 or any(
            type(part) is not int or part < 0 for part in self.version
        ):
            raise ValueError("a measured provider CLI version must be three integers")

    def encode(self) -> bytes:
        return ".".join(str(part) for part in self.version).encode("ascii")


@dataclass(frozen=True)
class AbsentProviderCli:
    """The manifest's executor starts no provider CLI, so READY measured none."""

    def encode(self) -> bytes:
        return _ABSENT_PROVIDER_CLI_FIELD


MeasuredProviderCliVersion = MeasuredProviderCli | AbsentProviderCli

_ABSENT_PROVIDER_CLI_FIELD = b"absent"
ABSENT_PROVIDER_CLI = AbsentProviderCli()


@dataclass(frozen=True)
class ProviderCliPin:
    """The one provider CLI an executor revision runs, and the versions it admits."""

    executable_name: str
    conformant_versions: frozenset[tuple[int, int, int]]

    def admits(self, measured: MeasuredProviderCliVersion) -> bool:
        return (
            isinstance(measured, MeasuredProviderCli)
            and measured.version in self.conformant_versions
        )


@dataclass(frozen=True)
class NoProviderCliPin:
    """An executor revision that runs no provider CLI admits only an absent one."""

    def admits(self, measured: MeasuredProviderCliVersion) -> bool:
        return isinstance(measured, AbsentProviderCli)


RunnerExecutorCliPin = ProviderCliPin | NoProviderCliPin


def encode_measured_provider_cli(measured: MeasuredProviderCliVersion) -> bytes:
    return measured.encode()


def decode_measured_provider_cli(encoded: bytes) -> MeasuredProviderCliVersion:
    """Read one READY measurement back, refusing every other byte sequence.

    The absent form is a declared token rather than an empty or zero version,
    so a Runner that measured nothing can never be mistaken for one that
    measured a release numbered like nothing.
    """
    if encoded == _ABSENT_PROVIDER_CLI_FIELD:
        return ABSENT_PROVIDER_CLI
    reported = encoded.decode("ascii", "replace")
    if _DOTTED_VERSION.fullmatch(reported) is None:
        raise ValueError("runner-provider-cli-noncanonical")
    major, minor, patch = (int(part) for part in reported.split("."))
    return MeasuredProviderCli((major, minor, patch))


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
    provider_credential_directory: PurePosixPath
    scratch_bytes: int
    child_path_grants: tuple[RunnerPathGrant, ...]

    def __post_init__(self) -> None:
        self._validate_child_surface()
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
            "scratch_bytes",
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

    def _validate_child_surface(self) -> None:
        """Pin the child's whole filesystem surface to one canonical, named set.

        Ordering and uniqueness are part of the manifest identity rather than a
        decoding convenience: two carriers granting the same surface must reach
        the same `RunnerManifestId`, and a repeated path could otherwise carry
        two different rights and let the wider one win silently.

        The credential directory must be granted exactly `READ_ONLY`: readable,
        never writable and never executable. ADR 0009 sec. 2's 2026-08-22
        amendment decided exactly one extra host surface beyond the
        per-invocation identity material -- the provider's own credential
        directory, bind-mounted read-only -- because a live operator session may
        hold that directory open. A write-capable per-Attempt copy is reserved
        for a later operator ruling, so this contract keeps it unrepresentable.

        Refusing `READ_AND_EXECUTE` here is the half a mount option cannot
        carry. That surface is the one host bind in the container, and a bind
        mount cannot be made `noexec` through the launcher, so a real
        credential directory's plugins, hooks and shell snippets would be
        runnable by the provider child unless the right itself forbids it.
        Putting it in the right rather than in a convention is what makes the
        property part of the manifest identity Core selected.
        """
        grants = self.child_path_grants
        if not grants:
            raise ValueError("runner manifest must grant the child at least one path")
        paths = tuple(grant.path.as_posix() for grant in grants)
        if list(paths) != sorted(set(paths)):
            raise ValueError(
                "runner manifest child path grants must be sorted and unique"
            )
        required_grant = RunnerPathGrant(
            self.provider_credential_directory, RunnerPathRight.READ_ONLY
        )
        if required_grant not in grants:
            raise ValueError(
                "runner manifest credential directory must be a granted read-only path"
            )


def encode_runner_manifest(manifest: RunnerManifestV1) -> bytes:
    """Encode the selected facts under their one manifest identity domain.

    The third field attests which session-wire generation this manifest was
    selected for (`contracts/runner_session_codec.py`'s own
    `RUNNER_SESSION_FRAME_DOMAIN`, reused rather than copied here so the two
    can never drift apart the way #672's delta review caught) -- a runner a
    manifest names must speak the exact PREPARE shape Core's session codec
    now emits, not a retired one.
    """
    return frame(
        "runner-manifest/v1",
        manifest.source_commit.encode("ascii"),
        manifest.image_digest.encode("ascii"),
        RUNNER_SESSION_FRAME_DOMAIN,
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
        manifest.provider_credential_directory.as_posix().encode("utf-8"),
        struct.pack(">Q", manifest.scratch_bytes),
        struct.pack(">Q", len(manifest.child_path_grants)),
        *_encoded_grant_fields(manifest.child_path_grants),
    )


def _encoded_grant_fields(grants: tuple[RunnerPathGrant, ...]) -> tuple[bytes, ...]:
    """Each path and each right in its own length-prefixed field.

    A path may hold any byte a filesystem accepts, so the allowlist never
    becomes one field with an in-band separator that a crafted path could
    imitate.
    """
    return tuple(
        field
        for grant in grants
        for field in (
            grant.path.as_posix().encode("utf-8"),
            grant.right.value.encode("ascii"),
        )
    )


def _decoded_grants(fields: tuple[bytes, ...]) -> tuple[RunnerPathGrant, ...]:
    return tuple(
        RunnerPathGrant(
            PurePosixPath(fields[index].decode("utf-8")),
            RunnerPathRight(fields[index + 1].decode("ascii")),
        )
        for index in range(0, len(fields), 2)
    )


def decode_runner_manifest(encoded: bytes) -> RunnerManifestV1:
    """Decode one canonical manifest and refuse any other byte sequence."""
    fields = _decode_manifest_fields(encoded)
    if len(fields) < _FIXED_FIELD_COUNT or fields[2] != RUNNER_SESSION_FRAME_DOMAIN:
        raise ValueError("runner-manifest-mismatch")
    if len(fields[23]) != 8 or len(fields[24]) != 8:
        raise ValueError("runner-manifest-mismatch")
    grant_count = struct.unpack(">Q", fields[24])[0]
    if len(fields) != _FIXED_FIELD_COUNT + 2 * grant_count:
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
            PurePosixPath(fields[22].decode("utf-8")),
            struct.unpack(">Q", fields[23])[0],
            _decoded_grants(fields[_FIXED_FIELD_COUNT:]),
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
    provider_credential_directory: PurePosixPath = CANDIDATE_CREDENTIAL_DIRECTORY,
    scratch_bytes: int = CANDIDATE_SCRATCH_BYTES,
    child_path_grants: tuple[RunnerPathGrant, ...] = CANDIDATE_CHILD_PATH_GRANTS,
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
        provider_credential_directory,
        scratch_bytes,
        child_path_grants,
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
