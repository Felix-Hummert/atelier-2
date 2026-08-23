"""Production owner of every Docker operation an Attempt's carrier performs.

The host launcher is the only role that holds Docker authority (ADR 0009 sec. 2,
`#540` operator ruling B): Serve never gets a socket, a broker, or a carrier
call. This module is where that authority is spent, once, behind typed
operations -- Attempt network with its policy installed before a container can
send a packet, the volumes an Attempt's Runner needs, container start, restart,
wait, removal, file transport in and out, and the inspect attestation that reads
the created container back against the manifest Core selected.

Every engine call is an argument vector; no host shell ever parses a value this
module builds, so a container name, a path, or a label can never become another
command. The engine binary is addressed by absolute path, so an ambient `PATH`
cannot decide who answers a carrier call.

The one program text this module does render is the Attempt's packet-filtering
ruleset, which runs inside a throwaway policy container's own `sh`. It is built
from a typed policy record rather than assembled from caller strings, and the
only values that reach it are this Attempt's own subnet and its served port.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from atelier2.adapters.runner_tls import CORE_DNS_NAME, CORE_SESSION_PORT
from atelier2.contracts.runner_manifests import (
    CANDIDATE_CPU_PERIOD,
    CANDIDATE_WORKSPACE_BYTES,
    RunnerManifestV1,
    RunnerPathRight,
    decode_runner_manifest,
    runner_manifest_id,
)

DOCKER_EXECUTABLE = Path("/usr/bin/docker")
# The private network mode a container is created in, so its Attempt policy can
# be installed before it is ever connected to anything.
_PRIVATE_NETWORK = "none"
_POLL_INTERVAL_SECONDS = 0.1
_RUNNER_OWNED_VOLUME_MODE = 0o700


class CarrierRefusal(Exception):
    """A named refusal of one carrier operation, carrying what the engine said."""


class MountRight(StrEnum):
    READ_ONLY = "ro"
    READ_WRITE = "rw"


class ContainerRole(StrEnum):
    """What an Attempt's network policy grants a container.

    Core serves this Attempt's session port to this Attempt's subnet and reaches
    nothing outbound beyond it. The Runner reaches outbound DNS and HTTPS and
    accepts nothing inbound at all. Nothing else, in either direction, is
    allowed (ADR 0009 sec. 2, 2026-08-23 amendment).
    """

    CORE = "core"
    RUNNER = "runner"


@dataclass(frozen=True, slots=True)
class TmpfsMount:
    destination: PurePosixPath
    size_bytes: int
    mode: int | None = None

    def options(self) -> str:
        flags = ["rw", "noexec", "nosuid", f"size={self.size_bytes}"]
        if self.mode is not None:
            flags.append(f"mode={self.mode:o}")
        return ",".join(flags)


@dataclass(frozen=True, slots=True)
class BindMount:
    source: Path
    destination: PurePosixPath
    right: MountRight


@dataclass(frozen=True, slots=True)
class VolumeMount:
    """A named volume, always mounted `volume-nocopy`: the image's own content
    at that path must never seed an Attempt's durable state."""

    volume: str
    destination: PurePosixPath
    right: MountRight


@dataclass(frozen=True, slots=True)
class ContainerHardening:
    """The sandbox a container runs under, as the launcher asks for it.

    Every field is read back out of the created container by the inspect
    attestation for the Runner, so this record and that fence speak about the
    same container rather than about each other.
    """

    user: str | None = None
    read_only_root: bool = False
    drop_all_capabilities: bool = False
    no_new_privileges: bool = False
    process_limit: int | None = None
    memory_bytes: int | None = None
    cpu_period_microseconds: int | None = None
    cpu_quota_microseconds: int | None = None


@dataclass(frozen=True, slots=True)
class ContainerSpec:
    name: str
    image: str
    label: str
    hardening: ContainerHardening
    tmpfs: tuple[TmpfsMount, ...] = ()
    binds: tuple[BindMount, ...] = ()
    volumes: tuple[VolumeMount, ...] = ()
    entrypoint: str | None = None
    arguments: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AttemptNetwork:
    name: str
    subnet: str


