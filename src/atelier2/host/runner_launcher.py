"""The host launcher: the one process that turns a Runner lease into an Attempt.

It runs beside the console, not inside it. Serve holds no carrier authority at
all -- no engine socket, no broker, no privileged helper (ADR 0009 sec. 2,
`#540` operator ruling B). Instead this process claims open Runner leases,
and for each one establishes the whole per-Attempt ensemble: the Attempt
network with its policy, the volumes, the Runner container, the identity that
one invocation may present, the inspect attestation Core admits the session on,
and the complete removal of all of it afterwards.

The lease source is the portability seam. Locally it is a directory of lease
documents a Serve process writes; a cluster plays the same role over the same
interface. Deploying elsewhere replaces that adapter, never this arrangement.

What this process is trusted with is deliberately narrow: it never reads the
product's own state, never runs provider code, and never hands an Attempt
anything but the material one invocation needs.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from atelier2.adapters.docker_carrier import (
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
    attest_runner_container,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.runner_manifests import (
    CANDIDATE_CPU_PERIOD,
    CANDIDATE_WORKSPACE_BYTES,
    RunnerManifestV1,
    RunnerPathRight,
    decode_runner_manifest,
)
from atelier2.host.runner_identity import (
    RunnerIdentityAuthority,
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
_HANDOFF_BYTES = 1_048_576
_TMPFS_MODE = 0o1777
_LEASE_LABEL = "atelier2.runner-lease"
_HANDOFF_DEADLINE_SECONDS = 30.0
_LEASE_POLL_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class RunnerLease:
    """One Attempt a launcher may establish, as its source hands it over.

    Everything an Attempt needs and nothing it does not: the generation Core
    already bound, the image and manifest that generation names, and the host
    directories this Attempt's material passes through.
    """

    lease_id: str
    binding: RunnerGenerationBinding
    runner_image: str
    manifest: RunnerManifestV1
    serve_container: str
    handoff_directory: Path
    core_peer_directory: Path
    issuance_directory: Path
    provider_credential_source: Path

    @property
    def label(self) -> str:
        """What every carrier object of this Attempt carries, so a launcher
        that died can still be told which objects were its own."""
        return lease_label(self.lease_id)

    @property
    def attempt_name(self) -> str:
        """The one name every object of this Attempt is derived from, so an
        operator reading the host sees which lease each of them belongs to."""
        return f"atelier2-attempt-{self.lease_id}"


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
        self, container: str, network: AttemptNetwork, role: ContainerRole
    ) -> None: ...

    def install_attempt_policy(
        self, container: str, subnet: str, role: ContainerRole
    ) -> None: ...

    def create_volume(
        self, name: str, label: str, tmpfs: TmpfsVolumeOptions | None = None
    ) -> None: ...

    def own_volume(self, name: str, image: str, uid: int, gid: int) -> None: ...

    def start_policed_container(
        self, spec: ContainerSpec, network: AttemptNetwork, role: ContainerRole
    ) -> str: ...

    def start_container(self, container: str) -> None: ...

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

    def labelled_containers(self, label: str) -> tuple[str, ...]: ...

    def labelled_volumes(self, label: str) -> tuple[str, ...]: ...

    def labelled_networks(self, label: str) -> tuple[str, ...]: ...

    def remove_containers(self, containers: Iterable[str]) -> None: ...

    def remove_volumes(self, volumes: Iterable[str]) -> None: ...

    def remove_networks(self, networks: Iterable[str]) -> None: ...


def lease_label(lease_id: str) -> str:
    """The label an Attempt's objects carry, derived from the lease alone."""
    return f"{_LEASE_LABEL}={lease_id}"


