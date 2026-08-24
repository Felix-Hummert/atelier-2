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
only values that reach it are this Attempt's own subnet, its served port, and
the chain names `AttemptChains` has already refused unless they are bounded
upper-case chain names.
"""

from __future__ import annotations

import argparse
import json
import re
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
_CHAIN_NAME = re.compile(r"[A-Z0-9-]{1,28}")
_ATTEMPT_CHAIN_PREFIX = "ATELIER2-"
_ATTEMPT_CHAIN_CHARACTERS = 12


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
class AttemptChains:
    """The two named chains one Attempt's rules live in, inside one container's
    own network namespace.

    A long-lived console outlives the Attempts it is attached to, so an
    Attempt's rules may not be appended to the namespace's own chains: the next
    Attempt's would sit behind the first Attempt's ACCEPTs and quietly widen
    both. Each Attempt gets its own pair of chains instead, jumped to from the
    dispatch chains the base policy installs once, and removed whole at
    release -- which is what makes a console attachable to one Attempt after
    another.

    The names are the one caller-supplied value that reaches the policy
    program's own `sh`, so anything but a bounded upper-case chain name is
    refused here rather than rendered. `iptables` accepts no longer one anyway
    (`XT_EXTENSION_MAXNAMELEN`).
    """

    inbound: str
    outbound: str

    def __post_init__(self) -> None:
        for name in (self.inbound, self.outbound):
            if _CHAIN_NAME.fullmatch(name) is None:
                raise ValueError(f"carrier-chain-name-refused: {name}")


def attempt_chains(attempt: str) -> AttemptChains:
    """The chains one Attempt's rules live in, named after the Attempt itself.

    A chain name is far shorter than an Attempt's own 64-hex identity, so it
    carries a bounded prefix of it. That name is a label inside one container's
    namespace and never an identity -- which objects belong to an Attempt is
    its lease label. Two Attempts whose prefixes met would collide on one
    chain, and the engine refuses to create a chain that exists rather than
    silently sharing it.
    """
    prefix = attempt[:_ATTEMPT_CHAIN_CHARACTERS].upper()
    return AttemptChains(
        f"{_ATTEMPT_CHAIN_PREFIX}{prefix}-IN", f"{_ATTEMPT_CHAIN_PREFIX}{prefix}-OUT"
    )


@dataclass(frozen=True, slots=True)
class AttemptAttachment:
    """One container's whole place in one Attempt, as the launcher asks for it.

    `base_network` is the network a long-lived console keeps besides its
    Attempts: the operator declared it, the console serves the cockpit on it,
    and it stays attached there while Attempts come and go. A container that
    has none -- every Runner -- is reachable on its one Attempt network and
    nowhere else.
    """

    chains: AttemptChains
    network: AttemptNetwork
    role: ContainerRole
    base_network: str | None = None

    def __post_init__(self) -> None:
        """A console has a base network; a Runner has none.

        The base policy is written against the base network's subnet, and the
        attestation against its name, so a console without one would render a
        rule about a network nobody named. A Runner with one would be reachable
        outside the Attempt it exists for.
        """
        if (self.role is ContainerRole.CORE) != (self.base_network is not None):
            raise ValueError(
                f"carrier-base-network-differs: {self.role.value} with "
                f"base network {self.base_network}"
            )

    def attached_networks(self) -> frozenset[str]:
        """Every network this container is reachable on once it is attached.

        Positive by construction: this is what the attestation reads back out
        of the engine, so "the declared base network and exactly one Attempt
        network, and nothing else" is one sentence written once.
        """
        return frozenset(
            {self.network.name}
            | ({self.base_network} if self.base_network is not None else set())
        )


@dataclass(frozen=True, slots=True)
class TmpfsVolumeOptions:
    """A volume the engine backs with tmpfs, for content that must not reach
    disk and is reproducible from what the host already holds."""

    size_bytes: int
    uid: int
    gid: int
    mode: int


# The last thing the base policy installs, and therefore the one thing whose
# presence proves the whole of it is in a namespace. It is an empty chain
# rather than a rule: a half-installed base policy that had left the most
# generic firewall line behind would otherwise be read as a complete one, and
# every later Attempt would skip installing a default-deny that is not there.
# It is also named after this arrangement, so nothing else in that namespace
# writes it by coincidence.
_BASE_POLICY_SENTINEL = "ATELIER2-BASE-INSTALLED"
_ATTEMPT_POLICY_REMAINS = "carrier-attempt-policy-remains"
_ATTEMPT_POLICY_UNREADABLE = "carrier-attempt-policy-unreadable"
# Where every Attempt's own chain is jumped to from. They are installed once
# per namespace, before the base policy's rejects, so an Attempt's chain can be
# appended at any later time and still be reached.
_INBOUND_DISPATCH = "ATELIER2-ATTEMPTS-IN"
_OUTBOUND_DISPATCH = "ATELIER2-ATTEMPTS-OUT"
# The Attempt networks this carrier creates are IPv4. IPv6 therefore carries a
# blanket reject that no Attempt ever widens, and needs no chain of its own.
_IPV6_RULES = (
    "ip6tables -A OUTPUT -o lo -j ACCEPT",
    "ip6tables -A OUTPUT -j REJECT",
    "ip6tables -A INPUT -i lo -j ACCEPT",
    "ip6tables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
    "ip6tables -A INPUT -j REJECT",
)


def _base_rules(role: ContainerRole, base_subnet: str | None) -> list[str]:
    """What one container may reach before any Attempt is added to it.

    Everything not named here or in an Attempt's own chain is REJECTed in both
    directions -- a loud, immediate connection failure the provider CLI's own
    error handling surfaces, never a silent DROP timeout.

    A console reaches its declared base network and answers on it, and nothing
    else: it holds the private key and the only store of product truth and has
    no business on the Internet, but it must keep serving the cockpit while
    Attempts come and go. A Runner has no base network at all; it reaches
    outbound DNS and HTTPS for its provider's API and accepts nothing inbound,
    because it dials out and its answers return as established connections.
    """
    outbound = ["iptables -A OUTPUT -o lo -j ACCEPT"]
    inbound = [
        "iptables -A INPUT -i lo -j ACCEPT",
        "iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
    ]
    if role is ContainerRole.CORE:
        outbound += [
            "iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT",
            f"iptables -A OUTPUT -d {base_subnet} -j ACCEPT",
        ]
        inbound.append(f"iptables -A INPUT -s {base_subnet} -j ACCEPT")
    else:
        outbound += [
            "iptables -A OUTPUT -p udp --dport 53 -j ACCEPT",
            "iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT",
            "iptables -A OUTPUT -p tcp --dport 443 -j ACCEPT",
        ]
    return [
        # An install that died halfway leaves no sentinel, so the next one runs
        # this whole block again. Creating a dispatch chain that survived that
        # death is therefore not a failure; every rule after it is appended
        # ahead of nothing, because the rejects of a completed base policy
        # would have carried the sentinel with them.
        f"iptables -N {_OUTBOUND_DISPATCH} 2>/dev/null || true",
        f"iptables -N {_INBOUND_DISPATCH} 2>/dev/null || true",
        *outbound,
        f"iptables -A OUTPUT -j {_OUTBOUND_DISPATCH}",
        "iptables -A OUTPUT -p tcp -j REJECT --reject-with tcp-reset",
        "iptables -A OUTPUT -j REJECT --reject-with icmp-port-unreachable",
        *inbound,
        f"iptables -A INPUT -j {_INBOUND_DISPATCH}",
        "iptables -A INPUT -p tcp -j REJECT --reject-with tcp-reset",
        "iptables -A INPUT -j REJECT --reject-with icmp-port-unreachable",
        *_IPV6_RULES,
        f"iptables -N {_BASE_POLICY_SENTINEL}",
    ]


def _attempt_rules(attachment: AttemptAttachment) -> list[str]:
    """What exactly one Attempt adds to a container that already has a policy.

    Core is the only container in an Attempt that serves anything, and it
    serves exactly one port to exactly this Attempt's subnet. The Runner's
    inbound chain stays empty on purpose: this Attempt grants it nothing
    inbound at all.

    Creating the chains is also the fence against establishing the same Attempt
    twice in one namespace -- the engine refuses a chain that already exists,
    so a second install refuses loudly instead of appending a second copy.
    """
    chains, subnet = attachment.chains, attachment.network.subnet
    rules = [
        f"iptables -N {chains.outbound}",
        f"iptables -N {chains.inbound}",
        f"iptables -A {chains.outbound} -d {subnet} -j ACCEPT",
    ]
    if attachment.role is ContainerRole.CORE:
        rules.append(
            f"iptables -A {chains.inbound} -s {subnet} "
            f"-p tcp --dport {CORE_SESSION_PORT} -j ACCEPT"
        )
    return [
        *rules,
        f"iptables -A {_OUTBOUND_DISPATCH} -j {chains.outbound}",
        f"iptables -A {_INBOUND_DISPATCH} -j {chains.inbound}",
    ]


def _policy_program(attachment: AttemptAttachment, base_subnet: str | None) -> str:
    """One container's base policy if it has none yet, and this Attempt's chains.

    The base policy is installed once per namespace and says nothing about any
    Attempt; a long-lived console that is attached to a second Attempt keeps
    the one it already has. What each Attempt adds and removes is its own pair
    of chains.

    Whether it is already there is asked of the sentinel chain, which the base
    policy writes last. Asking a rule instead would let a base policy that died
    after its first rule count as a complete one, and every Attempt after that
    would attach to a namespace with no default-deny in it at all.
    """
    return "\n".join(
        (
            "set -e",
            f"if ! iptables -S {_BASE_POLICY_SENTINEL} >/dev/null 2>&1; then",
            *(f"  {rule}" for rule in _base_rules(attachment.role, base_subnet)),
            "fi",
            *_attempt_rules(attachment),
        )
    )


def _policy_removal_program(chains: AttemptChains) -> str:
    """Take one Attempt's chains out of a namespace, and prove they are gone.

    Every step tolerates a piece that is already gone, because removal runs
    both at release and at reconciliation of an Attempt nobody finished. What
    does not tolerate anything is the answer, and the answer is read in two
    parts: a listing that could not be taken at all is its own refusal, and
    only a listing this really holds may say the chains are gone. A ruleset
    nobody could read must never pass for an empty one -- an `iptables` that
    fails here fails for reasons (a missing `CAP_NET_ADMIN`, a namespace that
    changed underneath) that would leave the Attempt's grants standing.

    The chain names are the bounded upper-case form `AttemptChains` admits, so
    the pattern match below is a literal comparison and not a glob a name
    could carry.
    """
    return "\n".join(
        (
            f"iptables -D {_OUTBOUND_DISPATCH} -j {chains.outbound} 2>/dev/null",
            f"iptables -D {_INBOUND_DISPATCH} -j {chains.inbound} 2>/dev/null",
            f"iptables -F {chains.outbound} 2>/dev/null",
            f"iptables -F {chains.inbound} 2>/dev/null",
            f"iptables -X {chains.outbound} 2>/dev/null",
            f"iptables -X {chains.inbound} 2>/dev/null",
            "remaining=$(iptables -S) || {",
            f"  echo {_ATTEMPT_POLICY_UNREADABLE} >&2",
            "  exit 4",
            "}",
            'case "$remaining" in',
            f"  *{chains.outbound}* | *{chains.inbound}*)",
            f"    echo {_ATTEMPT_POLICY_REMAINS} >&2",
            "    exit 3",
            "    ;;",
            "esac",
        )
    )


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
        return AttemptNetwork(name, self._network_subnet(name))

    def _network_subnet(self, name: str) -> str:
        document = self._inspect("network", name)
        configurations = document["IPAM"]["Config"]
        if not configurations:
            raise CarrierRefusal(f"carrier-network-has-no-subnet: {name}")
        return str(configurations[0]["Subnet"])

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
        self, container: str, attachment: AttemptAttachment
    ) -> None:
        """Install this Attempt's chains inside one container's own network
        namespace, from a throwaway container that exits immediately.

        The base network's own subnet is read out of the engine rather than
        taken from a caller: what a console may keep reaching is the network
        the operator declared, as the engine really allocated it.

        The target container holds no packet-filtering tool and no
        `CAP_NET_ADMIN`, so it cannot alter what this leaves behind.
        """
        base_subnet = (
            self._network_subnet(attachment.base_network)
            if attachment.base_network is not None
            else None
        )
        self._run(
            self._policy_arguments(container, _policy_program(attachment, base_subnet))
        )

    def remove_attempt_policy(self, container: str, chains: AttemptChains) -> None:
        """Take one Attempt's chains back out of a container's namespace.

        A Runner's namespace dies with its container, so this is about the one
        container that outlives its Attempts: the console. What it grants after
        a release must be exactly what it granted before the Attempt existed.

        A namespace exists only while its container runs, and a console that
        stopped took every rule in it along. That is the one failure this
        tolerates, and it is decided by reading the container's state back --
        never by the wording of what the engine said.
        """
        try:
            self._run(
                self._policy_arguments(container, _policy_removal_program(chains))
            )
        except CarrierRefusal:
            if self._is_running(container):
                raise

    def _is_running(self, container: str) -> bool:
        try:
            document = self.inspect_container(container)
        except CarrierRefusal:
            # A container the engine does not know has no namespace, for the
            # same reason a removal of an object that is already gone succeeds.
            return False
        return bool((document.get("State") or {}).get("Running"))

    def _policy_arguments(self, container: str, program: str) -> list[str]:
        return [
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
            program,
        ]

    def start_private_container(self, spec: ContainerSpec) -> str:
        """Start one container that can reach nothing at all.

        This is where every container of an Attempt begins: running, but
        detached from every network, so whatever it does before its Attempt
        exists it does alone. A container may stay here indefinitely -- the
        console's own Serve container waits like this for the Attempt it will
        be attached to.
        """
        self._run(
            ["run", "-d", "--network", _PRIVATE_NETWORK, *_create_arguments(spec)]
        )
        return self._attested_container_id(spec)

    def start_policed_container(
        self, spec: ContainerSpec, attachment: AttemptAttachment
    ) -> str:
        """Start one container that never runs for a single unfiltered packet.

        It is created detached from every network, its Attempt policy is
        installed while it can reach nothing at all, and only then is it
        connected. A policy that fails to install therefore leaves a container
        unable to reach anything rather than running wide open.
        """
        container_id = self.start_private_container(spec)
        self.attach_policed_container(spec.name, attachment)
        return container_id

    def attach_policed_container(
        self, container: str, attachment: AttemptAttachment
    ) -> None:
        """Put one Attempt's chains into a container, then attach it to it.

        The Attempt's chains go in before the connect, so the first packet this
        container may ever exchange with the Attempt is already filtered. A
        container that was created private additionally reaches nothing at all
        until this runs; the console, which the deployment started on its own
        base network long before, keeps exactly what that network already gave
        it (ADR 0009 sec. 2, 2026-08-23 amendment (b)).
        """
        self.install_attempt_policy(container, attachment)
        # A container that was never in private mode has no empty namespace to
        # release, so this outcome is not asserted. What is asserted is the
        # connect below, which the engine refuses for a container still in it.
        self._succeeded(["network", "disconnect", _PRIVATE_NETWORK, container])
        alias = (
            ["--alias", CORE_DNS_NAME] if attachment.role is ContainerRole.CORE else []
        )
        self._run(["network", "connect", *alias, attachment.network.name, container])
        self._attest_attachment(container, attachment.attached_networks())

    def detach_container(self, container: str, network: str) -> None:
        """Take one container off one network, and change nothing else.

        This is how the console leaves an Attempt that failed: the Attempt's
        own objects stay on the host to be read, and the container that
        outlives them stops being reachable from a network that is over.
        Detaching what is already detached, or what is already gone, is the
        same answer as detaching it, so this states no outcome.
        """
        self._succeeded(["network", "disconnect", "--force", network, container])

    def _attest_attachment(self, container: str, expected: frozenset[str]) -> None:
        """Where this container can be reached, read back out of the engine.

        Positive, not a list of refusals: the container must be attached to
        exactly the declared base network its policy keeps open and the one
        Attempt network it was just given -- or, while it is being restarted,
        to nothing at all. Anything else, including the host's own namespace or
        a second network nobody's policy speaks about, is refused here rather
        than discovered by an Attempt that could reach further than it should.
        """
        document = self.inspect_container(container)
        attached = set(
            (document.get("NetworkSettings") or {}).get("Networks") or {}
        ) - {_PRIVATE_NETWORK}
        if attached != set(expected):
            raise CarrierRefusal(
                f"carrier-attachment-differs: {container} is attached to "
                f"{sorted(attached)}, expected {sorted(expected)}"
            )

    def restart_private_container(self, container: str) -> None:
        """Start an exited container again, reachable by nothing.

        Its Attempt's policy lives in a network namespace the restart throws
        away, so a container that came back attached would run unfiltered until
        the policy is reinstalled. Every attachment is therefore released
        first, and the container that comes up is read back as private before
        `attach_policed_container` gives it its policy and its network again --
        the same order, in the same owner, as a first start.
        """
        document = self.inspect_container(container)
        for network in (document.get("NetworkSettings") or {}).get("Networks") or {}:
            self._run(["network", "disconnect", network, container])
        self._run(["start", container])
        self._attest_attachment(container, frozenset())

    def run_receiving_stdin(self, spec: ContainerSpec, stdin: bytes) -> None:
        """Run one container to completion on a pipe, on no network at all.

        This is how material reaches an Attempt without the host ever binding a
        directory into it: the container reads what it is given from its own
        standard input, writes it where it was told, and exits.
        """
        self._run(
            [
                "run",
                "-i",
                "--rm",
                "--network",
                _PRIVATE_NETWORK,
                *_create_arguments(spec),
            ],
            stdin,
        )

    def file_exists_in_volume(
        self, volume: str, image: str, path: PurePosixPath
    ) -> bool:
        """Whether a volume holds one file, read from a throwaway container.

        The volume is the only thing mounted and the container runs one test
        and exits, so asking this question grants nothing and changes nothing.
        """
        return self._succeeded(
            [
                "run",
                "--rm",
                "--user",
                "root",
                "--mount",
                f"type=volume,src={volume},dst={path.parent},volume-nocopy",
                "--entrypoint",
                "test",
                image,
                "-f",
                str(path),
            ]
        )

    def read_file_in_volume(
        self, volume: str, image: str, path: PurePosixPath, maximum_bytes: int
    ) -> bytes:
        """Read one bounded file out of a volume, from a throwaway container.

        The same shape as `file_exists_in_volume`, and used where a container
        that once wrote the file has already exited: the volume outlives it, so
        a launcher reads the retained bytes back off the volume itself rather
        than out of a process that is no longer there
        (`atelier2.host.runner_launcher`). The volume is mounted read-only and
        is the only thing mounted, and the file is refused if it crosses the
        caller's bound -- it was written by the least-trusted process on the
        host.

        The bound is enforced at the pipe, not after: `head -c` caps the read
        inside the throwaway container to `maximum_bytes + 1` bytes, so a
        compromised Runner that wrote a multi-gigabyte file to its own quota-free
        journal volume can never drive this launcher to buffer the whole thing
        into memory. At most one byte past the bound ever reaches the launcher --
        exactly enough to still detect and refuse an over-bound file.
        """
        completed = subprocess.run(
            [
                str(self.executable),
                "run",
                "--rm",
                "--user",
                "root",
                "--mount",
                f"type=volume,src={volume},dst={path.parent},volume-nocopy,readonly",
                "--entrypoint",
                "head",
                image,
                "-c",
                str(maximum_bytes + 1),
                "--",
                str(path),
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise CarrierRefusal(
                f"carrier-volume-file-unreadable: {volume} {path}: "
                f"{completed.stderr.decode('utf-8', 'replace').strip()}"
            )
        if len(completed.stdout) > maximum_bytes:
            raise CarrierRefusal(
                f"carrier-volume-file-exceeds-bound: {path} is "
                f"{len(completed.stdout)} bytes, bound {maximum_bytes}"
            )
        return completed.stdout

    def container_running(self, container: str) -> bool:
        """Whether a container still runs, answering false for one that is gone.

        A launcher retaining an exited Runner's terminal fact before it removes
        the Attempt must never read a Runner that is still writing: this is the
        gate that keeps that read to a container that has already stopped, or is
        gone entirely.
        """
        try:
            document = self.inspect_container(container)
        except CarrierRefusal:
            return False
        return bool((document.get("State") or {}).get("Running"))

    def labelled_containers(self, label: str) -> tuple[str, ...]:
        return self._labelled(["ps", "--all", "--no-trunc", "--quiet"], label)

    def labelled_volumes(self, label: str) -> tuple[str, ...]:
        return self._labelled(["volume", "ls", "--quiet"], label)

    def labelled_networks(self, label: str) -> tuple[str, ...]:
        return self._labelled(["network", "ls", "--quiet"], label)

    def _labelled(self, listing: Sequence[str], label: str) -> tuple[str, ...]:
        """Every object carrying exactly this label, and nothing else.

        Reconciliation removes what it finds here, so the filter is the whole
        safety of it: an object without this Attempt's own label is not this
        launcher's to remove.
        """
        return tuple(self._run([*listing, "--filter", f"label={label}"]).split())

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
        """Remove Attempt networks, detaching whatever is still attached.

        The console's own container outlives the Attempt it was attached to, so
        an Attempt network is never free of containers when its Attempt ends.
        Detaching is all that happens to them: nothing here stops or removes a
        container it did not create.
        """
        for network in networks:
            for container in self._attached_containers(network):
                self._succeeded(
                    ["network", "disconnect", "--force", network, container]
                )
            self._succeeded(["network", "rm", network])

    def _attached_containers(self, network: str) -> tuple[str, ...]:
        try:
            document = self._inspect("network", network)
        except CarrierRefusal:
            # A network that is already gone has nothing attached to it; the
            # removal below is idempotent for the same reason.
            return ()
        return tuple(document.get("Containers") or ())


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
    """No published port, and a container that was created private.

    An Attempt's single inbound opening is Core's session port inside the
    Attempt's own subnet. A published port would put the Runner on an address
    the host's own neighbours can reach; the host's own namespace, or any other
    network mode, would mean the Attempt policy this carrier installs is
    filtering something other than one Attempt.

    The network mode is therefore read positively: `none` is what every
    container this carrier creates is created as, and it stays `none` in the
    engine's own report after the Attempt network is connected, so it says
    exactly "this container was created reachable by nothing". Which network it
    then got is a different question, and the carrier answers that one where it
    attaches (`_attest_attachment`).

    Ports are read the other way round, because the engine reports "no
    bindings" as an empty object *or* as null -- an absent value is exactly
    that absence. Publication is what is stated, and what is refused.
    """
    host = document["HostConfig"]
    mode = host.get("NetworkMode")
    if mode is not None and mode != _PRIVATE_NETWORK:
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


def _add_attachment_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--network", required=True)
    command.add_argument("--subnet", required=True)
    command.add_argument("--role", type=ContainerRole, required=True)
    command.add_argument("--attempt", required=True)
    command.add_argument("--base-network")


def _attachment_from(parsed: argparse.Namespace) -> AttemptAttachment:
    return AttemptAttachment(
        attempt_chains(parsed.attempt),
        AttemptNetwork(parsed.network, parsed.subnet),
        parsed.role,
        parsed.base_network,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="The carrier operations a shell caller drives the engine through."
    )
    parser.add_argument("--policy-image", required=True)
    commands = parser.add_subparsers(dest="command", required=True)

    network = commands.add_parser("create-network")
    network.add_argument("--name", required=True)
    network.add_argument("--label", required=True)

    private = commands.add_parser("start-private")
    _add_container_spec_arguments(private)

    started = commands.add_parser("start-policed")
    _add_container_spec_arguments(started)
    _add_attachment_arguments(started)

    attached = commands.add_parser("attach-policed")
    attached.add_argument("--container", required=True)
    _add_attachment_arguments(attached)

    released = commands.add_parser("remove-policy")
    released.add_argument("--container", required=True)
    released.add_argument("--attempt", required=True)

    wait = commands.add_parser("wait")
    wait.add_argument("--container", required=True)

    logs = commands.add_parser("logs")
    logs.add_argument("--container", required=True)
    logs.add_argument("--output", type=Path, required=True)

    out = commands.add_parser("copy-from")
    out.add_argument("--container", required=True)
    out.add_argument("--source", type=PurePosixPath, action="append", required=True)
    out.add_argument("--destination", type=Path, required=True)
    out.add_argument("--deadline-seconds", type=float, required=True)

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
    elif parsed.command == "start-private":
        print(carrier.start_private_container(_spec_from(parsed)))
    elif parsed.command == "start-policed":
        print(
            carrier.start_policed_container(
                _spec_from(parsed), _attachment_from(parsed)
            )
        )
    elif parsed.command == "attach-policed":
        carrier.attach_policed_container(parsed.container, _attachment_from(parsed))
    elif parsed.command == "remove-policy":
        carrier.remove_attempt_policy(parsed.container, attempt_chains(parsed.attempt))
    elif parsed.command == "wait":
        print(carrier.wait_for_exit(parsed.container))
    elif parsed.command == "logs":
        carrier.capture_logs(parsed.container, parsed.output)
    elif parsed.command == "copy-from":
        carrier.copy_from_container(
            parsed.container, parsed.source, parsed.destination, parsed.deadline_seconds
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