@dataclass(frozen=True, slots=True)
class TmpfsVolumeOptions:
    """A volume the engine backs with tmpfs, for content that must not reach
    disk and is reproducible from what the host already holds."""

    size_bytes: int
    uid: int
    gid: int
    mode: int


def _policy_program(subnet: str, role: ContainerRole) -> str:
    """This Attempt's packet-filtering ruleset, for one container's namespace.

    Everything not named here is REJECTed in both directions -- a loud,
    immediate connection failure the provider CLI's own error handling
    surfaces, never a silent DROP timeout. The same reject chain is installed
    for IPv6, so enabling IPv6 on a future Attempt network cannot silently open
    a second, unfiltered path.
    """
    rules = [
        "iptables -A OUTPUT -o lo -j ACCEPT",
        f"iptables -A OUTPUT -d {subnet} -j ACCEPT",
    ]
    if role is ContainerRole.RUNNER:
        rules += [
            "iptables -A OUTPUT -p udp --dport 53 -j ACCEPT",
            "iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT",
            "iptables -A OUTPUT -p tcp --dport 443 -j ACCEPT",
        ]
    rules += [
        "iptables -A OUTPUT -p tcp -j REJECT --reject-with tcp-reset",
        "iptables -A OUTPUT -j REJECT --reject-with icmp-port-unreachable",
        "iptables -A INPUT -i lo -j ACCEPT",
        "iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
    ]
    if role is ContainerRole.CORE:
        # Core is the only container in an Attempt that serves anything, and it
        # serves exactly one port to exactly its own Attempt subnet. The Runner
        # gets no inbound rule at all: it dials out, and its answers come back
        # through the conntrack rule above.
        rules.append(
            f"iptables -A INPUT -s {subnet} -p tcp --dport {CORE_SESSION_PORT} -j ACCEPT"
        )
    rules += [
        "iptables -A INPUT -p tcp -j REJECT --reject-with tcp-reset",
        "iptables -A INPUT -j REJECT --reject-with icmp-port-unreachable",
        "ip6tables -A OUTPUT -o lo -j ACCEPT",
        "ip6tables -A OUTPUT -j REJECT",
        "ip6tables -A INPUT -i lo -j ACCEPT",
        "ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
        "ip6tables -A INPUT -j REJECT",
    ]
    return "\n".join(("set -e", *rules))


def _mount_arguments(spec: ContainerSpec) -> list[str]:
    arguments: list[str] = []
    for tmpfs in spec.tmpfs:
        arguments += ["--tmpfs", f"{tmpfs.destination}:{tmpfs.options()}"]
    for bind in spec.binds:
        arguments += [
            "--mount",
            f"type=bind,src={bind.source},dst={bind.destination}"
            + (",readonly" if bind.right is MountRight.READ_ONLY else ""),
        ]
    for volume in spec.volumes:
        arguments += [
            "--mount",
            f"type=volume,src={volume.volume},dst={volume.destination},volume-nocopy"
            + (",readonly" if volume.right is MountRight.READ_ONLY else ""),
        ]
    return arguments


def _hardening_arguments(hardening: ContainerHardening) -> list[str]:
    arguments: list[str] = []
    if hardening.user is not None:
        arguments += ["--user", hardening.user]
    if hardening.read_only_root:
        arguments.append("--read-only")
    if hardening.drop_all_capabilities:
        arguments += ["--cap-drop", "ALL"]
    if hardening.no_new_privileges:
        arguments += ["--security-opt", "no-new-privileges:true"]
    if hardening.process_limit is not None:
        arguments += ["--pids-limit", str(hardening.process_limit)]
    if hardening.memory_bytes is not None:
        arguments += ["--memory", str(hardening.memory_bytes)]
    if hardening.cpu_period_microseconds is not None:
        arguments += ["--cpu-period", str(hardening.cpu_period_microseconds)]
    if hardening.cpu_quota_microseconds is not None:
        arguments += ["--cpu-quota", str(hardening.cpu_quota_microseconds)]
    return arguments


