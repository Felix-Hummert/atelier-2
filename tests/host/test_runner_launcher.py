"""What the launcher does with a lease, and what it leaves on the host.

The engine is replaced by a recording stand-in that satisfies the launcher's
own `AttemptCarrier` surface -- the whole privilege the launcher role holds --
so these tests read as what the launcher asked the host to do. The certificate
authority is real: identity is the one thing a fake would stop proving.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

import pytest
from cryptography import x509

from atelier2.adapters.docker_carrier import (
    AttemptAttachment,
    AttemptChains,
    AttemptNetwork,
    CarrierRefusal,
    ContainerSpec,
    MountRight,
    TmpfsVolumeOptions,
    attempt_chains,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.runner_leases import RunnerLeaseId
from atelier2.contracts.runner_manifests import (
    CANDIDATE_CPU_PERIOD,
    CANDIDATE_JOURNAL_BYTES,
    CANDIDATE_SCRATCH_BYTES,
    CANDIDATE_WORKSPACE_BYTES,
    RunnerManifestV1,
    RunnerPathRight,
    candidate_runner_manifest,
    encode_runner_manifest,
    runner_manifest_id,
)
from atelier2.host.runner_identity import RunnerIdentityAuthority
from atelier2.host.runner_launcher import (
    AttemptRefusal,
    AttemptVolumes,
    FileRunnerLeaseSource,
    RunnerLauncher,
    RunnerLease,
    RunnerLeaseValidation,
    RunnerManifestBounds,
    admitted_attempt_root,
    admitted_console_identity,
    main,
    runner_container_spec,
    single_launcher,
)

_RUNNER_IMAGE = "atelier2-runner-candidate"
_SERVE_CONTAINER = "atelier2-console"
_CONSOLE_NETWORK = "atelier2-live_serve"
_SUBNET = "10.244.9.0/24"
_INVOCATION = "invocation-one"
_LEASE_ID = "a" * 64
_SECOND_LEASE_ID = "b" * 64


def _manifest(**overrides: Any) -> RunnerManifestV1:
    return candidate_runner_manifest(
        source_commit="a" * 40,
        image_digest="sha256:" + "b" * 64,
        required_landlock_abi=1,
        executor_revision="fake-free/v1",
        executor_operational_identity="free-runner-candidate",
        provider_id="fake-free",
        auth_mode="api_key",
        requested_capability="headless",
        **overrides,
    )


def _binding(manifest: RunnerManifestV1 | None = None) -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        AgentAttemptId("a" * 64),
        AgentExecutionRequestHash("c" * 64),
        RunnerGenerationId("generation-one"),
        runner_manifest_id(manifest if manifest is not None else _manifest()),
    )


def _attested_document(manifest: RunnerManifestV1) -> dict[str, Any]:
    """The Runner container as the engine reports the one the carrier created."""
    return {
        "Id": "sha256:" + "d" * 64,
        "Image": manifest.image_digest,
        "Config": {"User": f"{manifest.effective_uid}:{manifest.effective_gid}"},
        "NetworkSettings": {"Ports": {}},
        "HostConfig": {
            # Created private and connected afterwards: the engine keeps
            # reporting the mode the container was created with.
            "NetworkMode": "none",
            "PortBindings": {},
            "ReadonlyRootfs": True,
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges:true"],
            "PidsLimit": manifest.process_limit,
            "Memory": manifest.memory_bytes,
            "CpuQuota": manifest.cpu_quota_microseconds,
            "CpuPeriod": CANDIDATE_CPU_PERIOD,
            "Tmpfs": {
                "/workspace": f"rw,noexec,nosuid,size={CANDIDATE_WORKSPACE_BYTES}",
                **{
                    grant.path.as_posix(): (
                        f"rw,noexec,nosuid,size={manifest.scratch_bytes}"
                    )
                    for grant in manifest.child_path_grants
                    if grant.right is RunnerPathRight.READ_WRITE
                },
            },
        },
        "Mounts": [
            {"Destination": "/run/atelier2-identity", "RW": False, "Type": "volume"},
            {"Destination": "/journal", "RW": True, "Type": "volume"},
            {
                "Destination": manifest.provider_credential_directory.as_posix(),
                "RW": False,
                "Type": "bind",
            },
        ],
    }


@dataclass
class RecordingCarrier:
    """A host that records what the launcher asked of it.

    `runner_exits` and `journal_records` are each read one entry per call, so a
    test states the exact lifetime it is about -- one clean exit, or a crash,
    one journal answer, and the resumed exit after it -- instead of an answer
    that would keep repeating whatever it was asked.
    """

    document: dict[str, Any]
    runner_exits: list[int]
    offer: bytes = json.dumps({"invocation_id": _INVOCATION}).encode("utf-8")
    journal_records: list[bool] = field(default_factory=list)
    delivery_refusal: Exception | None = None
    policy_removal_refusal: Exception | None = None
    created: dict[str, list[str]] = field(
        default_factory=lambda: {"containers": [], "volumes": [], "networks": []}
    )
    removed: dict[str, list[str]] = field(
        default_factory=lambda: {"containers": [], "volumes": [], "networks": []}
    )
    attached: list[tuple[str, frozenset[str]]] = field(default_factory=list)
    policed: list[str] = field(default_factory=list)
    unpoliced: list[tuple[str, AttemptChains]] = field(default_factory=list)
    detached: list[tuple[str, str]] = field(default_factory=list)
    handoffs: list[list[str]] = field(default_factory=list)
    owned: list[str] = field(default_factory=list)
    delivered_identity: bytes = b""
    started_again: list[str] = field(default_factory=list)
    runner_specification: ContainerSpec | None = None
    operations: list[str] = field(default_factory=list)

    def create_attempt_network(self, name: str, label: str) -> AttemptNetwork:
        self.created["networks"].append(name)
        return AttemptNetwork(name, _SUBNET)

    def attach_policed_container(
        self, container: str, attachment: AttemptAttachment
    ) -> None:
        self.operations.append("attach")
        self.policed.append(container)
        self.attached.append((container, attachment.attached_networks()))

    def remove_attempt_policy(self, container: str, chains: AttemptChains) -> None:
        if self.policy_removal_refusal is not None:
            raise self.policy_removal_refusal
        self.operations.append("remove-policy")
        self.unpoliced.append((container, chains))

    def detach_container(self, container: str, network: str) -> None:
        self.operations.append("detach")
        self.detached.append((container, network))

    def create_volume(
        self, name: str, label: str, tmpfs: TmpfsVolumeOptions | None = None
    ) -> None:
        self.created["volumes"].append(name)

    def own_volume(self, name: str, image: str, uid: int, gid: int) -> None:
        self.owned.append(name)

    def start_policed_container(
        self, spec: ContainerSpec, attachment: AttemptAttachment
    ) -> str:
        self.operations.append("start-policed")
        self.runner_specification = spec
        self.created["containers"].append(spec.name)
        self.policed.append(spec.name)
        self.attached.append((spec.name, attachment.attached_networks()))
        return str(self.document["Id"])

    def restart_private_container(self, container: str) -> None:
        self.operations.append("restart-private")
        self.started_again.append(container)

    def wait_for_exit(self, container: str) -> int:
        return self.runner_exits.pop(0)

    def inspect_container(self, container: str) -> dict[str, Any]:
        return self.document

    def copy_into_container(
        self,
        container: str,
        sources: Sequence[Path],
        destination: PurePosixPath,
        deadline_seconds: float,
    ) -> None:
        self.operations.append("handoff")
        self.handoffs.append([source.name for source in sources])

    def read_file_in_container(
        self,
        container: str,
        path: PurePosixPath,
        user: str,
        maximum_bytes: int,
        deadline_seconds: float,
    ) -> bytes:
        return self.offer

    def run_receiving_stdin(self, spec: ContainerSpec, stdin: bytes) -> None:
        self.delivered_identity = stdin
        if self.delivery_refusal is not None:
            raise self.delivery_refusal

    def file_exists_in_volume(
        self, volume: str, image: str, path: PurePosixPath
    ) -> bool:
        return self.journal_records.pop(0)

    def labelled_containers(self, label: str) -> tuple[str, ...]:
        return tuple(self.created["containers"])

    def labelled_volumes(self, label: str) -> tuple[str, ...]:
        return tuple(self.created["volumes"])

    def labelled_networks(self, label: str) -> tuple[str, ...]:
        return tuple(self.created["networks"])

    def remove_containers(self, containers: Any) -> None:
        self.removed["containers"].extend(containers)

    def remove_volumes(self, volumes: Any) -> None:
        self.removed["volumes"].extend(volumes)

    def remove_networks(self, networks: Any) -> None:
        self.removed["networks"].extend(networks)


def _attempt_directories(root: Path) -> Path:
    """The per-Attempt tree a lease may name, as the writer prepares it."""
    for name in ("handoff", "peer", "issuance", "provider-credentials"):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def _lease(
    root: Path,
    manifest: RunnerManifestV1 | None = None,
    lease_id: str = _LEASE_ID,
    runner_image: str = _RUNNER_IMAGE,
    binding: RunnerGenerationBinding | None = None,
) -> RunnerLease:
    _attempt_directories(root)
    return RunnerLease(
        RunnerLeaseId(lease_id),
        binding if binding is not None else _binding(),
        runner_image,
        manifest if manifest is not None else _manifest(),
        _SERVE_CONTAINER,
        root / "handoff",
        root / "peer",
        root / "issuance",
        root / "provider-credentials",
    )


def _validation(
    root: Path, bounds: RunnerManifestBounds | None = None
) -> RunnerLeaseValidation:
    return RunnerLeaseValidation(
        root,
        _SERVE_CONTAINER,
        _CONSOLE_NETWORK,
        _RUNNER_IMAGE,
        bounds if bounds is not None else RunnerManifestBounds(),
    )


def _launcher(
    root: Path, carrier: RecordingCarrier, source: Any
) -> tuple[RunnerLauncher, list[str]]:
    announced: list[str] = []
    launcher = RunnerLauncher(
        carrier,
        RunnerIdentityAuthority(root / "authority"),
        source,
        _validation(root),
        announced.append,
    )
    return launcher, announced


class SourceExhausted(Exception):
    """What a test uses to end a watching launcher, standing in for the
    operator stopping the process. It is deliberately not an Attempt refusal,
    so it also shows how narrow the loop's own catch is."""


