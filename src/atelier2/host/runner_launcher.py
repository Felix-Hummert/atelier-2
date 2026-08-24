"""The host launcher: the one process that turns a Runner lease into an Attempt.

It runs beside the console, not inside it. Serve holds no carrier authority at
all -- no engine socket, no broker, no privileged helper (ADR 0009 sec. 2,
`#540` operator ruling B). Instead this process claims open Runner leases,
and for each one establishes the whole per-Attempt ensemble: the Attempt
network with its policy, the volumes, the Runner container, the identity that
one invocation may present, the inspect attestation Core admits the session on,
and the complete removal of all of it afterwards.

The lease source is the portability seam. Locally it is a directory of lease
documents; a cluster plays the same role over the same interface. Deploying
elsewhere replaces that adapter, never this arrangement.

A lease is a trusted input, and asking for something is not authority to get
it: whoever writes leases -- an operator's script today, Serve itself from
`#540` C-3 -- is asking this process to mount host directories, to start an
image as root over an Attempt's volumes, and to attach a container to a
network. Serve is precisely the component the arrangement refuses to trust with
the carrier, so the launcher validates what a lease names against what the
operator declared at start (`RunnerLeaseValidation`) instead of believing the
document.

What that protects is the HOST, not the Attempt. Serve writes the attempt root
this process mounts, so a compromised Serve distorts its own Attempts and no
launcher can stop it; what the launcher does stop is a lease reaching past the
Attempt it is about, or asking this host for more than the operator declared it
carries (ADR 0009 sec. 2, 2026-08-23 amendment (a)).

What this process is trusted with is deliberately narrow: it never reads the
product's own state, never runs provider code, and never hands an Attempt
anything but the material one invocation needs. Exactly one of it owns a lease
directory at a time (`single_launcher`), because reconciliation cannot tell a
second launcher's live Attempt from an abandoned one.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import sys
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from atelier2.adapters.docker_carrier import (
    AttemptAttachment,
    AttemptChains,
    AttemptNetwork,
    BindMount,
    CarrierRefusal,
    ContainerHardening,
    ContainerRole,
    ContainerSpec,
    DockerCarrier,
    MountRight,
    TmpfsMount,
    TmpfsVolumeOptions,
    VolumeMount,
    attempt_chains,
    attest_runner_container,
)
from atelier2.contracts.agent_attempts import RunnerInvocationId
from atelier2.contracts.runner_leases import (
    RunnerLease,
    RunnerLeaseId,
    decode_runner_binding,
    decode_runner_lease_document,
    lease_label,
)
from atelier2.contracts.runner_manifests import (
    CANDIDATE_CPU_PERIOD,
    CANDIDATE_CPU_QUOTA,
    CANDIDATE_JOURNAL_BYTES,
    CANDIDATE_MEMORY_BYTES,
    CANDIDATE_PROCESS_LIMIT,
    CANDIDATE_SCRATCH_BYTES,
    CANDIDATE_WORKSPACE_BYTES,
    RunnerManifestV1,
    RunnerPathRight,
    decode_runner_manifest,
    runner_manifest_id,
)
from atelier2.contracts.runner_terminal_evidence_codec import (
    MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES,
)
from atelier2.host.runner_identity import (
    RunnerIdentityAuthority,
    console_identity_expiry,
    receiver_record,
    unlink_private_keys,
)

# The paths an Attempt's Runner image declares as its own mount points. They are
# the Runner entry point's own argument vector (`Dockerfile.runner`), not a
# choice this launcher may make per Attempt.
_HANDOFF_PATH = PurePosixPath("/handoff")
_IDENTITY_PATH = PurePosixPath("/run/atelier2-identity")
_JOURNAL_PATH = PurePosixPath("/journal")
_OFFER_PATH = PurePosixPath("/offer")
_WORKSPACE_PATH = PurePosixPath("/workspace")
_INVOCATION_OFFER = _OFFER_PATH / "invocation.json"
_IDENTITY_RECEIVER_ENTRYPOINT = "atelier2-runner-identity-receiver"
_IDENTITY_RECEIVER_DESTINATION = PurePosixPath("/identity")
_ATTESTATION_NAME = "inspect-attested"
_JOURNAL_TERMINAL_RECORD = _JOURNAL_PATH / "terminal-record"
# Where the launcher lays a Runner's own retained terminal record down in the
# handoff directory, for Serve to converge over after a restart it never
# reconciled (`#540` Kind #585). The bytes are the journal record verbatim, so
# Serve decodes exactly what the Runner wrote; the name is this launcher's half
# of a filename contract Serve reads under the same value
# (`atelier2.adapters.file_runner_terminal_evidence`), never an import across
# the seam -- `host` sits above `adapters`, the same way `inspect-attested` is
# already shared by value at `file_runner_leases._ATTESTATION_NAME`.
_RETAINED_TERMINAL_RECORD_NAME = "retained-terminal-record"
# The suffix this launcher's own durable journal volume carries, and the
# handoff directory segment Serve lays each Attempt's material under. Both are
# reused when reconcile has only a lease id to work from: the journal volume is
# found among this Attempt's labelled volumes by its suffix, and the handoff
# directory is the same `attempt-root/<lease-id>/handoff` Serve wrote and the
# launcher validated (`atelier2.adapters.file_runner_leases`, shared by value
# across the seam, never by import).
_JOURNAL_VOLUME_SUFFIX = "-journal"
_HANDOFF_DIRECTORY_NAME = "handoff"
_HANDOFF_NAMES = (
    "ca.crt",
    "core.crt",
    "manifest",
    "core-peer.json",
    "bootstrap.json",
)
# What the Runner writes its one offer into, and the bound this launcher reads
# it back under: the offer is a small fixed record, and a Runner that wrote
# something larger is refused rather than read.
_OFFER_BYTES = 1_048_576
_MAXIMUM_OFFER_BYTES = 4_096
# The bound every document a lease writer names is read under -- the lease
# itself, the binding Core published, and the manifest with its whole path
# allowlist. Generous next to all three, and far below what a writer could
# otherwise make one claim cost this host in memory.
_MAXIMUM_LEASE_DOCUMENT_BYTES = 65_536
# The form a Runner's own offered invocation must have. The Runner mints it as
# `secrets.token_urlsafe` (`atelier2.runner.__main__`), and this is where that
# value crosses from the least trusted process on the host into a certificate:
# a URI-SAN takes ASCII, and the shared `RunnerInvocationId` admits far more
# than that, so the launcher states what it will actually put in a leaf.
_OFFERED_INVOCATION = re.compile(r"[A-Za-z0-9_-]{1,256}")
_HANDOFF_BYTES = 1_048_576
_TMPFS_MODE = 0o1777
_HANDOFF_DEADLINE_SECONDS = 30.0
_LEASE_POLL_SECONDS = 1.0
# One launcher owns one lease directory. The lock is an advisory `flock` on an
# open descriptor rather than a file whose content claims ownership, so the
# kernel releases it even when a launcher is SIGKILLed and never leaves a
# stale claim only a human could clear.
_LAUNCHER_LOCK_NAME = ".launcher.lock"


class AttemptRefusal(Exception):
    """A named refusal of one Attempt, carrying which lease it was about."""


def admitted_lease_id(named: str) -> RunnerLeaseId:
    """One lease's identity, in the only form a launcher acts on.

    Every container, volume, label and packet-filter chain of an Attempt is
    derived from a lease id, and those names reach a Docker argument vector
    and the Attempt policy's own `sh` program. `RunnerLease.lease_id` carries
    `RunnerLeaseId`, so no other value can build them; this is the one place a
    name a lease source read becomes one -- at both entrances a name can
    arrive through -- and a name outside the form is answered for as that
    document rather than as a `ValueError` out of a contract (`#540` D-4).
    """
    try:
        return RunnerLeaseId(named)
    except ValueError as malformed:
        raise AttemptRefusal(
            f"lease-name-is-not-a-lease-id-form: {named!r}"
        ) from malformed


@dataclass(frozen=True, slots=True)
class RunnerManifestBounds:
    """The largest Attempt this host will carry, as the operator declares it.

    Serve chooses the manifest of its own Attempts, and the manifest carries
    every resource number the launcher hands the engine. A manifest asking for
    the host's whole memory, a tmpfs the size of its RAM, or a process limit
    nothing survives is not a corrupted Attempt -- it is a corrupted host. The
    manifest fence (`RunnerLeaseValidation.validated`) proves an Attempt is the
    one Core bound; these bounds decide whether this host carries it at all.

    `journal_bytes` is the one of these the engine never sees: the local volume
    driver has no size for a disk-backed volume, and the journal has to be one
    because the Runner's own restart must find it (`resume`). The Runner keeps
    that capacity itself, against the manifest it was handed
    (`atelier2.adapters.runner_journal`), so bounding the number here is what
    keeps a lease from choosing how much of this host's disk that promise
    covers.

    The defaults are the candidate Attempt's own numbers: a deployment that
    declares nothing runs exactly what this repository already attests.
    """

    memory_bytes: int = CANDIDATE_MEMORY_BYTES
    process_limit: int = CANDIDATE_PROCESS_LIMIT
    cpu_quota_microseconds: int = CANDIDATE_CPU_QUOTA
    scratch_bytes: int = CANDIDATE_SCRATCH_BYTES
    writable_surface_bytes: int = CANDIDATE_SCRATCH_BYTES
    journal_bytes: int = CANDIDATE_JOURNAL_BYTES

    def admitted(self, manifest: RunnerManifestV1) -> None:
        """Refuse a manifest asking for more than this host declared it carries."""
        for field, asked, bound in (
            ("memory_bytes", manifest.memory_bytes, self.memory_bytes),
            ("process_limit", manifest.process_limit, self.process_limit),
            (
                "cpu_quota_microseconds",
                manifest.cpu_quota_microseconds,
                self.cpu_quota_microseconds,
            ),
            ("scratch_bytes", manifest.scratch_bytes, self.scratch_bytes),
            (
                "writable_surface_bytes",
                _writable_surface_bytes(manifest),
                self.writable_surface_bytes,
            ),
            ("journal_bytes", manifest.journal_bytes, self.journal_bytes),
        ):
            if asked > bound:
                raise AttemptRefusal(
                    f"lease-manifest-exceeds-a-declared-bound: {field}={asked} "
                    f"exceeds {bound}"
                )


def _writable_surface_bytes(manifest: RunnerManifestV1) -> int:
    """Every byte of tmpfs the Attempt's writable grants together ask the host for.

    Each writable grant becomes one tmpfs of `scratch_bytes`, and tmpfs is
    host memory: bounding one grant while the count is free would bound
    nothing.
    """
    return sum(
        manifest.scratch_bytes
        for grant in manifest.child_path_grants
        if grant.right is RunnerPathRight.READ_WRITE
    )


@dataclass(frozen=True, slots=True)
class RunnerLeaseValidation:
    """What this launcher admits about a lease, before one is acted on.

    A lease is a trusted input: it names host directories this process mounts
    into a container, the image it starts as root to own their volumes, and the
    console container it attaches to an Attempt network. A lease that could
    name any path would be a mount of the host's own root into an Attempt, one
    that could name any image would be that image running as root over the
    Attempt's volumes, and one that could name any container would be
    `CAP_NET_ADMIN` inside a namespace nobody meant. Today an operator script
    writes the leases; from `#540` C-3 a Serve process does, and Serve is
    exactly the component this whole arrangement refuses to trust with the
    carrier. So the launcher decides what it accepts rather than believing the
    document: every path must resolve inside the operator's declared attempt
    root, image and console container must be the ones named at start, the
    manifest must be the one Core bound, and what it asks the host for must
    stay inside the operator's declared bounds.
    """

    attempt_root: Path
    console_container: str
    console_network: str
    runner_image: str
    bounds: RunnerManifestBounds

    def validated_path(self, named: Path, field: str) -> Path:
        resolved = named.resolve()
        root = self.attempt_root.resolve()
        if not resolved.is_relative_to(root):
            raise AttemptRefusal(
                f"lease-names-a-path-outside-the-attempt-root: {field}={named}"
            )
        return resolved

    def validated_console(self, named: str) -> str:
        if named != self.console_container:
            raise AttemptRefusal(f"lease-names-another-console-container: {named}")
        return named

    def validated_image(self, named: str) -> str:
        if named != self.runner_image:
            raise AttemptRefusal(f"lease-names-another-runner-image: {named}")
        return named

    def validated_manifest(self, lease: RunnerLease) -> RunnerManifestV1:
        """The manifest, once it is proven to be the one Core bound.

        The lease carries the manifest document and the identity Core selected
        separately, and every number the launcher hands the engine comes from
        the document. Reading them against each other is what makes the
        Attempt the engine creates the Attempt Core decided on, rather than
        whatever a lease writer put beside a matching identity.
        """
        if runner_manifest_id(lease.manifest) != lease.binding.manifest_id:
            raise AttemptRefusal(
                f"lease-manifest-is-not-the-one-core-bound: {lease.lease_id.value}"
            )
        self.bounds.admitted(lease.manifest)
        return lease.manifest

    def validated(self, lease: RunnerLease) -> RunnerLease:
        """The same lease, carrying the paths that were actually admitted.

        The resolved form replaces what the document said, because those are
        two different paths: a name whose own last component is a symlink out
        of the attempt root is refused here rather than mounted.

        What this does not carry is the moment of the mount. A resolved path
        is a string, and the engine resolves it again when it binds it, so a
        writer who owns a directory *inside* the attempt root can swap an
        intermediate component between this check and that bind and have the
        engine follow the new one. Serve owns exactly that root, so this is a
        read of one host directory by a compromised Serve -- inside amendment
        (a)'s boundary, and above the line an operator should be told about
        (`docs/OPERATIONS.md`). Closing it needs the attempt root to be built
        by the launcher rather than validated after Serve built it, which is
        `#540` C-3.2/C-3.6's, not a check that can be added here.
        """
        self.validated_console(lease.serve_container)
        self.validated_image(lease.runner_image)
        self.validated_manifest(lease)
        return replace(
            lease,
            handoff_directory=self.validated_path(
                lease.handoff_directory, "handoff_directory"
            ),
            core_peer_directory=self.validated_path(
                lease.core_peer_directory, "core_peer_directory"
            ),
            issuance_directory=self.validated_path(
                lease.issuance_directory, "issuance_directory"
            ),
            provider_credential_source=self.validated_path(
                lease.provider_credential_source, "provider_credential_source"
            ),
        )


def admitted_attempt_root(
    attempt_root: Path, certificate_authority_state: Path
) -> Path:
    """The attempt root, once it is proven to hold only per-Attempt material.

    Everything under this root is a surface a lease may ask to have mounted
    into an Attempt. The authority's own key directory is the one thing on this
    host that must never become one, and a root containing it would admit a
    lease that names the root itself -- binding the authority into the Attempt
    along with everything else. The two are therefore required to be disjoint
    trees, refused at start rather than discovered by the lease that used it.
    """
    root = attempt_root.resolve()
    authority = certificate_authority_state.resolve()
    if root.is_relative_to(authority) or authority.is_relative_to(root):
        raise AttemptRefusal(
            f"attempt-root-contains-the-authority: {attempt_root} and "
            f"{certificate_authority_state} must be disjoint trees"
        )
    return root


def admitted_console_identity(identity_directory: Path, now: datetime) -> datetime:
    """When the console's own leaf stops being admissible, refused if it already has.

    The console's identity is what Core presents to every Runner of every
    Attempt this launcher establishes. One that has expired does not fail here
    -- it fails deep inside each Attempt, as a handshake nobody can read a
    renewal instruction out of. This launcher owns the authority that issues
    it, so it is the one place that can say so at start and name the command
    that fixes it.
    """
    try:
        expiry = console_identity_expiry(identity_directory)
    except (OSError, ValueError) as unreadable:
        raise AttemptRefusal(
            f"console-identity-unreadable: {identity_directory}: {unreadable}"
        ) from unreadable
    if expiry <= now:
        raise AttemptRefusal(
            f"console-identity-expired: {identity_directory} expired "
            f"{expiry.isoformat()}; renew it with issue-console-identity"
        )
    return expiry


@contextlib.contextmanager
def single_launcher(lease_directory: Path) -> Iterator[None]:
    """Hold this host's one claim on a lease directory, for as long as it runs.

    Reconciliation identifies an abandoned Attempt by its lease sitting in
    `claimed`, which is exactly what a working launcher's lease looks like: a
    second launcher beside a first would read a live Attempt as abandoned and
    remove the containers, volumes and network out from under it. So a
    launcher takes an exclusive advisory lock on an open descriptor before it
    reconciles anything, and refuses by name when another already holds it.

    `flock` is the right shape for that ownership because it is the kernel's:
    it disappears with the process that held it, including one that was
    SIGKILLed, so a crashed launcher never leaves a claim a human has to
    clear. Its bound is this host -- a launcher fleet needs the ownership
    token `#540` C-2 named, not a lock the other host cannot see.
    """
    lease_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(
        lease_directory / _LAUNCHER_LOCK_NAME,
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC,
        0o600,
    )
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as held:
            raise AttemptRefusal(
                f"another-launcher-holds-this-lease-directory: {lease_directory}"
            ) from held
        yield
    finally:
        os.close(lock)


class RunnerLeaseSource(Protocol):
    """Where open Runner leases come from, and where finished ones go back.

    This is the portability seam (`#540` C-3): a directory locally, a Serve
    endpoint or a cluster scheduler elsewhere. A claim is exclusive -- two
    launchers may watch the same source, and only one of them may ever
    establish a given Attempt.
    """

    def claim_open_lease(self) -> RunnerLease | None: ...

    def release(self, lease: RunnerLease) -> None: ...

    def abandon_stale_claims(self) -> tuple[str, ...]: ...


class AttemptCarrier(Protocol):
    """Every engine operation a launcher may perform, and nothing besides.

    Written out as one surface on purpose: this is the whole of the privilege
    the arrangement grants the launcher role, so it can be read in one place
    rather than inferred from call sites. `DockerCarrier` satisfies it
    structurally -- the launcher names no engine of its own.
    """

    def create_attempt_network(self, name: str, label: str) -> AttemptNetwork: ...

    def attach_policed_container(
        self, container: str, attachment: AttemptAttachment
    ) -> None: ...

    def remove_attempt_policy(self, container: str, chains: AttemptChains) -> None: ...

    def detach_container(self, container: str, network: str) -> None: ...

    def create_volume(
        self, name: str, label: str, tmpfs: TmpfsVolumeOptions | None = None
    ) -> None: ...

    def own_volume(self, name: str, image: str, uid: int, gid: int) -> None: ...

    def start_policed_container(
        self, spec: ContainerSpec, attachment: AttemptAttachment
    ) -> str: ...

    def restart_private_container(self, container: str) -> None: ...

    def wait_for_exit(self, container: str) -> int: ...

    def inspect_container(self, container: str) -> dict[str, Any]: ...

    def copy_into_container(
        self,
        container: str,
        sources: Sequence[Path],
        destination: PurePosixPath,
        deadline_seconds: float,
    ) -> None: ...

    def read_file_in_container(
        self,
        container: str,
        path: PurePosixPath,
        user: str,
        maximum_bytes: int,
        deadline_seconds: float,
    ) -> bytes: ...

    def run_receiving_stdin(self, spec: ContainerSpec, stdin: bytes) -> None: ...

    def file_exists_in_volume(
        self, volume: str, image: str, path: PurePosixPath
    ) -> bool: ...

    def read_file_in_volume(
        self, volume: str, image: str, path: PurePosixPath, maximum_bytes: int
    ) -> bytes: ...

    def container_running(self, container: str) -> bool: ...

    def labelled_containers(self, label: str) -> tuple[str, ...]: ...

    def labelled_volumes(self, label: str) -> tuple[str, ...]: ...

    def labelled_networks(self, label: str) -> tuple[str, ...]: ...

    def remove_containers(self, containers: Iterable[str]) -> None: ...

    def remove_volumes(self, volumes: Iterable[str]) -> None: ...

    def remove_networks(self, networks: Iterable[str]) -> None: ...


class FileRunnerLeaseSource:
    """Runner leases as documents in a directory, for a single-host install.

    A lease is claimed by renaming its document from `open` into `claimed`,
    which the filesystem does atomically: a second launcher racing for the same
    lease loses the rename and sees no lease at all, rather than establishing a
    second Attempt for one generation.

    A document still sitting in `claimed` when a launcher starts belonged to an
    earlier launcher that died. Its Attempt is not resumed -- an Attempt that
    may already have run must not be silently run again -- so reconciliation
    releases it and removes what it left behind. What its owner then does with
    an interrupted lease is that owner's decision (`#540` C-3).
    """

    def __init__(self, directory: Path, validation: RunnerLeaseValidation) -> None:
        self._validation = validation
        self._open = directory / "open"
        self._claimed = directory / "claimed"
        self._released = directory / "released"
        for state in (self._open, self._claimed, self._released):
            state.mkdir(mode=0o700, parents=True, exist_ok=True)

    def claim_open_lease(self) -> RunnerLease | None:
        for document in sorted(self._open.glob("*.json")):
            claimed = self._claimed / document.name
            try:
                document.rename(claimed)
            except OSError:
                continue
            return self._decode(claimed)
        return None

    def release(self, lease: RunnerLease) -> None:
        document = self._claimed / f"{lease.lease_id.value}.json"
        document.rename(self._released / document.name)

    def abandon_stale_claims(self) -> tuple[str, ...]:
        stale = sorted(document.stem for document in self._claimed.glob("*.json"))
        for lease_id in stale:
            document = self._claimed / f"{lease_id}.json"
            document.rename(self._released / document.name)
        return tuple(stale)

    def _decode(self, document: Path) -> RunnerLease:
        """One lease document, admitted before a single byte of it is followed.

        The document's own name is admitted first, because it is this Attempt's
        identity and every object name is built from it. The two paths this
        reads are checked next: a lease that could point `manifest_path` or
        `binding_path` anywhere would make this adapter read whatever the
        writer chose, and reading is already acting on it. Each of the three
        documents is read only as far as this launcher would ever act on, so a
        writer cannot hand this process a file the size of its memory.
        """
        lease_id = admitted_lease_id(document.stem)
        try:
            fields = decode_runner_lease_document(_bounded_bytes(document))
            binding_path = self._validation.validated_path(
                fields.binding_path, "binding_path"
            )
            manifest_path = self._validation.validated_path(
                fields.manifest_path, "manifest_path"
            )
            lease = RunnerLease(
                lease_id,
                decode_runner_binding(_bounded_bytes(binding_path)),
                fields.runner_image,
                decode_runner_manifest(_bounded_bytes(manifest_path)),
                fields.serve_container,
                fields.handoff_directory,
                fields.core_peer_directory,
                fields.issuance_directory,
                fields.provider_credential_source,
            )
        except AttemptRefusal:
            # Already a named answer about this document -- a path outside the
            # attempt root, or a file over the bound -- and re-wrapping it would
            # bury which of the two it was.
            raise
        except Exception as unreadable:
            # Truncated JSON, a missing field, a manifest that will not decode,
            # a nesting depth that exhausts the parser's own stack: the writer
            # handed this process something it cannot act on, which is one
            # refused lease and never a stopped launcher. Every way that can
            # happen is caught, not a list of the ways already seen -- a list
            # is a guess about a parser's failure modes, and this one has
            # already been wrong (`json` answers a nested document with
            # `RecursionError`). The document has already been renamed into
            # `claimed`, so it stays quarantined rather than being picked up
            # again on the next poll.
            raise AttemptRefusal(
                f"lease-document-unreadable: {document.stem}: {unreadable!r}"
            ) from unreadable
        return self._validation.validated(lease)


def _bounded_bytes(path: Path) -> bytes:
    """One document a lease writer chose, read only as far as it can be acted on.

    Every file this reads was named by the same writer the launcher refuses to
    believe, and a read with no bound is that writer choosing how much of this
    host's memory a claim costs. The bound is generous next to the largest
    document that has meaning here -- a manifest with its whole path
    allowlist -- and a file over it is one refused lease.
    """
    with path.open("rb") as handle:
        payload = handle.read(_MAXIMUM_LEASE_DOCUMENT_BYTES + 1)
    if len(payload) > _MAXIMUM_LEASE_DOCUMENT_BYTES:
        raise AttemptRefusal(
            f"lease-document-exceeds-bound: {path} is larger than "
            f"{_MAXIMUM_LEASE_DOCUMENT_BYTES} bytes"
        )
    return payload


def _offered_invocation(offer: bytes) -> RunnerInvocationId:
    """The invocation the Runner published, or one refused Attempt.

    The offer is written inside the Attempt's own Runner container, which is
    the least trusted process this host runs. A document that is not the one
    record this expects therefore costs that Attempt and never the launcher --
    which from `#540` C-3 serves every other Attempt on the host. Every way a
    document can fail to be that record is one refusal, rather than the ways
    already thought of.

    The value is also held to the form the launcher will really put in a
    certificate. `RunnerInvocationId` admits any canonical UTF-8 up to a
    thousand characters, because it is the shape Core stores; a URI-SAN takes
    ASCII, so a Runner offering anything else would otherwise reach `x509`
    and end this process instead of its own Attempt.
    """
    try:
        offered = json.loads(offer)["invocation_id"]
        if (
            not isinstance(offered, str)
            or _OFFERED_INVOCATION.fullmatch(offered) is None
        ):
            raise ValueError(f"outside the offered invocation form: {offered!r}")
        return RunnerInvocationId(offered)
    except Exception as unreadable:
        raise AttemptRefusal(f"runner-offer-unreadable: {unreadable!r}") from unreadable


def _writable_grants(manifest: RunnerManifestV1) -> Iterator[TmpfsMount]:
    for grant in manifest.child_path_grants:
        if grant.right is RunnerPathRight.READ_WRITE:
            yield TmpfsMount(grant.path, manifest.scratch_bytes, _TMPFS_MODE)


@dataclass(frozen=True, slots=True)
class AttemptVolumes:
    identity: str
    handoff: str
    journal: str


def runner_container_spec(
    lease: RunnerLease, container: str, volumes: AttemptVolumes
) -> ContainerSpec:
    """The one container an Attempt's Runner is allowed to be.

    Every number in it comes from the manifest Core bound, never from this
    process, so the container the engine creates is the container the
    attestation reads back and Core selected.
    """
    manifest = lease.manifest
    return ContainerSpec(
        container,
        lease.runner_image,
        lease.label,
        ContainerHardening(
            user=f"{manifest.effective_uid}:{manifest.effective_gid}",
            read_only_root=True,
            drop_all_capabilities=True,
            no_new_privileges=True,
            process_limit=manifest.process_limit,
            memory_bytes=manifest.memory_bytes,
            cpu_period_microseconds=CANDIDATE_CPU_PERIOD,
            cpu_quota_microseconds=manifest.cpu_quota_microseconds,
        ),
        tmpfs=(
            *_writable_grants(manifest),
            TmpfsMount(_WORKSPACE_PATH, CANDIDATE_WORKSPACE_BYTES, _TMPFS_MODE),
            TmpfsMount(_OFFER_PATH, _OFFER_BYTES, _TMPFS_MODE),
        ),
        binds=(
            BindMount(
                lease.provider_credential_source,
                manifest.provider_credential_directory,
                MountRight.READ_ONLY,
            ),
        ),
        volumes=(
            VolumeMount(volumes.handoff, _HANDOFF_PATH, MountRight.READ_WRITE),
            VolumeMount(volumes.identity, _IDENTITY_PATH, MountRight.READ_ONLY),
            VolumeMount(volumes.journal, _JOURNAL_PATH, MountRight.READ_WRITE),
        ),
    )


@dataclass(frozen=True, slots=True)
class RunnerLauncher:
    """One launcher, holding exactly the two authorities its role needs."""

    carrier: AttemptCarrier
    authority: RunnerIdentityAuthority
    leases: RunnerLeaseSource
    validation: RunnerLeaseValidation
    announce: Callable[[str], None]

    def reconcile_abandoned_attempts(self) -> None:
        """Remove what an earlier launcher left behind before claiming anything.

        A launcher that died mid-Attempt left containers, volumes, a network
        and the console's own chains labelled with that lease. Nothing else may
        be touched: only objects carrying the exact lease label of a lease this
        source says is stale, and only for a name that is a lease id at all --
        the same fence the claim path applies, because a lease directory is
        written by the same untrusted writer at both ends.

        One residue nobody can clear is reported and stepped over. A console
        that is gone took its namespace with it, and a launcher that refused to
        start because of an Attempt from a previous deployment would be an
        outage over a leftover.

        An Attempt whose Runner is somehow still running is deferred whole: its
        terminal fact lives only on the journal volume this pass would delete, so
        removing it while the Runner may still be writing that fact would destroy
        it rather than retain it. The removal is left for a later pass, once
        every container this Attempt labelled has stopped.
        """
        for named in self.leases.abandon_stale_claims():
            try:
                lease_id = admitted_lease_id(named)
                self.announce(f"reconciled-attempt={lease_id.value}")
                if self._attempt_containers_still_running(lease_id):
                    self.announce(f"reconcile-deferred-running={lease_id.value}")
                    continue
                self._retain_terminal_record_before_delete(lease_id)
                self._remove_attempt(lease_id)
            except (AttemptRefusal, CarrierRefusal) as refusal:
                self.announce(f"reconcile-refused={refusal}")

    def serve_open_leases(self, once: bool, poll_seconds: float) -> int:
        """Establish leases until there are none left, or forever.

        A failed Attempt is loud and local: it is reported, its lease stays
        claimed and its objects stay on the host to be read, and this launcher
        keeps serving the next lease. One bad Attempt taking the whole host's
        launcher down with it would turn a single failure into an outage --
        and from `#540` C-3 the leases are written by Serve, so one document
        Serve got wrong must cost exactly one Attempt.

        Claiming is inside that same guard, because it is where a lease is read
        and admitted: a document naming a path nobody declared, or one that
        cannot be read at all, refuses there rather than in the Attempt it
        never became. Such a lease has already been renamed into `claimed`,
        which is where a refused lease belongs -- quarantined, not retried.

        Returns how many leases were refused, so a bounded run can answer for
        them.
        """
        failed = 0
        self.reconcile_abandoned_attempts()
        while True:
            acted, refused = self._serve_next_lease()
            failed += 1 if refused else 0
            if once:
                return failed
            if not acted:
                time.sleep(poll_seconds)

    def _serve_next_lease(self) -> tuple[bool, bool]:
        """Claim and establish the next lease; say whether it acted and refused.

        A lease that refuses while it is being claimed and one that refuses
        while its Attempt is established are reported apart, because they are
        different things to read in a log: one is a document this launcher
        would not accept, the other an Attempt that did not come up.
        """
        lease = None
        try:
            lease = self.leases.claim_open_lease()
            if lease is None:
                return False, False
            self.establish(lease)
        except (AttemptRefusal, CarrierRefusal) as refusal:
            claimed = lease is not None
            self.announce(
                f"attempt-failed={refusal}" if claimed else f"lease-refused={refusal}"
            )
            return True, True
        return True, False

    def establish(self, lease: RunnerLease) -> None:
        """Run one Attempt from an empty host to a released one.

        Everything the lease asks for is admitted before the first engine call,
        so an Attempt this launcher will not carry costs no object at all: the
        volume-owning container alone starts the lease's image as root.

        On success every object this created is removed and the lease goes
        back to its source. On failure the Attempt's own objects are
        deliberately left, named, for the operator to read -- but the console
        is not one of them. It outlives every Attempt, so a failure that left
        it holding this Attempt's chains and its network would refuse the next
        Attempt's own attestation, and the one after that, while a subnet the
        engine is free to hand out again kept an ACCEPT rule pointing at it.
        The console half is therefore given back before the failure is
        reported.
        """
        lease = self.validation.validated(lease)
        self.announce(f"attempt-lease={lease.lease_id.value}")
        network = self.carrier.create_attempt_network(lease.attempt_name, lease.label)
        self.announce(f"attempt-network={network.name}")
        chains = attempt_chains(lease.lease_id.value)
        try:
            self.carrier.attach_policed_container(
                lease.serve_container,
                AttemptAttachment(
                    chains, network, ContainerRole.CORE, self.validation.console_network
                ),
            )
            volumes = self._create_volumes(lease)
            container = f"{lease.attempt_name}-runner"
            specification = runner_container_spec(lease, container, volumes)
            attachment = AttemptAttachment(chains, network, ContainerRole.RUNNER)
            container_id = self.carrier.start_policed_container(
                specification, attachment
            )
            self._deliver_handoff(lease, container)
            self._issue_identity(lease, container, volumes)
            self._attest(lease, container_id)
            self._await_release(lease, container, attachment, volumes)
            self._remove_attempt(lease.lease_id)
        except (AttemptRefusal, CarrierRefusal):
            self._give_the_console_back(lease, network, chains)
            raise
        self.leases.release(lease)
        self.announce(f"attempt-released={lease.lease_id.value}")

    def _give_the_console_back(
        self, lease: RunnerLease, network: AttemptNetwork, chains: AttemptChains
    ) -> None:
        """Take the console out of an Attempt that failed, and say if it stuck.

        Only the console: what this Attempt created stays where an operator can
        read it. A removal that refuses is announced rather than raised,
        because the Attempt's own failure is the answer this call is running
        underneath -- and a console that kept something is a second fact, not a
        replacement for the first.
        """
        try:
            self.carrier.remove_attempt_policy(lease.serve_container, chains)
        except CarrierRefusal as refusal:
            self.announce(f"console-still-holds-the-attempt={refusal}")
        self.carrier.detach_container(lease.serve_container, network.name)

    def _create_volumes(self, lease: RunnerLease) -> AttemptVolumes:
        """This Attempt's three volumes, each in the form its content needs.

        Identity and journal are durable, because the Runner's own restart must
        find them intact; handoff is engine-backed tmpfs, because everything in
        it is reproducible from what the host already holds and none of it
        needs to reach a disk.
        """
        volumes = AttemptVolumes(
            f"{lease.attempt_name}-identity",
            f"{lease.attempt_name}-handoff",
            f"{lease.attempt_name}{_JOURNAL_VOLUME_SUFFIX}",
        )
        manifest = lease.manifest
        for durable in (volumes.identity, volumes.journal):
            self.carrier.create_volume(durable, lease.label)
            self.carrier.own_volume(
                durable,
                lease.runner_image,
                manifest.effective_uid,
                manifest.effective_gid,
            )
        self.carrier.create_volume(
            volumes.handoff,
            lease.label,
            TmpfsVolumeOptions(
                _HANDOFF_BYTES,
                manifest.effective_uid,
                manifest.effective_gid,
                _TMPFS_MODE,
            ),
        )
        return volumes

    def _deliver_handoff(self, lease: RunnerLease, container: str) -> None:
        self.carrier.copy_into_container(
            container,
            [lease.handoff_directory / name for name in _HANDOFF_NAMES],
            _HANDOFF_PATH,
            _HANDOFF_DEADLINE_SECONDS,
        )

    def _issue_identity(
        self, lease: RunnerLease, container: str, volumes: AttemptVolumes
    ) -> None:
        """Mint this invocation's identity and put it where only it can read it.

        The invocation the Runner published is what the leaf is bound to, so
        identity cannot be minted before the Runner exists, and the leaf cannot
        be reused by a second one. The material is handed to a receiver
        container over a pipe rather than bind-mounted from the host, and the
        private key is unlinked the moment that delivery is over -- taken or
        not.
        """
        manifest = lease.manifest
        invocation = _offered_invocation(
            self.carrier.read_file_in_container(
                container,
                _INVOCATION_OFFER,
                f"{manifest.effective_uid}:{manifest.effective_gid}",
                _MAXIMUM_OFFER_BYTES,
                _HANDOFF_DEADLINE_SECONDS,
            )
        )
        try:
            self.authority.issue_runner_identity(
                lease.binding,
                invocation,
                lease.issuance_directory,
                lease.core_peer_directory,
                timedelta(milliseconds=manifest.total_attempt_milliseconds),
            )
        except Exception as refused:
            # Minting builds a certificate out of values an Attempt's own
            # Runner and its lease writer chose. Whatever a library makes of
            # one it will not accept is this Attempt's failure and never the
            # launcher's end -- the same standard the offer above is read
            # under, and the reason it is a whole surface rather than the one
            # exception type that was found first.
            raise AttemptRefusal(
                f"runner-identity-not-issuable: {lease.lease_id.value}: {refused!r}"
            ) from refused
        receiver = ContainerSpec(
            f"{container}-identity-receiver",
            lease.runner_image,
            lease.label,
            ContainerHardening(
                user=f"{manifest.effective_uid}:{manifest.effective_gid}",
                read_only_root=True,
                drop_all_capabilities=True,
                no_new_privileges=True,
                process_limit=manifest.process_limit,
                memory_bytes=manifest.memory_bytes,
                cpu_period_microseconds=CANDIDATE_CPU_PERIOD,
                cpu_quota_microseconds=manifest.cpu_quota_microseconds,
            ),
            tmpfs=tuple(_writable_grants(manifest)),
            volumes=(
                VolumeMount(
                    volumes.identity,
                    _IDENTITY_RECEIVER_DESTINATION,
                    MountRight.READ_WRITE,
                ),
            ),
            entrypoint=_IDENTITY_RECEIVER_ENTRYPOINT,
            arguments=("--destination", str(_IDENTITY_RECEIVER_DESTINATION)),
        )
        try:
            self.carrier.run_receiving_stdin(
                receiver, receiver_record(lease.issuance_directory)
            )
        finally:
            # A key that was minted has to leave this host whether or not the
            # receiver took it: a failed delivery is a failed Attempt, never a
            # private key left lying in the issuance directory.
            unlink_private_keys([lease.issuance_directory / "client.key"])

    def _attest(self, lease: RunnerLease, container_id: str) -> None:
        """Read the created container back against the manifest Core bound.

        Core admits the session only when the identity written here matches the
        manifest it selected, so an Attempt whose container is not what the
        manifest says never gets a session at all.
        """
        try:
            attested = attest_runner_container(
                self.carrier.inspect_container(container_id), lease.manifest
            )
        except ValueError as mismatch:
            raise AttemptRefusal(
                f"launcher-attempt-failed: {lease.lease_id.value} container does not "
                f"attest the manifest Core bound: {mismatch}"
            ) from mismatch
        (lease.handoff_directory / _ATTESTATION_NAME).write_text(
            attested + "\n", encoding="ascii"
        )

    def _await_release(
        self,
        lease: RunnerLease,
        container: str,
        attachment: AttemptAttachment,
        volumes: AttemptVolumes,
    ) -> None:
        """Wait for the Runner to finish, restarting it once if it can resume.

        A Runner that died after journaling its terminal fact still holds the
        only record of what happened, and its journal and identity volumes
        outlive its container. Restarting exactly that container is how that
        record reaches Core; a nonzero exit with nothing retained is a failed
        Attempt and is refused. The restarted container gets a fresh network
        namespace, so this Attempt's policy is installed into it again before
        its handoff is replaced.
        """
        exit_code = self.carrier.wait_for_exit(container)
        self.announce(f"runner-exit={exit_code}")
        if exit_code == 0:
            return
        retained = self.carrier.file_exists_in_volume(
            volumes.journal, lease.runner_image, _JOURNAL_TERMINAL_RECORD
        )
        self.announce(f"journal-terminal-record={'present' if retained else 'absent'}")
        if not retained:
            raise AttemptRefusal(
                f"launcher-attempt-failed: {lease.lease_id.value} runner exited "
                f"{exit_code} with no retained terminal record"
            )
        self.carrier.restart_private_container(container)
        self.carrier.attach_policed_container(container, attachment)
        self._deliver_handoff(lease, container)
        self.announce(f"runner-resumed={container}")
        resumed = self.carrier.wait_for_exit(container)
        self.announce(f"runner-exit={resumed}")
        if resumed != 0:
            # The Runner journaled a terminal fact and its single resume still
            # could not deliver it -- Core is gone, and a console-only restart
            # leaves this launcher alive, so nothing else here ever reconciles
            # this Attempt. The retained fact reaches the handoff now, where
            # Serve converges it over on its own restart (`#585`), rather than
            # waiting for this launcher to itself restart. Both retain gates
            # already hold here: the container has exited (`wait_for_exit`
            # returned) and its terminal record is present (checked above).
            self._retain_terminal_record(
                volumes.journal, lease.handoff_directory, lease.runner_image
            )
            raise AttemptRefusal(
                f"launcher-attempt-failed: {lease.lease_id.value} resumed runner "
                f"exited {resumed}"
            )

    def _retain_terminal_record(
        self, journal_volume: str, handoff_directory: Path, runner_image: str
    ) -> None:
        """Copy the Runner's retained terminal record into the handoff, once.

        The bytes are the journal record verbatim, read back off the durable
        volume the exited Runner left behind (`read_file_in_volume`) and laid
        down under a fixed name Serve reads. The write is atomic and idempotent:
        a cross-restart second retain of the same Attempt finds the same bytes
        already there and rewrites nothing, so Serve never reads a half-written
        record and two retains never disagree.
        """
        record = self.carrier.read_file_in_volume(
            journal_volume,
            runner_image,
            _JOURNAL_TERMINAL_RECORD,
            MAXIMUM_RUNNER_TERMINAL_EVIDENCE_RECORD_BYTES,
        )
        target = handoff_directory / _RETAINED_TERMINAL_RECORD_NAME
        if target.is_file() and target.read_bytes() == record:
            self.announce(f"terminal-record-retained={target} already-present")
            return
        temporary = handoff_directory / f".{_RETAINED_TERMINAL_RECORD_NAME}.publish"
        temporary.write_bytes(record)
        os.replace(temporary, target)
        self.announce(f"terminal-record-retained={target}")

    def _attempt_containers_still_running(self, lease_id: RunnerLeaseId) -> bool:
        """Whether any container this Attempt labelled is still running.

        A reconcile never removes an Attempt whose Runner may still be alive:
        `docker rm -f`ing its container and deleting its journal volume would
        destroy the terminal fact the Runner is still producing. The removal --
        and the retain that must precede it -- waits until every labelled
        container has stopped, which is what makes the deferral real rather than
        a fact quietly lost.
        """
        label = lease_label(lease_id)
        return any(
            self.carrier.container_running(container)
            for container in self.carrier.labelled_containers(label)
        )

    def _retain_terminal_record_before_delete(self, lease_id: RunnerLeaseId) -> None:
        """Retain an abandoned Runner's terminal fact before its volume is gone.

        Reconcile removes what a dead launcher left behind, and the journal
        volume is the only place that Attempt's terminal fact still lives -- so
        it is copied into the handoff here, strictly before `_remove_attempt`
        deletes the volume. The caller has already established that every
        container this Attempt labelled has stopped, so this never reads a Runner
        that is still writing. A missing record is the ordinary case -- a cleanly
        released Attempt unlinked its own -- and retains nothing.
        """
        label = lease_label(lease_id)
        journal = next(
            (
                volume
                for volume in self.carrier.labelled_volumes(label)
                if volume.endswith(_JOURNAL_VOLUME_SUFFIX)
            ),
            None,
        )
        if journal is None or not self.carrier.file_exists_in_volume(
            journal, self.validation.runner_image, _JOURNAL_TERMINAL_RECORD
        ):
            return
        handoff = (
            self.validation.attempt_root / lease_id.value / _HANDOFF_DIRECTORY_NAME
        )
        if not handoff.is_dir():
            self.announce(f"terminal-record-retain-no-handoff={lease_id.value}")
            return
        self._retain_terminal_record(journal, handoff, self.validation.runner_image)

    def _remove_attempt(self, lease_id: RunnerLeaseId) -> None:
        """Everything one Attempt left on this host, including in the console.

        The console outlives its Attempts, so its own namespace is the one
        place a removed Attempt could stay behind in: its chains go first,
        while the network they name still exists, and the objects this launcher
        created follow.
        """
        self.carrier.remove_attempt_policy(
            self.validation.console_container, attempt_chains(lease_id.value)
        )
        label = lease_label(lease_id)
        self.carrier.remove_containers(self.carrier.labelled_containers(label))
        self.carrier.remove_volumes(self.carrier.labelled_volumes(label))
        self.carrier.remove_networks(self.carrier.labelled_networks(label))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Establish Runner leases as Attempts on this host."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    issue = commands.add_parser(
        "issue-console-identity",
        help=(
            "mint or renew the identity the console serves Attempt sessions "
            "under, from this installation's own authority"
        ),
    )
    issue.add_argument("--certificate-authority-state", type=Path, required=True)
    issue.add_argument(
        "--identity",
        type=Path,
        required=True,
        help="the directory the console reads its identity from, read-only",
    )

    serve = commands.add_parser(
        "serve", help="claim open Runner leases and establish them as Attempts"
    )
    serve.add_argument("--lease-directory", type=Path, required=True)
    serve.add_argument("--certificate-authority-state", type=Path, required=True)
    serve.add_argument("--network-policy-image", required=True)
    serve.add_argument(
        "--attempt-root",
        type=Path,
        required=True,
        help=(
            "the one directory tree a lease may name; it holds per-Attempt "
            "material only, and never the certificate authority state"
        ),
    )
    serve.add_argument(
        "--console-container",
        required=True,
        help="the container a lease may have attached to an Attempt network",
    )
    serve.add_argument(
        "--console-network",
        required=True,
        help=(
            "the network the console keeps besides its Attempts; it serves the "
            "cockpit there, and its Attempt policy admits nothing else"
        ),
    )
    serve.add_argument(
        "--console-identity",
        type=Path,
        required=True,
        help="the console identity directory this refuses to serve past expiry",
    )
    serve.add_argument(
        "--runner-image",
        required=True,
        help="the one image a lease may have started as this host's Runner",
    )
    _add_bound_arguments(serve)
    serve.add_argument("--poll-seconds", type=float, default=_LEASE_POLL_SECONDS)
    serve.add_argument(
        "--once",
        action="store_true",
        help="establish at most one lease and stop, instead of watching",
    )
    return parser


def _add_bound_arguments(command: argparse.ArgumentParser) -> None:
    """The largest Attempt this host declares it carries.

    Serve chooses these numbers for its own Attempts; the operator decides how
    much of this host any Attempt may ask for. The defaults are the candidate
    Attempt's own numbers.
    """
    declared = RunnerManifestBounds()
    for flag, default in (
        ("--maximum-memory-bytes", declared.memory_bytes),
        ("--maximum-process-limit", declared.process_limit),
        ("--maximum-cpu-quota-microseconds", declared.cpu_quota_microseconds),
        ("--maximum-scratch-bytes", declared.scratch_bytes),
        ("--maximum-writable-surface-bytes", declared.writable_surface_bytes),
        ("--maximum-journal-bytes", declared.journal_bytes),
    ):
        command.add_argument(flag, type=int, default=default)


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    authority = RunnerIdentityAuthority(parsed.certificate_authority_state)
    if parsed.command == "issue-console-identity":
        authority.issue_core_identity(parsed.identity)
        _announce(f"console-identity={parsed.identity}")
        return 0
    expiry = admitted_console_identity(parsed.console_identity, datetime.now(UTC))
    # The operator's renewal reminder is the startup log: this leaf is what
    # every Attempt's Core presents, and nothing else on this host says when it
    # stops standing.
    _announce(f"console-identity-expires={expiry.isoformat()}")
    validation = RunnerLeaseValidation(
        admitted_attempt_root(parsed.attempt_root, parsed.certificate_authority_state),
        parsed.console_container,
        parsed.console_network,
        parsed.runner_image,
        RunnerManifestBounds(
            parsed.maximum_memory_bytes,
            parsed.maximum_process_limit,
            parsed.maximum_cpu_quota_microseconds,
            parsed.maximum_scratch_bytes,
            parsed.maximum_writable_surface_bytes,
            parsed.maximum_journal_bytes,
        ),
    )
    with single_launcher(parsed.lease_directory):
        launcher = RunnerLauncher(
            DockerCarrier(parsed.network_policy_image),
            authority,
            FileRunnerLeaseSource(parsed.lease_directory, validation),
            validation,
            _announce,
        )
        # A bounded run answers for the Attempts it failed; a watching one has
        # already reported each of them and keeps going.
        return 1 if launcher.serve_open_leases(parsed.once, parsed.poll_seconds) else 0


def _announce(line: str) -> None:
    print(line, flush=True)


if __name__ == "__main__":
    try:
        status = main()
    except AttemptRefusal as refusal:
        # A refusal at start is an answer to the operator, not a stack trace:
        # what was declared cannot be launched under, and this says which part.
        print(refusal, file=sys.stderr)
        status = 1
    raise SystemExit(status)