def _create_arguments(spec: ContainerSpec) -> list[str]:
    arguments = ["--name", spec.name, "--label", spec.label]
    arguments += _hardening_arguments(spec.hardening)
    arguments += _mount_arguments(spec)
    if spec.entrypoint is not None:
        arguments += ["--entrypoint", spec.entrypoint]
    return [*arguments, spec.image, *spec.arguments]


@dataclass(frozen=True, slots=True)
class DockerCarrier:
    """Every Docker operation the launcher performs, and no other authority.

    `network_policy_image` is the throwaway image that installs an Attempt's
    packet filter from inside a container's own network namespace and exits; it
    is deliberately not the Runner image, so no packet-filtering tool ever sits
    beside the provider CLI.
    """

    network_policy_image: str
    executable: Path = DOCKER_EXECUTABLE

    def _run(self, arguments: Sequence[str], stdin: bytes | None = None) -> str:
        completed = subprocess.run(
            [str(self.executable), *arguments],
            capture_output=True,
            input=stdin,
            check=False,
        )
        if completed.returncode != 0:
            raise CarrierRefusal(
                f"carrier-command-refused: {' '.join(arguments)} exited "
                f"{completed.returncode}: "
                f"{completed.stderr.decode('utf-8', 'replace').strip()}"
            )
        return completed.stdout.decode("utf-8")

    def _succeeded(self, arguments: Sequence[str]) -> bool:
        return (
            subprocess.run(
                [str(self.executable), *arguments], capture_output=True, check=False
            ).returncode
            == 0
        )

    def create_attempt_network(self, name: str, label: str) -> AttemptNetwork:
        """One routed bridge network for one Attempt, and the subnet its policy
        is written against."""
        self._run(["network", "create", "--label", label, name])
        document = self._inspect("network", name)
        configurations = document["IPAM"]["Config"]
        if not configurations:
            raise CarrierRefusal(f"carrier-network-has-no-subnet: {name}")
        return AttemptNetwork(name, str(configurations[0]["Subnet"]))

    def create_volume(
        self, name: str, label: str, tmpfs: TmpfsVolumeOptions | None = None
    ) -> None:
        arguments = ["volume", "create", "--driver", "local"]
        if tmpfs is not None:
            arguments += [
                "--opt",
                "type=tmpfs",
                "--opt",
                "device=tmpfs",
                "--opt",
                (
                    f"o=uid={tmpfs.uid},gid={tmpfs.gid},mode={tmpfs.mode:o},"
                    f"size={tmpfs.size_bytes}"
                ),
            ]
        self._run([*arguments, "--label", label, name])

    def own_volume(self, name: str, image: str, uid: int, gid: int) -> None:
        """Hand a durable volume to the Runner's own account before it is ever
        mounted into the Runner.

        The `local` driver has no ownership option for a disk-backed volume, so
        ownership is set from a throwaway root container and then read back:
        a volume the Runner cannot write is a session that fails deep inside
        the Runner instead of here.
        """
        mount = f"type=volume,src={name},dst=/target,volume-nocopy"
        for entrypoint, arguments in (
            ("chown", (f"{uid}:{gid}", "/target")),
            ("chmod", (f"{_RUNNER_OWNED_VOLUME_MODE:o}", "/target")),
        ):
            self._run(
                [
                    "run",
                    "--rm",
                    "--user",
                    "root",
                    "--mount",
                    mount,
                    "--entrypoint",
                    entrypoint,
                    image,
                    *arguments,
                ]
            )
        observed = self._run(
            [
                "run",
                "--rm",
                "--user",
                "root",
                "--mount",
                mount,
                "--entrypoint",
                "stat",
                image,
                "-c",
                "%u:%g:%a",
                "/target",
            ]
        ).strip()
        expected = f"{uid}:{gid}:{_RUNNER_OWNED_VOLUME_MODE:o}"
        if observed != expected:
            raise CarrierRefusal(
                f"carrier-volume-ownership-differs: {name} is {observed}, "
                f"expected {expected}"
            )

    def install_attempt_policy(
        self, container: str, subnet: str, role: ContainerRole
    ) -> None:
        """Install this Attempt's policy inside one container's own network
        namespace, from a throwaway container that exits immediately.

        The target container holds no packet-filtering tool and no
        `CAP_NET_ADMIN`, so it cannot alter what this leaves behind.
        """
        self._run(
            [
                "run",
                "--rm",
                "--network",
                f"container:{container}",
                "--user",
                "0",
                "--cap-drop",
                "ALL",
                "--cap-add",
                "NET_ADMIN",
                "--entrypoint",
                "sh",
                self.network_policy_image,
                "-c",
                _policy_program(subnet, role),
            ]
        )

    def start_policed_container(
        self, spec: ContainerSpec, network: AttemptNetwork, role: ContainerRole
    ) -> str:
        """Start one container that never runs for a single unfiltered packet.

        It is created detached from every network, its Attempt policy is
        installed while it can reach nothing at all, and only then is it
        connected. A policy that fails to install therefore leaves a container
        unable to reach anything rather than running wide open.
        """
        self._run(
            ["run", "-d", "--network", _PRIVATE_NETWORK, *_create_arguments(spec)]
        )
        self.install_attempt_policy(spec.name, network.subnet, role)
        # The engine refuses to attach a container that is still in private
        # mode, so the empty namespace is released only once the policy is in it.
        self._run(["network", "disconnect", _PRIVATE_NETWORK, spec.name])
        self.connect_policed_container(spec.name, network, role)
        return self._attested_container_id(spec)

    def connect_policed_container(
        self, container: str, network: AttemptNetwork, role: ContainerRole
    ) -> None:
        alias = ["--alias", CORE_DNS_NAME] if role is ContainerRole.CORE else []
        self._run(["network", "connect", *alias, network.name, container])

    def _attested_container_id(self, spec: ContainerSpec) -> str:
        """The created container's id, once its mounts read back as asked for.

        A mount right is the one part of a spec the engine could honour
        differently than the launcher meant -- a bind the launcher asked for
        read-only and got writable is a host surface nobody decided to open --
        so the carrier proves its own work rather than assuming it.
        """
        document = self.inspect_container(spec.name)
        for bind in spec.binds:
            observed = _mount(document, bind.destination.as_posix())
            if observed is None or observed.get("RW") is not (
                bind.right is MountRight.READ_WRITE
            ):
                raise CarrierRefusal(
                    f"carrier-mount-right-differs: {spec.name} {bind.destination}"
                )
        return str(document["Id"])

    def start_container(self, container: str) -> None:
        self._run(["start", container])

    def wait_for_exit(self, container: str) -> int:
        return int(self._run(["wait", container]).strip())

    def inspect_container(self, container: str) -> dict[str, Any]:
        return self._inspect("container", container)

    def _inspect(self, kind: str, name: str) -> dict[str, Any]:
        document = json.loads(self._run([kind, "inspect", name]))
        if not isinstance(document, list) or len(document) != 1:
            raise CarrierRefusal(f"carrier-inspect-is-not-one-object: {kind} {name}")
        return document[0]

    def capture_logs(self, container: str, destination: Path) -> bool:
        """Write one container's combined output for failure analysis.

        Diagnostics of a container that may already be gone: a missing
        container leaves the file untouched and says so, rather than failing a
        teardown that is itself running because something else failed.
        """
        completed = subprocess.run(
            [str(self.executable), "logs", container], capture_output=True, check=False
        )
        if completed.returncode != 0:
            return False
        destination.write_bytes(completed.stdout + completed.stderr)
        return True

    def copy_into_container(
        self,
        container: str,
        sources: Sequence[Path],
        destination: PurePosixPath,
        deadline_seconds: float,
    ) -> None:
        """Copy every source into the container, or none of them.

        The destination may not exist yet -- a container publishes its writable
        surface when it starts, not when the engine reports it running -- so
        this retries the whole set until the bound expires.
        """
        self._copy_all(
            container,
            [
                (str(source), f"{container}:{destination / source.name}")
                for source in sources
            ],
            deadline_seconds,
            partial=(),
        )

    def copy_from_container(
        self,
        container: str,
        sources: Sequence[PurePosixPath],
        destination: Path,
        deadline_seconds: float,
    ) -> None:
        """Copy every source out of the container, or leave none behind.

        A half-copied set is removed between attempts, so a caller never reads
        one file of a set the container had not finished publishing.
        """
        landed = [destination / source.name for source in sources]
        self._copy_all(
            container,
            [
                (f"{container}:{source}", str(destination / source.name))
                for source in sources
            ],
            deadline_seconds,
            partial=tuple(landed),
        )
        for path in landed:
            if path.stat().st_size == 0:
                raise CarrierRefusal(f"carrier-copied-an-empty-file: {path.name}")

    def _copy_all(
        self,
        container: str,
        transfers: Sequence[tuple[str, str]],
        deadline_seconds: float,
        partial: Sequence[Path],
    ) -> None:
        deadline = time.monotonic() + deadline_seconds
        while True:
            if all(
                self._succeeded(["cp", source, destination])
                for source, destination in transfers
            ):
                return
            for path in partial:
                path.unlink(missing_ok=True)
            if time.monotonic() >= deadline:
                raise CarrierRefusal(
                    f"carrier-copy-did-not-complete: {container} "
                    f"{[destination for _source, destination in transfers]}"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)

    def read_file_in_container(
        self,
        container: str,
        path: PurePosixPath,
        user: str,
        maximum_bytes: int,
        deadline_seconds: float,
    ) -> bytes:
        """Read one bounded file out of a running container, as an unprivileged
        account.

        The container is addressed by the id its own name resolved to once, so
        a replaced name cannot answer a later read. The file is written by the
        container under this Attempt's own sized tmpfs, and is refused here if
        it exceeds the bound the caller states anyway.
        """
        container_id = str(self.inspect_container(container)["Id"])
        deadline = time.monotonic() + deadline_seconds
        while True:
            completed = subprocess.run(
                [
                    str(self.executable),
                    "exec",
                    "--user",
                    user,
                    "--",
                    container_id,
                    "/usr/bin/cat",
                    "--",
                    str(path),
                ],
                capture_output=True,
                check=False,
            )
            if completed.returncode == 0 and not completed.stderr and completed.stdout:
                if len(completed.stdout) > maximum_bytes:
                    raise CarrierRefusal(
                        f"carrier-file-exceeds-bound: {path} is "
                        f"{len(completed.stdout)} bytes, bound {maximum_bytes}"
                    )
                return completed.stdout
            if time.monotonic() >= deadline:
                raise CarrierRefusal(
                    f"carrier-file-unreadable: {container} {path}: "
                    f"{completed.stderr.decode('utf-8', 'replace').strip()}"
                )
            time.sleep(_POLL_INTERVAL_SECONDS)

    def remove_containers(self, containers: Iterable[str]) -> None:
        for container in containers:
            self._succeeded(["rm", "-f", container])

    def remove_volumes(self, volumes: Iterable[str]) -> None:
        for volume in volumes:
            self._succeeded(["volume", "rm", volume])

    def remove_networks(self, networks: Iterable[str]) -> None:
        for network in networks:
            self._succeeded(["network", "rm", network])


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