def decode_runner_binding(document: bytes) -> RunnerGenerationBinding:
    """The generation Core bound for one Attempt, as it publishes it."""
    record = json.loads(document)
    return RunnerGenerationBinding(
        AgentAttemptId(record["attempt_id"]),
        AgentExecutionRequestHash(record["request_hash"]),
        RunnerGenerationId(record["generation_id"]),
        RunnerManifestId(record["manifest_id"]),
    )


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

    def __init__(self, directory: Path) -> None:
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
        document = self._claimed / f"{lease.lease_id}.json"
        document.rename(self._released / document.name)

    def abandon_stale_claims(self) -> tuple[str, ...]:
        stale = sorted(document.stem for document in self._claimed.glob("*.json"))
        for lease_id in stale:
            document = self._claimed / f"{lease_id}.json"
            document.rename(self._released / document.name)
        return tuple(stale)

    def _decode(self, document: Path) -> RunnerLease:
        record = json.loads(document.read_text(encoding="utf-8"))
        manifest_path = Path(record["manifest_path"])
        return RunnerLease(
            document.stem,
            decode_runner_binding(Path(record["binding_path"]).read_bytes()),
            record["runner_image"],
            decode_runner_manifest(manifest_path.read_bytes()),
            record["serve_container"],
            Path(record["handoff_directory"]),
            Path(record["core_peer_directory"]),
            Path(record["issuance_directory"]),
            Path(record["provider_credential_source"]),
        )


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
    announce: Callable[[str], None]

    def reconcile_abandoned_attempts(self) -> None:
        """Remove what an earlier launcher left behind before claiming anything.

        A launcher that died mid-Attempt left containers, volumes and a network
        labelled with that lease. Nothing else may be touched: only objects
        carrying the exact lease label of a lease this source says is stale.
        """
        for lease_id in self.leases.abandon_stale_claims():
            self.announce(f"reconciled-attempt={lease_id}")
            self._remove_attempt(lease_label(lease_id))

    def serve_open_leases(self, once: bool, poll_seconds: float) -> None:
        self.reconcile_abandoned_attempts()
        while True:
            lease = self.leases.claim_open_lease()
            if lease is None:
                if once:
                    return
                time.sleep(poll_seconds)
                continue
            self.establish(lease)
            if once:
                return

    def establish(self, lease: RunnerLease) -> None:
        """Run one Attempt from an empty host to a released one.

        On success every object this created is removed and the lease goes
        back to its source. On failure they are deliberately left, named, for
        the operator to read -- the next launcher start reconciles them.
        """
        self.announce(f"attempt-lease={lease.lease_id}")
        network = self.carrier.create_attempt_network(lease.attempt_name, lease.label)
        self.announce(f"attempt-network={network.name}")
        self.carrier.attach_policed_container(
            lease.serve_container, network, ContainerRole.CORE
        )
        volumes = self._create_volumes(lease)
        container = f"{lease.attempt_name}-runner"
        specification = runner_container_spec(lease, container, volumes)
        container_id = self.carrier.start_policed_container(
            specification, network, ContainerRole.RUNNER
        )
        self._deliver_handoff(lease, container)
        self._issue_identity(lease, container, volumes)
        self._attest(lease, container_id)
        self._await_release(lease, container, network, volumes)
        self._remove_attempt(lease.label)
        self.leases.release(lease)
        self.announce(f"attempt-released={lease.lease_id}")

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
            f"{lease.attempt_name}-journal",
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
        private key is unlinked as soon as the receiver has it.
        """
        manifest = lease.manifest
        offer = json.loads(
            self.carrier.read_file_in_container(
                container,
                _INVOCATION_OFFER,
                f"{manifest.effective_uid}:{manifest.effective_gid}",
                _MAXIMUM_OFFER_BYTES,
                _HANDOFF_DEADLINE_SECONDS,
            )
        )
        self.authority.issue_runner_identity(
            lease.binding,
            RunnerInvocationId(offer["invocation_id"]),
            lease.issuance_directory,
            lease.core_peer_directory,
        )
        self.carrier.run_receiving_stdin(
            ContainerSpec(
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
            ),
            receiver_record(lease.issuance_directory),
        )
        unlink_private_keys([lease.issuance_directory / "client.key"])

    def _attest(self, lease: RunnerLease, container_id: str) -> None:
        """Read the created container back against the manifest Core bound.

        Core admits the session only when the identity written here matches the
        manifest it selected, so an Attempt whose container is not what the
        manifest says never gets a session at all.
        """
        attested = attest_runner_container(
            self.carrier.inspect_container(container_id), lease.manifest
        )
        (lease.handoff_directory / _ATTESTATION_NAME).write_text(
            attested + "\n", encoding="ascii"
        )

    def _await_release(
        self,
        lease: RunnerLease,
        container: str,
        network: AttemptNetwork,
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
            raise CarrierRefusal(
                f"launcher-attempt-failed: {lease.lease_id} runner exited "
                f"{exit_code} with no retained terminal record"
            )
        self.carrier.start_container(container)
        self.carrier.install_attempt_policy(
            container, network.subnet, ContainerRole.RUNNER
        )
        self._deliver_handoff(lease, container)
        self.announce(f"runner-resumed={container}")
        resumed = self.carrier.wait_for_exit(container)
        self.announce(f"runner-exit={resumed}")
        if resumed != 0:
            raise CarrierRefusal(
                f"launcher-attempt-failed: {lease.lease_id} resumed runner "
                f"exited {resumed}"
            )

    def _remove_attempt(self, label: str) -> None:
        self.carrier.remove_containers(self.carrier.labelled_containers(label))
        self.carrier.remove_volumes(self.carrier.labelled_volumes(label))
        self.carrier.remove_networks(self.carrier.labelled_networks(label))


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Establish Runner leases as Attempts on this host."
    )
    parser.add_argument("--lease-directory", type=Path, required=True)
    parser.add_argument("--certificate-authority-state", type=Path, required=True)
    parser.add_argument("--network-policy-image", required=True)
    parser.add_argument("--poll-seconds", type=float, default=_LEASE_POLL_SECONDS)
    parser.add_argument(
        "--once",
        action="store_true",
        help="establish at most one lease and stop, instead of watching",
    )
    parsed = parser.parse_args(arguments)
    launcher = RunnerLauncher(
        DockerCarrier(parsed.network_policy_image),
        RunnerIdentityAuthority(parsed.certificate_authority_state),
        FileRunnerLeaseSource(parsed.lease_directory),
        _announce,
    )
    launcher.serve_open_leases(parsed.once, parsed.poll_seconds)
    return 0


def _announce(line: str) -> None:
    print(line, flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