@dataclass
class OpenLeases:
    """A source holding exactly the leases a test is about, in order.

    `exhausted` makes a watching loop end: without it a launcher that is
    working correctly would poll an empty source forever, which is what a
    launcher is for and what a test cannot wait for.
    """

    open_leases: list[RunnerLease]
    released: list[str] = field(default_factory=list)
    stale: tuple[str, ...] = ()
    exhausted: type[Exception] | None = None

    def claim_open_lease(self) -> RunnerLease | None:
        if self.open_leases:
            return self.open_leases.pop(0)
        if self.exhausted is not None:
            raise self.exhausted()
        return None

    def release(self, lease: RunnerLease) -> None:
        self.released.append(lease.lease_id.value)

    def abandon_stale_claims(self) -> tuple[str, ...]:
        abandoned, self.stale = self.stale, ()
        return abandoned


def test_a_lease_document_is_claimed_by_exactly_one_launcher(tmp_path: Path) -> None:
    """Two launchers may watch one source; only one may establish an Attempt."""
    manifest = _manifest()
    (tmp_path / "manifest").write_bytes(encode_runner_manifest(manifest))
    (tmp_path / "bootstrap.json").write_text(
        json.dumps(
            {
                "attempt_id": "a" * 64,
                "request_hash": "c" * 64,
                "generation_id": "generation-one",
                "manifest_id": runner_manifest_id(manifest).value,
            }
        ),
        encoding="utf-8",
    )
    directory = tmp_path / "leases"
    source = FileRunnerLeaseSource(directory, _validation(tmp_path))
    (directory / "open" / f"{_LEASE_ID}.json").write_text(
        json.dumps(
            {
                "binding_path": str(tmp_path / "bootstrap.json"),
                "manifest_path": str(tmp_path / "manifest"),
                "runner_image": _RUNNER_IMAGE,
                "serve_container": _SERVE_CONTAINER,
                "handoff_directory": str(tmp_path / "handoff"),
                "core_peer_directory": str(tmp_path / "peer"),
                "issuance_directory": str(tmp_path / "issuance"),
                "provider_credential_source": str(tmp_path / "credentials"),
            }
        ),
        encoding="utf-8",
    )

    claimed = source.claim_open_lease()

    assert claimed is not None
    assert claimed.lease_id == RunnerLeaseId(_LEASE_ID)
    assert claimed.binding == _binding()
    assert (
        FileRunnerLeaseSource(directory, _validation(tmp_path)).claim_open_lease()
        is None
    )

    source.release(claimed)

    assert (directory / "released" / f"{_LEASE_ID}.json").is_file()