def _attest_writable_surface_is_noexec_tmpfs(
    tmpfs: dict[str, str], manifest: RunnerManifestV1
) -> None:
    """Every writable path the manifest attests must be a sized `noexec` tmpfs.

    The manifest's Landlock grant already denies the child `EXECUTE` beneath a
    writable path; this is the carrier's independent second fence, read back
    out of the container Docker actually created. Executable code therefore
    stays in the read-only image root even if one of the two ever slipped:
    code written into the writable surface cannot be run from it, and nothing
    there gains privilege through a set-user-ID bit. The size is attested too,
    exactly as `/workspace`'s already is, so the one surface a provider child
    may fill has an attested bound rather than whatever the launcher typed.
    """
    for grant in manifest.child_path_grants:
        if grant.right is not RunnerPathRight.READ_WRITE:
            continue
        options = tmpfs.get(grant.path.as_posix())
        if options is None:
            raise ValueError("runner-attestation-mismatch")
        flags = set(options.split(","))
        if not {"noexec", "nosuid"} <= flags or "rw" not in flags:
            raise ValueError("runner-attestation-mismatch")
        if _tmpfs_size(options) != manifest.scratch_bytes:
            raise ValueError("runner-attestation-mismatch")


def _attest_credential_directory_is_the_only_read_only_bind(
    document: dict[str, Any], manifest: RunnerManifestV1
) -> None:
    """The credential directory is bind-mounted read-only, and nothing else is.

    ADR 0009 sec. 2's 2026-08-22 amendment admits exactly one host surface
    beyond the per-invocation identity material: the provider's own credential
    directory, read-only. Read-write access to that original directory stays
    forbidden, because a live operator session may hold it open. This fence
    reads both halves back out of the created container -- the credential
    directory is bound and not writable, and no other host path was bound in
    at all -- so a launcher cannot quietly add a second host surface, and the
    write-capable per-Attempt copy reserved for its own operator ruling cannot
    appear here first.
    """
    admitted_directory = manifest.provider_credential_directory.as_posix()
    mount = _mount(document, admitted_directory)
    if mount is None or mount.get("RW") is not False or mount.get("Type") != "bind":
        raise ValueError("runner-attestation-mismatch")
    for other in document.get("Mounts") or ():
        if (
            other.get("Type") == "bind"
            and other.get("Destination") != admitted_directory
        ):
            raise ValueError("runner-attestation-mismatch")


