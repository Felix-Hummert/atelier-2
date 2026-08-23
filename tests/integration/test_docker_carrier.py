"""What the carrier does to the engine, and what it refuses to accept back.

The engine is replaced by a recording stand-in: a real executable the carrier
addresses exactly as it addresses Docker, which writes down the argument vector
it was handed and answers the reads the carrier makes. That keeps these tests
about the carrier's own behaviour -- the order it establishes an Attempt in, the
values it hands over, and the fences it applies to what comes back -- without a
live engine.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any

import pytest

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
    VolumeMount,
    attest_runner_container,
)
from atelier2.contracts.runner_manifests import (
    CANDIDATE_CPU_PERIOD,
    CANDIDATE_WORKSPACE_BYTES,
    RunnerManifestV1,
    RunnerPathRight,
    candidate_runner_manifest,
)

_POLICY_IMAGE = "atelier2-network-policy"
_SUBNET = "10.244.7.0/24"


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


def _created_runner_container(manifest: RunnerManifestV1) -> dict[str, Any]:
    """One Runner container as the engine reports it after the carrier made it."""
    return {
        "Id": "sha256:" + "c" * 64,
        "Image": manifest.image_digest,
        "Config": {"User": f"{manifest.effective_uid}:{manifest.effective_gid}"},
        "NetworkSettings": {"Ports": {}},
        "HostConfig": {
            "NetworkMode": "attempt-network",
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


def _engine(tmp_path: Path, container: dict[str, Any] | None = None) -> Path:
    """A stand-in engine that records every call and answers the carrier's reads."""
    log = tmp_path / "engine-calls.jsonl"
    answers = tmp_path / "engine-container.json"
    answers.write_text(
        json.dumps(container if container is not None else {"Id": "container-id"}),
        encoding="utf-8",
    )
    executable = tmp_path / "engine"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"log = open({str(log)!r}, 'a', encoding='utf-8')\n"
        "log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "log.close()\n"
        "if sys.argv[1:3] == ['container', 'inspect']:\n"
        f"    print(json.dumps([json.load(open({str(answers)!r}))]))\n"
        "elif sys.argv[1:3] == ['network', 'inspect']:\n"
        f"    print(json.dumps([{{'IPAM': {{'Config': [{{'Subnet': {_SUBNET!r}}}]}}}}]))\n"
        "elif sys.argv[1:2] == ['wait']:\n"
        "    print('0')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable


def _calls(tmp_path: Path) -> list[list[str]]:
    return [
        json.loads(line)
        for line in (tmp_path / "engine-calls.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]


def _spec(
    name: str = "attempt-runner", binds: tuple[BindMount, ...] = ()
) -> ContainerSpec:
    return ContainerSpec(
        name,
        "atelier2-runner",
        "atelier2.runner-lease=one",
        ContainerHardening(user="10001:10001", read_only_root=True),
        tmpfs=(TmpfsMount(PurePosixPath("/tmp"), 1024, 0o1777),),
        binds=binds,
        volumes=(
            VolumeMount(
                "journal-one", PurePosixPath("/journal"), MountRight.READ_WRITE
            ),
        ),
    )


def test_a_container_is_private_then_policed_and_only_then_connected(
    tmp_path: Path,
) -> None:
    """The order is the fence: a container that is connected before its Attempt
    policy is installed would run for exactly as many unfiltered packets as that
    window is wide."""
    carrier = DockerCarrier(_POLICY_IMAGE, _engine(tmp_path))

    carrier.start_policed_container(
        _spec(), AttemptNetwork("attempt-network", _SUBNET), ContainerRole.RUNNER
    )

    verbs = [(call[0], call[1]) for call in _calls(tmp_path)]
    assert verbs == [
        ("run", "-d"),
        ("run", "--rm"),
        ("network", "disconnect"),
        ("network", "connect"),
        ("container", "inspect"),
    ]
    created, policed = _calls(tmp_path)[0], _calls(tmp_path)[1]
    assert created[created.index("--network") + 1] == "none"
    assert policed[policed.index("--network") + 1] == "container:attempt-runner"


def test_an_attempt_value_reaches_the_engine_as_one_argument(tmp_path: Path) -> None:
    """No host shell parses what the carrier builds, so a name that reads like a
    second command is still just a name."""
    carrier = DockerCarrier(_POLICY_IMAGE, _engine(tmp_path))
    hostile = "attempt; rm -rf /"

    carrier.start_policed_container(
        _spec(hostile), AttemptNetwork("attempt-network", _SUBNET), ContainerRole.CORE
    )

    created = _calls(tmp_path)[0]
    assert created[created.index("--name") + 1] == hostile


def test_a_bind_the_engine_made_writable_is_refused(tmp_path: Path) -> None:
    """A read-only bind that came back writable is a host surface nobody
    decided to open, so the carrier refuses the container it just created."""
    document = {
        "Id": "container-id",
        "Mounts": [
            {"Destination": "/handoff", "RW": True, "Type": "bind"},
        ],
    }
    carrier = DockerCarrier(_POLICY_IMAGE, _engine(tmp_path, document))
    spec = _spec(
        binds=(
            BindMount(
                Path("/tmp/handoff"), PurePosixPath("/handoff"), MountRight.READ_ONLY
            ),
        )
    )

    with pytest.raises(CarrierRefusal, match="carrier-mount-right-differs"):
        carrier.start_policed_container(
            spec, AttemptNetwork("attempt-network", _SUBNET), ContainerRole.CORE
        )


def test_a_refused_engine_call_carries_what_the_engine_said(tmp_path: Path) -> None:
    """A carrier operation never half-succeeds quietly: the engine's own
    sentence is what the caller is refused with."""
    engine = tmp_path / "engine"
    engine.write_text("#!/bin/sh\necho 'no such container' >&2\nexit 1\n")
    engine.chmod(0o755)

    with pytest.raises(CarrierRefusal, match="no such container"):
        DockerCarrier(_POLICY_IMAGE, engine).wait_for_exit("attempt-runner")


def test_the_attested_container_carries_the_manifest_identity() -> None:
    manifest = _manifest()

    assert attest_runner_container(_created_runner_container(manifest), manifest)


@pytest.mark.parametrize(
    "opened",
    (
        pytest.param(
            {"HostConfig": {"PortBindings": {"8443/tcp": [{"HostPort": "8443"}]}}},
            id="a-port-was-published",
        ),
        pytest.param(
            {"NetworkSettings": {"Ports": {"8443/tcp": [{"HostPort": "8443"}]}}},
            id="a-published-port-is-live",
        ),
        pytest.param(
            {"HostConfig": {"NetworkMode": "host"}},
            id="the-host-namespace-was-taken",
        ),
    ),
)
def test_a_way_into_the_attempt_from_the_host_is_refused(
    opened: dict[str, Any],
) -> None:
    """An Attempt's only inbound opening is Core's session port inside the
    Attempt's own subnet. A published port puts the Runner on an address the
    host's neighbours can reach, and the host network namespace would leave the
    Attempt policy filtering the host itself."""
    manifest = _manifest()
    document = _created_runner_container(manifest)
    for section, values in opened.items():
        document[section] = {**document[section], **values}

    with pytest.raises(ValueError, match="runner-attestation-mismatch"):
        attest_runner_container(document, manifest)