def test_a_lease_a_dead_launcher_left_claimed_is_abandoned_with_its_objects(
    tmp_path: Path,
) -> None:
    """An Attempt that may already have run is never silently run again: what
    it left on the host is removed -- including in the console, which outlives
    it -- and the lease goes back to its owner."""
    interrupted = "f" * 64
    directory = tmp_path / "leases"
    FileRunnerLeaseSource(directory, _validation(tmp_path))
    (directory / "claimed" / f"{interrupted}.json").write_text("{}", encoding="utf-8")
    carrier = RecordingCarrier(_attested_document(_manifest()), [])
    carrier.created["containers"].append(f"atelier2-attempt-{interrupted}-runner")
    carrier.created["volumes"].append(f"atelier2-attempt-{interrupted}-journal")
    carrier.created["networks"].append(f"atelier2-attempt-{interrupted}")
    launcher, announced = _launcher(
        tmp_path, carrier, FileRunnerLeaseSource(directory, _validation(tmp_path))
    )

    launcher.reconcile_abandoned_attempts()

    assert f"reconciled-attempt={interrupted}" in announced
    assert carrier.unpoliced == [(_SERVE_CONTAINER, attempt_chains(interrupted))]
    assert carrier.removed["containers"] == [f"atelier2-attempt-{interrupted}-runner"]
    assert carrier.removed["volumes"] == [f"atelier2-attempt-{interrupted}-journal"]
    assert carrier.removed["networks"] == [f"atelier2-attempt-{interrupted}"]
    assert (directory / "released" / f"{interrupted}.json").is_file()


def test_a_claimed_document_nobody_named_a_lease_removes_nothing(
    tmp_path: Path,
) -> None:
    """A lease directory is written by the same untrusted writer at both ends.
    A claimed name that is not a lease id never named an object this launcher
    created, and it never becomes a label or a chain name either."""
    directory = tmp_path / "leases"
    FileRunnerLeaseSource(directory, _validation(tmp_path))
    (directory / "claimed" / "; rm -rf .json").write_text("{}", encoding="utf-8")
    carrier = RecordingCarrier(_attested_document(_manifest()), [])
    carrier.created["containers"].append("atelier2-console")
    launcher, announced = _launcher(
        tmp_path, carrier, FileRunnerLeaseSource(directory, _validation(tmp_path))
    )

    launcher.reconcile_abandoned_attempts()

    assert any("lease-name-is-not-a-lease-id-form" in line for line in announced)
    assert carrier.removed["containers"] == []
    assert carrier.unpoliced == []


def test_a_console_that_is_gone_does_not_stop_a_launcher_from_starting(
    tmp_path: Path,
) -> None:
    """Residue from a previous deployment is reported and stepped over: a
    launcher refusing to start over a console that no longer exists would turn
    a leftover into an outage."""
    interrupted = "e" * 64
    directory = tmp_path / "leases"
    FileRunnerLeaseSource(directory, _validation(tmp_path))
    (directory / "claimed" / f"{interrupted}.json").write_text("{}", encoding="utf-8")
    carrier = RecordingCarrier(_attested_document(_manifest()), [])
    carrier.policy_removal_refusal = CarrierRefusal("carrier-command-refused: no such")
    launcher, announced = _launcher(
        tmp_path, carrier, FileRunnerLeaseSource(directory, _validation(tmp_path))
    )

    launcher.reconcile_abandoned_attempts()

    assert any(line.startswith("reconcile-refused=") for line in announced)


def test_an_established_attempt_leaves_nothing_behind(tmp_path: Path) -> None:
    """The whole ensemble, and then a host that looks as it did before."""
    carrier = RecordingCarrier(_attested_document(_manifest()), [0])
    lease = _lease(tmp_path)
    source = OpenLeases([lease])
    launcher, announced = _launcher(tmp_path, carrier, source)

    launcher.establish(lease)

    assert (
        _SERVE_CONTAINER,
        frozenset({_CONSOLE_NETWORK, f"atelier2-attempt-{_LEASE_ID}"}),
    ) in carrier.attached
    assert carrier.unpoliced == [(_SERVE_CONTAINER, attempt_chains(_LEASE_ID))]
    assert carrier.owned == [
        f"atelier2-attempt-{_LEASE_ID}-identity",
        f"atelier2-attempt-{_LEASE_ID}-journal",
    ]
    assert carrier.delivered_identity
    assert (lease.core_peer_directory / "client.crt").is_file()
    assert not (lease.issuance_directory / "client.key").exists()
    attested = (lease.handoff_directory / "inspect-attested").read_text(
        encoding="ascii"
    )
    assert attested.strip() == lease.binding.manifest_id.value
    assert carrier.removed == {
        "containers": [f"atelier2-attempt-{_LEASE_ID}-runner"],
        "volumes": [
            f"atelier2-attempt-{_LEASE_ID}-identity",
            f"atelier2-attempt-{_LEASE_ID}-journal",
            f"atelier2-attempt-{_LEASE_ID}-handoff",
        ],
        "networks": [f"atelier2-attempt-{_LEASE_ID}"],
    }
    assert _SERVE_CONTAINER not in carrier.removed["containers"]
    assert source.released == [_LEASE_ID]
    assert f"attempt-network=atelier2-attempt-{_LEASE_ID}" in announced