def _attest_the_attempt_is_the_only_way_in(document: dict[str, Any]) -> None:
    """No published port, and no share of the host's own network namespace.

    An Attempt's single inbound opening is Core's session port inside the
    Attempt's own subnet. A published port would put the Runner on an address
    the host's own neighbours can reach, and `--network host` would hand it the
    host's namespace outright -- in which the Attempt policy this carrier
    installs would be filtering the host itself rather than one Attempt. Both
    are refused here, read back out of the container the engine created.

    Docker reports "no bindings" as either an empty object or as null, so an
    absent value is exactly that absence; publication is what is stated, and
    what is refused.
    """
    host = document["HostConfig"]
    if host.get("NetworkMode") == "host":
        raise ValueError("runner-attestation-mismatch")
    published = host.get("PortBindings") or {}
    exposed = (document.get("NetworkSettings") or {}).get("Ports") or {}
    if published or any(bindings for bindings in exposed.values()):
        raise ValueError("runner-attestation-mismatch")


def _mount(document: dict[str, Any], destination: str) -> dict[str, Any] | None:
    return next(
        (
            mount
            for mount in document.get("Mounts") or ()
            if mount.get("Destination") == destination
        ),
        None,
    )


def attest_runner_inspect(inspect_path: Path, manifest_path: Path, output: Path) -> int:
    """Read the created Runner container back against the manifest Core selected.

    This is carrier truth, not launcher decoration: what a container really is
    can only be read out of the engine that created it, and Core admits the
    session only when the manifest identity this writes matches the one it
    bound.
    """
    document = json.loads(inspect_path.read_text(encoding="utf-8"))
    if isinstance(document, list):
        document = document[0]
    manifest = decode_runner_manifest(manifest_path.read_bytes())
    output.write_text(
        attest_runner_container(document, manifest) + "\n", encoding="ascii"
    )
    return 0


