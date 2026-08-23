"""What the launcher does with a lease, and what it leaves on the host.

The engine is replaced by a recording stand-in that satisfies the launcher's
own `AttemptCarrier` surface -- the whole privilege the launcher role holds --
so these tests read as what the launcher asked the host to do. The certificate
authority is real: identity is the one thing a fake would stop proving.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from atelier2.adapters.docker_carrier import (
    AttemptNetwork,
    CarrierRefusal,
    ContainerRole,
    ContainerSpec,
    MountRight,
    TmpfsVolumeOptions,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.contracts.runner_manifests import (
    CANDIDATE_CPU_PERIOD,
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
    runner_container_spec,
)

_RUNNER_IMAGE = "atelier2-runner-candidate"
_SERVE_CONTAINER = "atelier2-console"
_SUBNET = "10.244.9.0/24"
_INVOCATION = "invocation-one"
_LEASE_ID = "lease-one"


def _manifest() -> RunnerManifestV1:
    return candidate_runner_manifest(
        source_commit="a" * 40,
        image_digest="sha256:" + "b" * 64,
        required_landlock_abi=1,
        executor_revision="fake-free/v1",
        executor_operational_identity="free-runner-candidate",
        provider_id="fake-free",
        auth_mode="api_key",
        requested_capability="headless",
    )


def _binding() -> RunnerGenerationBinding:
    manifest = _manifest()
    return RunnerGenerationBinding(
        AgentAttemptId("a" * 64),
        AgentExecutionRequestHash("c" * 64),
        RunnerGenerationId("generation-one"),
        runner_manifest_id(manifest),
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
    journal_records: list[bool] = field(default_factory=list)
    delivery_refusal: Exception | None = None
    created: dict[str, list[str]] = field(
        default_factory=lambda: {"containers": [], "volumes": [], "networks": []}
    )
    removed: dict[str, list[str]] = field(
        default_factory=lambda: {"containers": [], "volumes": [], "networks": []}
    )
    attached: list[tuple[str, str]] = field(default_factory=list)
    policed: list[str] = field(default_factory=list)
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
        self, container: str, network: AttemptNetwork, role: ContainerRole
    ) -> None:
        self.operations.append("attach")
        self.policed.append(container)
        self.attached.append((container, network.name))

    def install_attempt_policy(
        self, container: str, subnet: str, role: ContainerRole
    ) -> None:
        self.policed.append(container)

    def create_volume(
        self, name: str, label: str, tmpfs: TmpfsVolumeOptions | None = None
    ) -> None:
        self.created["volumes"].append(name)

    def own_volume(self, name: str, image: str, uid: int, gid: int) -> None:
        self.owned.append(name)

    def start_policed_container(
        self, spec: ContainerSpec, network: AttemptNetwork, role: ContainerRole
    ) -> str:
        self.operations.append("start-policed")
        self.runner_specification = spec
        self.created["containers"].append(spec.name)
        self.policed.append(spec.name)
        self.attached.append((spec.name, network.name))
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
        return json.dumps({"invocation_id": _INVOCATION}).encode("utf-8")

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


def _lease(
    root: Path, manifest: RunnerManifestV1 | None = None, lease_id: str = _LEASE_ID
) -> RunnerLease:
    for name in ("handoff", "peer", "issuance", "provider-credentials"):
        (root / name).mkdir(mode=0o700, parents=True, exist_ok=True)
    return RunnerLease(
        lease_id,
        _binding(),
        _RUNNER_IMAGE,
        manifest if manifest is not None else _manifest(),
        _SERVE_CONTAINER,
        root / "handoff",
        root / "peer",
        root / "issuance",
        root / "provider-credentials",
    )


def _validation(root: Path) -> RunnerLeaseValidation:
    return RunnerLeaseValidation(root, _SERVE_CONTAINER)


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
        self.released.append(lease.lease_id)

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
    assert claimed.lease_id == _LEASE_ID
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
    it left on the host is removed, and the lease goes back to its owner."""
    directory = tmp_path / "leases"
    FileRunnerLeaseSource(directory, _validation(tmp_path))
    (directory / "claimed" / "interrupted.json").write_text("{}", encoding="utf-8")
    carrier = RecordingCarrier(_attested_document(_manifest()), [])
    carrier.created["containers"].append("atelier2-attempt-interrupted-runner")
    carrier.created["volumes"].append("atelier2-attempt-interrupted-journal")
    carrier.created["networks"].append("atelier2-attempt-interrupted")
    launcher, announced = _launcher(
        tmp_path, carrier, FileRunnerLeaseSource(directory, _validation(tmp_path))
    )

    launcher.reconcile_abandoned_attempts()

    assert "reconciled-attempt=interrupted" in announced
    assert carrier.removed["containers"] == ["atelier2-attempt-interrupted-runner"]
    assert carrier.removed["volumes"] == ["atelier2-attempt-interrupted-journal"]
    assert carrier.removed["networks"] == ["atelier2-attempt-interrupted"]
    assert (directory / "released" / "interrupted.json").is_file()


def test_an_established_attempt_leaves_nothing_behind(tmp_path: Path) -> None:
    """The whole ensemble, and then a host that looks as it did before."""
    carrier = RecordingCarrier(_attested_document(_manifest()), [0])
    lease = _lease(tmp_path)
    source = OpenLeases([lease])
    launcher, announced = _launcher(tmp_path, carrier, source)

    launcher.establish(lease)

    assert (_SERVE_CONTAINER, f"atelier2-attempt-{_LEASE_ID}") in carrier.attached
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
    validation = RunnerLeaseValidation(tmp_path, "another-console")

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
    ]


def test_a_failed_attempt_does_not_end_a_watching_launcher(tmp_path: Path) -> None:
    """One bad Attempt is a bad Attempt, not an outage: it is reported, its
    lease stays claimed for reconciliation, and the next lease is served."""
    carrier = RecordingCarrier(
        _attested_document(_manifest()), [1, 0], journal_records=[False]
    )
    failing = _lease(tmp_path)
    good = _lease(tmp_path / "second", lease_id="lease-two")
    source = OpenLeases([failing, good], exhausted=SourceExhausted)
    launcher, announced = _launcher(tmp_path, carrier, source)

    with pytest.raises(SourceExhausted):
        launcher.serve_open_leases(once=False, poll_seconds=0)

    assert [line for line in announced if line.startswith("attempt-failed=")]
    assert announced.count(f"attempt-released={good.lease_id}") == 1
    assert source.released == [good.lease_id]


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