def test_the_runner_container_is_the_one_the_manifest_describes(
    tmp_path: Path,
) -> None:
    """Every number and right in the container comes from the attested
    manifest, so the container Core selected is the container that runs."""
    lease = _lease(tmp_path)
    manifest = lease.manifest

    specification = runner_container_spec(
        lease, "attempt-runner", AttemptVolumes("identity", "handoff", "journal")
    )

    assert specification.hardening.user == (
        f"{manifest.effective_uid}:{manifest.effective_gid}"
    )
    assert specification.hardening.process_limit == manifest.process_limit
    assert specification.hardening.memory_bytes == manifest.memory_bytes
    assert specification.hardening.read_only_root
    assert specification.hardening.drop_all_capabilities
    assert specification.hardening.no_new_privileges
    assert [
        (bind.source, bind.destination, bind.right) for bind in specification.binds
    ] == [
        (
            lease.provider_credential_source,
            manifest.provider_credential_directory,
            MountRight.READ_ONLY,
        )
    ]
    rights = {
        volume.destination.as_posix(): volume.right for volume in specification.volumes
    }
    assert rights == {
        "/handoff": MountRight.READ_WRITE,
        "/run/atelier2-identity": MountRight.READ_ONLY,
        "/journal": MountRight.READ_WRITE,
    }
    writable = {
        grant.path.as_posix()
        for grant in manifest.child_path_grants
        if grant.right is RunnerPathRight.READ_WRITE
    }
    mounted = {mount.destination.as_posix(): mount for mount in specification.tmpfs}
    assert writable <= set(mounted)
    for path in writable:
        assert mounted[path].size_bytes == manifest.scratch_bytes
        assert "noexec" in mounted[path].options()


def test_a_runner_that_journaled_its_terminal_fact_is_resumed_once(
    tmp_path: Path,
) -> None:
    """A Runner that died holding the only record of what happened gets its
    own container back, re-policed, with its handoff replaced."""
    carrier = RecordingCarrier(
        _attested_document(_manifest()), [92, 0], journal_records=[True]
    )
    lease = _lease(tmp_path)
    launcher, announced = _launcher(tmp_path, carrier, OpenLeases([lease]))

    launcher.establish(lease)

    container = f"atelier2-attempt-{_LEASE_ID}-runner"
    assert carrier.started_again == [container]
    assert carrier.policed.count(container) == 2
    assert len(carrier.handoffs) == 2
    assert "runner-exit=92" in announced
    assert "journal-terminal-record=present" in announced


def test_a_runner_that_retained_nothing_fails_its_attempt(tmp_path: Path) -> None:
    """A nonzero exit with nothing to deliver is a failed Attempt: it is
    refused, and what it left is kept for the operator and the next
    reconciliation rather than swept away."""
    carrier = RecordingCarrier(
        _attested_document(_manifest()), [1], journal_records=[False]
    )
    lease = _lease(tmp_path)
    source = OpenLeases([lease])
    launcher, _announced = _launcher(tmp_path, carrier, source)

    with pytest.raises(AttemptRefusal, match="launcher-attempt-failed"):
        launcher.establish(lease)

    assert carrier.started_again == []
    assert carrier.removed["networks"] == []
    assert source.released == []


def test_a_failed_attempt_gives_the_console_back_before_it_is_reported(
    tmp_path: Path,
) -> None:
    """The console outlives every Attempt, so a failure that left it holding
    this Attempt's chains and its network would refuse the next Attempt's own
    attestation and every one after it -- while an ACCEPT rule kept pointing at
    a subnet the engine is free to hand out again. What the Attempt itself
    created stays for the operator to read."""
    carrier = RecordingCarrier(
        _attested_document(_manifest()), [1], journal_records=[False]
    )
    lease = _lease(tmp_path)
    launcher, _announced = _launcher(tmp_path, carrier, OpenLeases([lease]))

    with pytest.raises(AttemptRefusal):
        launcher.establish(lease)

    attempt_network = f"atelier2-attempt-{_LEASE_ID}"
    assert carrier.unpoliced == [(_SERVE_CONTAINER, attempt_chains(_LEASE_ID))]
    assert carrier.detached == [(_SERVE_CONTAINER, attempt_network)]
    assert carrier.removed == {"containers": [], "volumes": [], "networks": []}


def test_a_release_that_cannot_clear_the_console_still_detaches_it(
    tmp_path: Path,
) -> None:
    """The same rollback covers the last step of a good Attempt: a release that
    cannot take its chains out of the console must not leave the console on an
    Attempt network either, or the next Attempt is refused for it."""
    carrier = RecordingCarrier(_attested_document(_manifest()), [0])
    carrier.policy_removal_refusal = CarrierRefusal("carrier-attempt-policy-remains")
    lease = _lease(tmp_path)
    source = OpenLeases([lease])
    launcher, _announced = _launcher(tmp_path, carrier, source)

    with pytest.raises(CarrierRefusal):
        launcher.establish(lease)

    assert carrier.detached == [(_SERVE_CONTAINER, f"atelier2-attempt-{_LEASE_ID}")]
    assert source.released == []