def attest_runner_container(
    document: dict[str, Any], manifest: RunnerManifestV1
) -> str:
    """The manifest identity one created Runner container attests, or a refusal."""
    host = document["HostConfig"]
    config = document["Config"]
    tmpfs = host.get("Tmpfs") or {}
    security = host.get("SecurityOpt") or []
    cap_drop = host.get("CapDrop") or []
    user = config.get("User") or ""
    expected_user = f"{manifest.effective_uid}:{manifest.effective_gid}"
    nnp = "no-new-privileges:true" in security
    identity_mount = _mount(document, "/run/atelier2-identity")
    # `docker inspect` reports "volume" for both a tmpfs-backed and a
    # disk-backed named volume, so this attests only the mount's type and
    # writability -- the same shape identity's own volume mount is attested
    # below. It cannot itself prove the journal will survive this exact
    # container's own restart (`#15-B5`); the `resume` witness leg proves
    # that live, by actually restarting the container and resuming from it.
    journal_mount = _mount(document, "/journal")
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
        or identity_mount is None
        or identity_mount.get("RW") is not False
        or identity_mount.get("Type") != "volume"
        or journal_mount is None
        or journal_mount.get("RW") is not True
        or journal_mount.get("Type") != "volume"
    ):
        raise ValueError("runner-attestation-mismatch")
    _attest_writable_surface_is_noexec_tmpfs(tmpfs, manifest)
    _attest_credential_directory_is_the_only_read_only_bind(document, manifest)
    _attest_the_attempt_is_the_only_way_in(document)
    return runner_manifest_id(manifest).value


def _tmpfs_argument(value: str) -> TmpfsMount:
    fields = value.split(":")
    if len(fields) not in {2, 3}:
        raise argparse.ArgumentTypeError("expected DESTINATION:BYTES[:MODE]")
    mode = int(fields[2], 8) if len(fields) == 3 else None
    return TmpfsMount(PurePosixPath(fields[0]), int(fields[1]), mode)


def _bind_argument(value: str) -> BindMount:
    fields = value.split(":")
    if len(fields) != 3:
        raise argparse.ArgumentTypeError("expected SOURCE:DESTINATION:ro|rw")
    return BindMount(Path(fields[0]), PurePosixPath(fields[1]), MountRight(fields[2]))


def _volume_argument(value: str) -> VolumeMount:
    fields = value.split(":")
    if len(fields) != 3:
        raise argparse.ArgumentTypeError("expected VOLUME:DESTINATION:ro|rw")
    return VolumeMount(fields[0], PurePosixPath(fields[1]), MountRight(fields[2]))


def _spec_from(parsed: argparse.Namespace) -> ContainerSpec:
    return ContainerSpec(
        parsed.name,
        parsed.image,
        parsed.label,
        ContainerHardening(
            parsed.user,
            parsed.read_only,
            parsed.cap_drop_all,
            parsed.no_new_privileges,
            parsed.pids_limit,
            parsed.memory_bytes,
            parsed.cpu_period,
            parsed.cpu_quota,
        ),
        tuple(parsed.tmpfs),
        tuple(parsed.bind),
        tuple(parsed.volume),
        parsed.entrypoint,
        tuple(parsed.argument),
    )