def test_a_console_that_would_not_let_the_attempt_go_says_so(
    tmp_path: Path,
) -> None:
    """A console that kept something is a second fact, not a replacement for
    the Attempt's own failure: the refusal an operator has to act on is still
    the one that ended the Attempt."""
    carrier = RecordingCarrier(
        _attested_document(_manifest()), [1], journal_records=[False]
    )
    carrier.policy_removal_refusal = CarrierRefusal("carrier-attempt-policy-remains")
    lease = _lease(tmp_path)
    launcher, announced = _launcher(tmp_path, carrier, OpenLeases([lease]))

    with pytest.raises(AttemptRefusal, match="launcher-attempt-failed"):
        launcher.establish(lease)

    assert any(
        line.startswith("console-still-holds-the-attempt=") for line in announced
    )
    assert carrier.detached == [(_SERVE_CONTAINER, f"atelier2-attempt-{_LEASE_ID}")]


def test_a_lease_may_not_name_a_path_outside_the_attempt_root(tmp_path: Path) -> None:
    """A lease asks; it does not authorise. A document that named the host's own
    directories would otherwise mount them into an Attempt."""
    validation = _validation(tmp_path / "attempts")
    (tmp_path / "attempts").mkdir()
    lease = _lease(tmp_path)

    with pytest.raises(AttemptRefusal, match="outside-the-attempt-root"):
        validation.validated(lease)


def test_a_lease_may_not_name_another_console_container(tmp_path: Path) -> None:
    """Attaching a container to an Attempt network installs a packet filter in
    its namespace; naming a foreign container would aim that at a stranger."""
    validation = RunnerLeaseValidation(
        tmp_path,
        "another-console",
        _CONSOLE_NETWORK,
        _RUNNER_IMAGE,
        RunnerManifestBounds(),
    )

    with pytest.raises(AttemptRefusal, match="another-console-container"):
        validation.validated(_lease(tmp_path))