def _add_container_spec_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--name", required=True)
    command.add_argument("--image", required=True)
    command.add_argument("--label", required=True)
    command.add_argument("--user")
    command.add_argument("--read-only", action="store_true")
    command.add_argument("--cap-drop-all", action="store_true")
    command.add_argument("--no-new-privileges", action="store_true")
    command.add_argument("--pids-limit", type=int)
    command.add_argument("--memory-bytes", type=int)
    command.add_argument("--cpu-period", type=int)
    command.add_argument("--cpu-quota", type=int)
    command.add_argument("--tmpfs", type=_tmpfs_argument, action="append", default=[])
    command.add_argument("--bind", type=_bind_argument, action="append", default=[])
    command.add_argument("--volume", type=_volume_argument, action="append", default=[])
    command.add_argument("--entrypoint")
    command.add_argument("--argument", action="append", default=[])


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="The carrier operations a shell caller drives the engine through."
    )
    parser.add_argument("--policy-image", required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    network = commands.add_parser("create-network")
    network.add_argument("--name", required=True)
    network.add_argument("--label", required=True)

    volume = commands.add_parser("create-volume")
    volume.add_argument("--name", required=True)
    volume.add_argument("--label", required=True)
    volume.add_argument("--tmpfs-bytes", type=int)
    volume.add_argument("--tmpfs-uid", type=int)
    volume.add_argument("--tmpfs-gid", type=int)
    volume.add_argument("--tmpfs-mode", type=lambda value: int(value, 8))
    volume.add_argument("--own-with-image")
    volume.add_argument("--owner-uid", type=int)
    volume.add_argument("--owner-gid", type=int)

    started = commands.add_parser("start-policed")
    _add_container_spec_arguments(started)
    started.add_argument("--network", required=True)
    started.add_argument("--subnet", required=True)
    started.add_argument("--role", type=ContainerRole, required=True)

    policed = commands.add_parser("police")
    policed.add_argument("--container", required=True)
    policed.add_argument("--subnet", required=True)
    policed.add_argument("--role", type=ContainerRole, required=True)

    start = commands.add_parser("start")
    start.add_argument("--container", required=True)

    wait = commands.add_parser("wait")
    wait.add_argument("--container", required=True)

    logs = commands.add_parser("logs")
    logs.add_argument("--container", required=True)
    logs.add_argument("--output", type=Path, required=True)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--container", required=True)
    inspect.add_argument("--output", type=Path, required=True)

    into = commands.add_parser("copy-into")
    into.add_argument("--container", required=True)
    into.add_argument("--source", type=Path, action="append", required=True)
    into.add_argument("--destination", type=PurePosixPath, required=True)
    into.add_argument("--deadline-seconds", type=float, required=True)

    out = commands.add_parser("copy-from")
    out.add_argument("--container", required=True)
    out.add_argument("--source", type=PurePosixPath, action="append", required=True)
    out.add_argument("--destination", type=Path, required=True)
    out.add_argument("--deadline-seconds", type=float, required=True)

    read = commands.add_parser("read-file")
    read.add_argument("--container", required=True)
    read.add_argument("--path", type=PurePosixPath, required=True)
    read.add_argument("--user", required=True)
    read.add_argument("--maximum-bytes", type=int, required=True)
    read.add_argument("--deadline-seconds", type=float, required=True)
    read.add_argument("--output", type=Path, required=True)

    remove = commands.add_parser("remove")
    remove.add_argument("--container", action="append", default=[])
    remove.add_argument("--volume", action="append", default=[])
    remove.add_argument("--network", action="append", default=[])
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    carrier = DockerCarrier(parsed.policy_image)
    if parsed.command == "create-network":
        print(carrier.create_attempt_network(parsed.name, parsed.label).subnet)
    elif parsed.command == "create-volume":
        tmpfs = (
            TmpfsVolumeOptions(
                parsed.tmpfs_bytes,
                parsed.tmpfs_uid,
                parsed.tmpfs_gid,
                parsed.tmpfs_mode,
            )
            if parsed.tmpfs_bytes is not None
            else None
        )
        carrier.create_volume(parsed.name, parsed.label, tmpfs)
        if parsed.own_with_image is not None:
            carrier.own_volume(
                parsed.name, parsed.own_with_image, parsed.owner_uid, parsed.owner_gid
            )
    elif parsed.command == "start-policed":
        print(
            carrier.start_policed_container(
                _spec_from(parsed),
                AttemptNetwork(parsed.network, parsed.subnet),
                parsed.role,
            )
        )
    elif parsed.command == "police":
        carrier.install_attempt_policy(parsed.container, parsed.subnet, parsed.role)
    elif parsed.command == "start":
        carrier.start_container(parsed.container)
    elif parsed.command == "wait":
        print(carrier.wait_for_exit(parsed.container))
    elif parsed.command == "logs":
        carrier.capture_logs(parsed.container, parsed.output)
    elif parsed.command == "inspect":
        parsed.output.write_text(
            json.dumps(carrier.inspect_container(parsed.container)), encoding="utf-8"
        )
    elif parsed.command == "copy-into":
        carrier.copy_into_container(
            parsed.container, parsed.source, parsed.destination, parsed.deadline_seconds
        )
    elif parsed.command == "copy-from":
        carrier.copy_from_container(
            parsed.container, parsed.source, parsed.destination, parsed.deadline_seconds
        )
    elif parsed.command == "read-file":
        parsed.output.write_bytes(
            carrier.read_file_in_container(
                parsed.container,
                parsed.path,
                parsed.user,
                parsed.maximum_bytes,
                parsed.deadline_seconds,
            )
        )
    else:
        carrier.remove_containers(parsed.container)
        carrier.remove_volumes(parsed.volume)
        carrier.remove_networks(parsed.network)
    return 0


if __name__ == "__main__":
    try:
        status = main()
    except CarrierRefusal as refusal:
        # A refusal is a named answer to the caller, not a stack trace: the
        # shell caller that drives these operations reads exit status and the
        # one sentence, and fails its own run on it.
        print(refusal, file=sys.stderr)
        status = 1
    raise SystemExit(status)