def test_a_lease_document_naming_a_foreign_path_is_never_even_read(
    tmp_path: Path,
) -> None:
    """The refusal comes before the adapter follows what the document says:
    reading a path a stranger chose is already acting on it."""
    directory = tmp_path / "leases"
    source = FileRunnerLeaseSource(directory, _validation(tmp_path / "attempts"))
    (tmp_path / "attempts").mkdir()
    (directory / "open" / f"{_LEASE_ID}.json").write_text(
        json.dumps(
            {
                "binding_path": "/etc/shadow",
                "manifest_path": str(tmp_path / "manifest"),
                "runner_image": _RUNNER_IMAGE,
                "serve_container": _SERVE_CONTAINER,
                "handoff_directory": str(tmp_path / "handoff"),
                "core_peer_directory": str(tmp_path / "peer"),
                "issuance_directory": str(tmp_path / "issuance"),
                "provider_credential_source": str(tmp_path / "credentials"),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AttemptRefusal, match="binding_path"):
        source.claim_open_lease()


def test_the_resumed_runner_is_detached_before_it_is_started_again(
    tmp_path: Path,
) -> None:
    """One order on both paths: a container is never reachable before its
    Attempt policy is in its namespace. A restart throws that namespace away,
    so the container is released from its network first and gets policy and
    network back through the same operation a first start uses."""
    carrier = RecordingCarrier(
        _attested_document(_manifest()), [92, 0], journal_records=[True]
    )
    lease = _lease(tmp_path)
    launcher, _announced = _launcher(tmp_path, carrier, OpenLeases([lease]))

    launcher.establish(lease)

    assert carrier.operations == [
        "attach",
        "start-policed",
        "handoff",
        "restart-private",
        "attach",
        "handoff",
        "remove-policy",
    ]


def test_a_failed_attempt_does_not_end_a_watching_launcher(tmp_path: Path) -> None:
    """One bad Attempt is a bad Attempt, not an outage: it is reported, its
    lease stays claimed for reconciliation, and the next lease is served."""
    carrier = RecordingCarrier(
        _attested_document(_manifest()), [1, 0], journal_records=[False]
    )
    failing = _lease(tmp_path)
    good = _lease(tmp_path / "second", lease_id=_SECOND_LEASE_ID)
    source = OpenLeases([failing, good], exhausted=SourceExhausted)
    launcher, announced = _launcher(tmp_path, carrier, source)

    with pytest.raises(SourceExhausted):
        launcher.serve_open_leases(once=False, poll_seconds=0)

    assert [line for line in announced if line.startswith("attempt-failed=")]
    assert announced.count(f"attempt-released={good.lease_id.value}") == 1
    assert source.released == [good.lease_id.value]


def test_a_minted_key_leaves_the_host_even_when_delivery_fails(
    tmp_path: Path,
) -> None:
    """A failed delivery is a failed Attempt, never a private key left behind."""
    carrier = RecordingCarrier(_attested_document(_manifest()), [])
    carrier.delivery_refusal = CarrierRefusal("carrier-command-refused: receiver")
    lease = _lease(tmp_path)
    launcher, _announced = _launcher(tmp_path, carrier, OpenLeases([lease]))

    with pytest.raises(CarrierRefusal):
        launcher.establish(lease)

    assert not (lease.issuance_directory / "client.key").exists()
    assert (lease.issuance_directory / "client.crt").is_file()


def _lease_document(root: Path, **overrides: str) -> dict[str, str]:
    _attempt_directories(root)
    manifest = _manifest()
    (root / "manifest").write_bytes(encode_runner_manifest(manifest))
    (root / "bootstrap.json").write_text(
        json.dumps(
            {
                "attempt_id": "a" * 64,
                "request_hash": "c" * 64,
                "generation_id": "generation-one",
                "manifest_id": runner_manifest_id(manifest).value,
            }
        ),
        encoding="utf-8",
    )
    return {
        "binding_path": str(root / "bootstrap.json"),
        "manifest_path": str(root / "manifest"),
        "runner_image": _RUNNER_IMAGE,
        "serve_container": _SERVE_CONTAINER,
        "handoff_directory": str(root / "handoff"),
        "core_peer_directory": str(root / "peer"),
        "issuance_directory": str(root / "issuance"),
        "provider_credential_source": str(root / "provider-credentials"),
        **overrides,
    }


@dataclass
class UntilEmpty:
    """The real file source, ending the watch once it has nothing left.

    A launcher that is working correctly polls an empty source forever, which
    is what it is for and what a test cannot wait for. Everything else -- the
    claim, the decode, the refusal -- stays the real adapter's.
    """

    source: FileRunnerLeaseSource

    def claim_open_lease(self) -> RunnerLease | None:
        lease = self.source.claim_open_lease()
        if lease is None:
            raise SourceExhausted()
        return lease

    def release(self, lease: RunnerLease) -> None:
        self.source.release(lease)

    def abandon_stale_claims(self) -> tuple[str, ...]:
        return self.source.abandon_stale_claims()


def _published(directory: Path, lease_id: str, document: str) -> None:
    (directory / "open" / f"{lease_id}.json").write_text(document, encoding="utf-8")


@pytest.mark.parametrize(
    "published",
    (
        pytest.param("{ this is not json", id="a-document-that-is-not-a-document"),
        pytest.param('{"runner_image": "x"}', id="a-document-missing-every-field"),
    ),
)
def test_a_lease_document_nobody_can_read_does_not_end_the_launcher(
    tmp_path: Path, published: str
) -> None:
    """From C-3 a Serve process writes these documents. One it got wrong has to
    cost exactly one lease -- quarantined where it was claimed -- and the next
    lease has to be served."""
    directory = tmp_path / "leases"
    source = FileRunnerLeaseSource(directory, _validation(tmp_path))
    _published(directory, _SECOND_LEASE_ID, published)
    _published(directory, _LEASE_ID, json.dumps(_lease_document(tmp_path)))
    carrier = RecordingCarrier(_attested_document(_manifest()), [0, 0])
    launcher, announced = _launcher(tmp_path, carrier, UntilEmpty(source))

    with pytest.raises(SourceExhausted):
        launcher.serve_open_leases(once=False, poll_seconds=0)

    assert any(line.startswith("lease-refused=") for line in announced)
    assert f"attempt-released={_LEASE_ID}" in announced
    assert (directory / "claimed" / f"{_SECOND_LEASE_ID}.json").is_file()


@pytest.mark.parametrize(
    "named",
    (
        pytest.param("lease,one", id="a-name-carrying-a-separator"),
        pytest.param("lease one", id="a-name-carrying-a-space"),
        pytest.param("-lease", id="a-name-that-would-read-as-a-flag"),
    ),
)
def test_a_lease_document_whose_name_is_no_lease_id_is_never_acted_on(
    tmp_path: Path, named: str
) -> None:
    """Every container, volume, label and chain name of an Attempt is built
    from this value, and they reach a Docker argument vector and the policy's
    own shell. A name outside the one lease-id form is refused before any of
    them is built (`#540` D-4)."""
    directory = tmp_path / "leases"
    source = FileRunnerLeaseSource(directory, _validation(tmp_path))
    _published(directory, named, json.dumps(_lease_document(tmp_path)))

    with pytest.raises(AttemptRefusal, match="lease-name-is-not-a-lease-id-form"):
        source.claim_open_lease()


def test_a_lease_naming_a_foreign_path_does_not_end_the_launcher(
    tmp_path: Path,
) -> None:
    """The refusal the validation raises is a refused lease like any other."""
    directory = tmp_path / "leases"
    source = FileRunnerLeaseSource(directory, _validation(tmp_path))
    _published(
        directory,
        _SECOND_LEASE_ID,
        json.dumps(_lease_document(tmp_path, handoff_directory="/etc")),
    )
    _published(directory, _LEASE_ID, json.dumps(_lease_document(tmp_path)))
    carrier = RecordingCarrier(_attested_document(_manifest()), [0, 0])
    launcher, announced = _launcher(tmp_path, carrier, UntilEmpty(source))

    with pytest.raises(SourceExhausted):
        launcher.serve_open_leases(once=False, poll_seconds=0)

    assert any(line.startswith("lease-refused=") for line in announced)
    assert f"attempt-released={_LEASE_ID}" in announced


def test_a_container_that_does_not_attest_its_manifest_fails_only_its_attempt(
    tmp_path: Path,
) -> None:
    """The attestation refuses in the manifest's own vocabulary; the launcher
    answers for it as this Attempt's failure, not as its own end."""
    softened = _attested_document(_manifest())
    softened["HostConfig"] = {**softened["HostConfig"], "ReadonlyRootfs": False}
    carrier = RecordingCarrier(softened, [])
    lease = _lease(tmp_path)
    source = OpenLeases([lease])
    launcher, announced = _launcher(tmp_path, carrier, source)

    failed = launcher.serve_open_leases(once=True, poll_seconds=0)

    assert failed == 1
    assert any("does not attest the manifest" in line for line in announced)
    assert source.released == []


def test_the_attempt_root_may_not_contain_the_authority(tmp_path: Path) -> None:
    """Everything under the attempt root is a surface a lease may ask to have
    mounted. The authority's key directory is the one thing that must never
    become one, so overlapping trees are refused at start."""
    with pytest.raises(AttemptRefusal, match="attempt-root-contains-the-authority"):
        admitted_attempt_root(tmp_path, tmp_path / "authority")

    with pytest.raises(AttemptRefusal, match="attempt-root-contains-the-authority"):
        admitted_attempt_root(tmp_path / "attempts", tmp_path)

    assert admitted_attempt_root(tmp_path / "attempts", tmp_path / "authority")


def _nothing_was_created(carrier: RecordingCarrier) -> bool:
    return carrier.created == {"containers": [], "volumes": [], "networks": []}


def _lease_naming_a_foreign_image(root: Path) -> RunnerLease:
    return _lease(root, runner_image="a-stranger/root-shell")


def _lease_whose_manifest_is_not_the_bound_one(root: Path) -> RunnerLease:
    """A manifest document beside the identity of a different manifest."""
    return _lease(root, binding=_binding(_manifest(scratch_bytes=4096)))


def _lease_asking_for_more_of_the_host_than_declared(root: Path) -> RunnerLease:
    oversized = _manifest(scratch_bytes=CANDIDATE_SCRATCH_BYTES * 4)
    return _lease(root, manifest=oversized, binding=_binding(oversized))


def _lease_asking_for_more_disk_than_declared(root: Path) -> RunnerLease:
    """The one bound the engine never sees: the journal is a durable volume the
    local driver gives no size, so the Runner keeps that capacity against its
    own manifest -- and a lease chooses the number it keeps."""
    oversized = replace(_manifest(), journal_bytes=CANDIDATE_JOURNAL_BYTES * 4)
    return _lease(root, manifest=oversized, binding=_binding(oversized))


@pytest.mark.parametrize(
    ("lease_of", "refusal"),
    (
        pytest.param(
            _lease_naming_a_foreign_image,
            "lease-names-another-runner-image",
            id="an-image-the-operator-never-declared",
        ),
        pytest.param(
            _lease_whose_manifest_is_not_the_bound_one,
            "lease-manifest-is-not-the-one-core-bound",
            id="a-manifest-that-is-not-the-one-core-bound",
        ),
        pytest.param(
            _lease_asking_for_more_of_the_host_than_declared,
            "lease-manifest-exceeds-a-declared-bound",
            id="a-manifest-asking-for-more-of-the-host-than-declared",
        ),
        pytest.param(
            _lease_asking_for_more_disk_than_declared,
            "lease-manifest-exceeds-a-declared-bound",
            id="a-manifest-asking-for-more-of-the-hosts-disk-than-declared",
        ),
    ),
)
def test_a_lease_this_host_will_not_carry_costs_no_object_at_all(
    tmp_path: Path,
    lease_of: Callable[[Path], RunnerLease],
    refusal: str,
) -> None:
    """Every fence a lease has to pass sits before the first engine call. The
    volume-owning container alone runs the lease's own image as root over the
    Attempt's volumes, so an Attempt that is refused must never reach it."""
    carrier = RecordingCarrier(_attested_document(_manifest()), [])
    launcher, _announced = _launcher(tmp_path, carrier, OpenLeases([]))

    with pytest.raises(AttemptRefusal, match=refusal):
        launcher.establish(lease_of(tmp_path))

    assert _nothing_was_created(carrier)
    assert carrier.owned == []


def test_a_manifest_within_the_declared_bounds_is_carried(tmp_path: Path) -> None:
    """The bound is the operator's, not the candidate's: a host that declares a
    larger Attempt carries the manifest a smaller one refuses."""
    manifest = _manifest(scratch_bytes=CANDIDATE_SCRATCH_BYTES * 4)
    lease = _lease(tmp_path, manifest=manifest, binding=_binding(manifest))
    bounds = RunnerManifestBounds(
        scratch_bytes=CANDIDATE_SCRATCH_BYTES * 4,
        writable_surface_bytes=CANDIDATE_SCRATCH_BYTES * 4,
    )

    assert _validation(tmp_path, bounds).validated(lease).manifest == manifest


@pytest.mark.parametrize(
    "offered",
    (
        pytest.param(b"{}", id="an-offer-without-an-invocation"),
        pytest.param(b"[1, 2]", id="an-offer-that-is-not-a-record"),
        pytest.param(b"{ not json", id="an-offer-that-is-not-a-document"),
        pytest.param(
            json.dumps({"invocation_id": "über"}).encode("utf-8"),
            id="an-invocation-a-certificate-cannot-carry",
        ),
        pytest.param(
            json.dumps({"invocation_id": "a/b"}).encode("utf-8"),
            id="an-invocation-outside-the-offered-form",
        ),
    ),
)
def test_an_unreadable_runner_offer_costs_one_attempt(
    tmp_path: Path, offered: bytes
) -> None:
    """The offer is written inside the Attempt's own Runner container, the
    least trusted process on this host. One it got wrong is one failed Attempt,
    and the launcher goes on serving the next lease -- including a value the
    shared invocation contract would accept and a URI-SAN would not, which
    reached `x509` and ended this process before it was held to the form the
    launcher really puts in a leaf."""
    carrier = RecordingCarrier(_attested_document(_manifest()), [])
    carrier.offer = offered
    lease = _lease(tmp_path)
    source = OpenLeases([lease])
    launcher, announced = _launcher(tmp_path, carrier, source)

    failed = launcher.serve_open_leases(once=True, poll_seconds=0)

    assert failed == 1
    assert any("runner-offer-unreadable" in line for line in announced)
    assert source.released == []


def test_an_identity_a_library_will_not_mint_costs_one_attempt(
    tmp_path: Path,
) -> None:
    """Minting builds a certificate out of values an Attempt's Runner and its
    lease writer chose. Whatever a library makes of one it will not accept is
    that Attempt's failure, never the end of a launcher that owes every other
    Attempt on this host its next poll."""

    class RefusingAuthority(RunnerIdentityAuthority):
        def issue_runner_identity(self, *arguments: Any, **named: Any) -> None:
            raise ValueError("a value this library will not put in a certificate")

    carrier = RecordingCarrier(_attested_document(_manifest()), [])
    lease = _lease(tmp_path)
    announced: list[str] = []
    launcher = RunnerLauncher(
        carrier,
        RefusingAuthority(tmp_path / "authority"),
        OpenLeases([lease]),
        _validation(tmp_path),
        announced.append,
    )

    failed = launcher.serve_open_leases(once=True, poll_seconds=0)

    assert failed == 1
    assert any("runner-identity-not-issuable" in line for line in announced)


@pytest.mark.parametrize(
    "published",
    (
        pytest.param(
            '{"a":' * 4_000 + "1" + "}" * 4_000,
            id="a-document-nested-past-the-parsers-own-stack",
        ),
        pytest.param(
            json.dumps({"runner_image": "x" * 70_000}),
            id="a-document-larger-than-a-launcher-will-read",
        ),
    ),
)
def test_a_lease_document_built_to_end_the_launcher_costs_one_lease(
    tmp_path: Path, published: str
) -> None:
    """From C-3 a Serve process writes these. A document that ends this process
    would take every other Attempt on the host with it, so the ways one can
    fail to be read are answered for as a whole -- not as the list of ways
    somebody thought of."""
    directory = tmp_path / "leases"
    source = FileRunnerLeaseSource(directory, _validation(tmp_path))
    _published(directory, _SECOND_LEASE_ID, published)
    _published(directory, _LEASE_ID, json.dumps(_lease_document(tmp_path)))
    carrier = RecordingCarrier(_attested_document(_manifest()), [0, 0])
    launcher, announced = _launcher(tmp_path, carrier, UntilEmpty(source))

    with pytest.raises(SourceExhausted):
        launcher.serve_open_leases(once=False, poll_seconds=0)

    assert any(line.startswith("lease-refused=") for line in announced)
    assert f"attempt-released={_LEASE_ID}" in announced


def test_a_manifest_larger_than_a_launcher_will_read_is_refused(
    tmp_path: Path,
) -> None:
    """Every file a lease names was named by the writer this launcher refuses
    to believe, so a read with no bound is that writer choosing what one claim
    costs this host in memory."""
    directory = tmp_path / "leases"
    source = FileRunnerLeaseSource(directory, _validation(tmp_path))
    document = _lease_document(tmp_path)
    (tmp_path / "manifest").write_bytes(b"\0" * 70_000)
    _published(directory, _LEASE_ID, json.dumps(document))

    with pytest.raises(AttemptRefusal, match="lease-document-exceeds-bound"):
        source.claim_open_lease()


def test_a_second_launcher_refuses_before_it_reconciles_anything(
    tmp_path: Path,
) -> None:
    """Reconciliation reads a claimed lease as abandoned, which is exactly what
    a working launcher's lease looks like. A second launcher beside a first
    would tear a live Attempt down, so it refuses by name -- before it has
    looked at a single lease."""
    directory = tmp_path / "leases"
    source = FileRunnerLeaseSource(directory, _validation(tmp_path))
    claimed = directory / "claimed" / f"{_LEASE_ID}.json"
    claimed.write_text("{}", encoding="utf-8")

    with (
        single_launcher(directory),
        pytest.raises(AttemptRefusal, match="another-launcher-holds"),
        single_launcher(directory),
    ):
        pass

    assert claimed.is_file()
    assert source.abandon_stale_claims() == (_LEASE_ID,)


def test_the_lock_a_dead_launcher_held_is_free_for_the_next_one(
    tmp_path: Path,
) -> None:
    """The claim is the kernel's, not a file whose content says who owns it, so
    a launcher that was killed leaves nothing a human has to clear."""
    directory = tmp_path / "leases"

    with single_launcher(directory):
        pass

    with single_launcher(directory):
        assert (directory / ".launcher.lock").is_file()


def test_the_runner_leaf_covers_the_attempt_span_the_manifest_declares(
    tmp_path: Path,
) -> None:
    """A Runner's identity stands for exactly as long as the Attempt it was
    minted for may run -- the same span the Runner's own session deadline uses,
    so an invocation that is over holds a key that opens nothing."""
    carrier = RecordingCarrier(_attested_document(_manifest()), [0])
    lease = _lease(tmp_path)
    launcher, _announced = _launcher(tmp_path, carrier, OpenLeases([lease]))

    launcher.establish(lease)

    leaf = x509.load_pem_x509_certificate(
        (lease.core_peer_directory / "client.crt").read_bytes()
    )
    span = timedelta(milliseconds=lease.manifest.total_attempt_milliseconds)
    assert leaf.not_valid_after_utc - datetime.now(UTC) >= span


def test_an_expired_console_identity_is_refused_by_name(tmp_path: Path) -> None:
    """Core presents this leaf to every Runner of every Attempt. An expired one
    fails deep inside each Attempt as an unreadable handshake, so the launcher
    that owns the authority says so at start and names the renewal."""
    identity = tmp_path / "console-identity"
    RunnerIdentityAuthority(tmp_path / "authority").issue_core_identity(identity)
    issued = datetime.now(UTC)

    assert admitted_console_identity(identity, issued) > issued

    with pytest.raises(AttemptRefusal, match="console-identity-expired"):
        admitted_console_identity(identity, issued + timedelta(days=365))


def test_a_console_identity_that_is_not_there_is_refused_by_name(
    tmp_path: Path,
) -> None:
    """A directory holding no leaf is a deployment mistake, not a session to
    start and watch fail."""
    with pytest.raises(AttemptRefusal, match="console-identity-unreadable"):
        admitted_console_identity(tmp_path / "nothing-here", datetime.now(UTC))


def test_issuing_the_console_identity_is_its_own_command(tmp_path: Path) -> None:
    """The console reads its identity from a directory the launcher's authority
    writes; issuing into it again is the renewal."""
    identity = tmp_path / "console-identity"

    assert (
        main(
            [
                "issue-console-identity",
                "--certificate-authority-state",
                str(tmp_path / "authority"),
                "--identity",
                str(identity),
            ]
        )
        == 0
    )

    first = admitted_console_identity(identity, datetime.now(UTC))

    assert (identity / "ca.crt").is_file()
    assert first > datetime.now(UTC)


def test_an_admitted_lease_carries_the_paths_that_were_checked(
    tmp_path: Path,
) -> None:
    """What a consumer mounts is the resolved path, never the one the document
    spelled: a symlink swapped after the check would otherwise decide what ends
    up inside the Attempt."""
    (tmp_path / "provider-credentials").mkdir(mode=0o700, parents=True)
    (tmp_path / "by-another-name").symlink_to(tmp_path / "provider-credentials")
    lease = _lease(tmp_path)

    admitted = _validation(tmp_path).validated(
        replace(lease, provider_credential_source=tmp_path / "by-another-name")
    )

    assert (
        admitted.provider_credential_source
        == (tmp_path / "provider-credentials").resolve()
    )
