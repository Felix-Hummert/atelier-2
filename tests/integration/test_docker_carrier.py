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
    VolumeMount,
    attempt_chains,
    attest_runner_container,
)
from atelier2.adapters.runner_tls import CORE_SESSION_PORT
from atelier2.contracts.runner_manifests import (
    CANDIDATE_CPU_PERIOD,
    CANDIDATE_WORKSPACE_BYTES,
    RunnerManifestV1,
    RunnerPathRight,
    candidate_runner_manifest,
)

_POLICY_IMAGE = "atelier2-network-policy"
_SUBNET = "10.244.7.0/24"
_BASE_SUBNET = "172.31.5.0/24"
_ATTEMPT_NETWORK = "attempt-network"
_CONSOLE_NETWORK = "console-network"
_LEASE_ID = "a1b2c3d4e5f6" + "0" * 52


def _attachment(
    role: ContainerRole = ContainerRole.RUNNER, base_network: str | None = None
) -> AttemptAttachment:
    return AttemptAttachment(
        attempt_chains(_LEASE_ID),
        AttemptNetwork(_ATTEMPT_NETWORK, _SUBNET),
        role,
        base_network,
    )


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


def _attached_container(*networks: str, running: bool = True) -> dict[str, Any]:
    """A container as the engine reports it: attached to the networks named, or
    -- while it is being restarted -- to none at all."""
    return {
        "Id": "container-id",
        "State": {"Running": running},
        "HostConfig": {"NetworkMode": "none"},
        "NetworkSettings": {"Networks": {network: {} for network in networks}},
    }


def _engine(tmp_path: Path, *containers: dict[str, Any]) -> Path:
    """A stand-in engine that records every call and answers the carrier's reads.

    Each container inspect is answered with the next document given, and the
    last one keeps answering, so a test can state a container that changes
    between two reads -- attached, and then released.

    It keeps the one engine rule the carrier's own failure handling is written
    against: a namespace can only be joined while its container runs.
    """
    log = tmp_path / "engine-calls.jsonl"
    answers = tmp_path / "engine-containers.json"
    answers.write_text(
        json.dumps(list(containers) or [_attached_container(_ATTEMPT_NETWORK)]),
        encoding="utf-8",
    )
    subnets = {_ATTEMPT_NETWORK: _SUBNET, _CONSOLE_NETWORK: _BASE_SUBNET}
    executable = tmp_path / "engine"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"log = open({str(log)!r}, 'a', encoding='utf-8')\n"
        "log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "log.close()\n"
        "if sys.argv[1:2] == ['run'] and any(\n"
        "    argument.startswith('container:') for argument in sys.argv\n"
        "):\n"
        f"    documents = json.load(open({str(answers)!r}))\n"
        "    if not documents[0].get('State', {}).get('Running', True):\n"
        "        print('cannot join network namespace', file=sys.stderr)\n"
        "        raise SystemExit(125)\n"
        "elif sys.argv[1:3] == ['container', 'inspect']:\n"
        f"    documents = json.load(open({str(answers)!r}))\n"
        "    print(json.dumps([documents[0]]))\n"
        f"    open({str(answers)!r}, 'w').write(json.dumps(documents[1:] or documents))\n"
        "elif sys.argv[1:3] == ['network', 'inspect']:\n"
        f"    subnet = {subnets!r}[sys.argv[3]]\n"
        "    print(json.dumps([{'IPAM': {'Config': [{'Subnet': subnet}]}}]))\n"
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

    carrier.start_policed_container(_spec(), _attachment())

    verbs = [(call[0], call[1]) for call in _calls(tmp_path)]
    assert verbs == [
        ("run", "-d"),
        ("container", "inspect"),
        ("run", "--rm"),
        ("network", "disconnect"),
        ("network", "connect"),
        ("container", "inspect"),
    ]
    created, policed = _calls(tmp_path)[0], _calls(tmp_path)[2]
    assert created[created.index("--network") + 1] == "none"
    assert policed[policed.index("--network") + 1] == "container:attempt-runner"


def test_an_attempt_value_reaches_the_engine_as_one_argument(tmp_path: Path) -> None:
    """No host shell parses what the carrier builds, so a name that reads like a
    second command is still just a name."""
    carrier = DockerCarrier(_POLICY_IMAGE, _engine(tmp_path))
    hostile = "attempt; rm -rf /"

    carrier.start_policed_container(_spec(hostile), _attachment())

    created = _calls(tmp_path)[0]
    assert created[created.index("--name") + 1] == hostile


def test_a_bind_the_engine_made_writable_is_refused(tmp_path: Path) -> None:
    """A read-only bind that came back writable is a host surface nobody
    decided to open, so the carrier refuses the container it just created."""
    document = {
        **_attached_container(_ATTEMPT_NETWORK),
        "Mounts": [{"Destination": "/handoff", "RW": True, "Type": "bind"}],
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
        carrier.start_policed_container(spec, _attachment())


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


@pytest.mark.parametrize(
    "reported",
    (
        pytest.param(
            {"NetworkSettings": {"Ports": {"8443/tcp": None}}},
            id="a-port-is-exposed-and-bound-to-nothing",
        ),
        pytest.param(
            {"HostConfig": {"PortBindings": None}},
            id="the-engine-reports-no-bindings-as-null",
        ),
    ),
)
def test_an_absence_of_bindings_is_an_absence(reported: dict[str, Any]) -> None:
    """The engine spells "nothing is published" in more than one way, and every
    one of them has to read as nothing published -- otherwise this fence would
    refuse the very containers the carrier creates."""
    manifest = _manifest()
    document = _created_runner_container(manifest)
    for section, values in reported.items():
        document[section] = {**document[section], **values}

    assert attest_runner_container(document, manifest)


@pytest.mark.parametrize(
    ("attached", "expected"),
    (
        pytest.param(
            _ATTEMPT_NETWORK,
            [
                ("container", "inspect"),
                ("network", "disconnect"),
                ("start", "attempt-runner"),
                ("container", "inspect"),
            ],
            id="the-exited-container-is-still-on-its-attempt-network",
        ),
        pytest.param(
            None,
            [
                ("container", "inspect"),
                ("start", "attempt-runner"),
                ("container", "inspect"),
            ],
            id="the-exited-container-is-already-attached-to-nothing",
        ),
    ),
)
def test_a_restarted_container_is_detached_started_and_only_then_policed(
    tmp_path: Path, attached: str | None, expected: list[tuple[str, str]]
) -> None:
    """A restart throws away the namespace its Attempt policy lived in, so a
    container that came back attached would run unfiltered until the policy was
    reinstalled. Whatever it is still attached to is released first, and what
    comes up is read back as reachable by nothing -- so policy and network
    arrive afterwards, in the same order a first start has."""
    carrier = DockerCarrier(
        _POLICY_IMAGE,
        _engine(
            tmp_path,
            _attached_container(*([attached] if attached else [])),
            _attached_container(),
        ),
    )

    carrier.restart_private_container("attempt-runner")

    assert [(call[0], call[1]) for call in _calls(tmp_path)] == expected
    if attached is not None:
        released = _calls(tmp_path)[1]
        assert released[2:] == [attached, "attempt-runner"]


def test_a_container_that_came_back_attached_is_refused(tmp_path: Path) -> None:
    """The order is only a guarantee if it is read back: a restart that
    reattached the container is refused rather than policed afterwards."""
    carrier = DockerCarrier(
        _POLICY_IMAGE, _engine(tmp_path, _attached_container(_ATTEMPT_NETWORK))
    )

    with pytest.raises(CarrierRefusal, match="carrier-attachment-differs"):
        carrier.restart_private_container("attempt-runner")


def _policy_program(tmp_path: Path, call: int = 0) -> str:
    """The one program text the carrier renders, as the engine received it."""
    policy_calls = [
        arguments
        for arguments in _calls(tmp_path)
        if arguments[0] == "run" and "sh" in arguments
    ]
    return policy_calls[call][-1]


def test_the_console_keeps_its_declared_base_network_and_gains_one_attempt(
    tmp_path: Path,
) -> None:
    """A long-lived console serves the cockpit on the network the deployment
    started it on and is attached to one Attempt besides. The attestation says
    exactly that, positively: those two networks, and nothing else."""
    carrier = DockerCarrier(
        _POLICY_IMAGE,
        _engine(tmp_path, _attached_container(_CONSOLE_NETWORK, _ATTEMPT_NETWORK)),
    )

    carrier.attach_policed_container(
        "console", _attachment(ContainerRole.CORE, _CONSOLE_NETWORK)
    )

    program = _policy_program(tmp_path)
    assert f"iptables -A INPUT -s {_BASE_SUBNET} -j ACCEPT" in program
    assert f"iptables -A OUTPUT -d {_BASE_SUBNET} -j ACCEPT" in program


def test_a_console_attached_to_a_network_nobody_declared_is_refused(
    tmp_path: Path,
) -> None:
    """A second network nobody's policy speaks about is reach nobody granted,
    so it is refused where it is read rather than discovered by an Attempt."""
    carrier = DockerCarrier(
        _POLICY_IMAGE,
        _engine(
            tmp_path,
            _attached_container(_CONSOLE_NETWORK, _ATTEMPT_NETWORK, "a-third"),
        ),
    )

    with pytest.raises(CarrierRefusal, match="carrier-attachment-differs"):
        carrier.attach_policed_container(
            "console", _attachment(ContainerRole.CORE, _CONSOLE_NETWORK)
        )


def test_an_attempt_installs_its_own_chains_and_leaves_the_base_policy_alone(
    tmp_path: Path,
) -> None:
    """Appending an Attempt's rules to a namespace's own chains would put the
    next Attempt's behind the first Attempt's ACCEPTs and widen both. Each
    Attempt therefore owns a pair of chains, reached from a dispatch chain the
    base policy installs once and keeps."""
    carrier = DockerCarrier(
        _POLICY_IMAGE,
        _engine(tmp_path, _attached_container(_CONSOLE_NETWORK, _ATTEMPT_NETWORK)),
    )
    chains = attempt_chains(_LEASE_ID)

    carrier.attach_policed_container(
        "console", _attachment(ContainerRole.CORE, _CONSOLE_NETWORK)
    )

    program = _policy_program(tmp_path)
    base, attempt = program.split("\nfi\n")
    assert f"iptables -N {chains.inbound}" in attempt
    assert f"iptables -N {chains.outbound}" in attempt
    assert (
        f"iptables -A {chains.inbound} -s {_SUBNET} "
        f"-p tcp --dport {CORE_SESSION_PORT} -j ACCEPT" in attempt
    )
    assert f"iptables -A ATELIER2-ATTEMPTS-IN -j {chains.inbound}" in attempt
    assert "ATELIER2-ATTEMPTS-IN" in base
    assert chains.inbound not in base


def test_the_runner_gets_no_inbound_grant_from_its_attempt(tmp_path: Path) -> None:
    """The Runner dials out and its answers return as established connections,
    so its Attempt chain grants it nothing inbound at all."""
    carrier = DockerCarrier(_POLICY_IMAGE, _engine(tmp_path))
    chains = attempt_chains(_LEASE_ID)

    carrier.install_attempt_policy("attempt-runner", _attachment())

    program = _policy_program(tmp_path)
    assert f"iptables -A {chains.outbound} -d {_SUBNET} -j ACCEPT" in program
    assert f"iptables -A {chains.inbound} -" not in program


def test_releasing_an_attempt_takes_its_chains_out_and_proves_it(
    tmp_path: Path,
) -> None:
    """The console outlives the Attempt, so what it grants afterwards has to be
    what it granted before. A namespace still naming this Attempt's chains is
    a stale grant for a subnet that no longer exists, and refuses."""
    carrier = DockerCarrier(_POLICY_IMAGE, _engine(tmp_path))
    chains = attempt_chains(_LEASE_ID)

    carrier.remove_attempt_policy("console", chains)

    program = _policy_program(tmp_path)
    for chain in (chains.inbound, chains.outbound):
        assert f"iptables -X {chain}" in program
    assert f"iptables -S | grep -q -e {chains.outbound} -e {chains.inbound}" in program
    assert "carrier-attempt-policy-remains" in program


def test_a_console_that_stopped_has_no_rule_left_to_take_out(
    tmp_path: Path,
) -> None:
    """A network namespace exists only while its container runs, so a console
    that stopped took every rule of every Attempt with it. That is decided by
    reading the container's state back, not by what the engine said."""
    carrier = DockerCarrier(
        _POLICY_IMAGE, _engine(tmp_path, _attached_container(running=False))
    )

    carrier.remove_attempt_policy("console", attempt_chains(_LEASE_ID))

    assert [call[0] for call in _calls(tmp_path)] == ["run", "container"]


def test_a_running_console_that_kept_a_released_attempts_rules_refuses(
    tmp_path: Path,
) -> None:
    """A grant for a subnet that no longer exists is residue nobody decided to
    leave, so a console that is up and still names the Attempt is loud."""
    engine = tmp_path / "engine"
    engine.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "if sys.argv[1:3] == ['container', 'inspect']:\n"
        "    print(json.dumps([{'State': {'Running': True}}]))\n"
        "else:\n"
        "    print('carrier-attempt-policy-remains', file=sys.stderr)\n"
        "    raise SystemExit(3)\n",
        encoding="utf-8",
    )
    engine.chmod(0o755)
    carrier = DockerCarrier(_POLICY_IMAGE, engine)

    with pytest.raises(CarrierRefusal, match="carrier-attempt-policy-remains"):
        carrier.remove_attempt_policy("console", attempt_chains(_LEASE_ID))


@pytest.mark.parametrize(
    ("role", "base_network"),
    (
        pytest.param(ContainerRole.CORE, None, id="a-console-without-a-base-network"),
        pytest.param(
            ContainerRole.RUNNER, _CONSOLE_NETWORK, id="a-runner-with-a-base-network"
        ),
    ),
)
def test_only_a_console_keeps_a_network_besides_its_attempt(
    role: ContainerRole, base_network: str | None
) -> None:
    """The base policy is written against the base network's subnet and the
    attestation against its name, so a console without one would speak about a
    network nobody named -- and a Runner with one would be reachable outside
    the Attempt it exists for."""
    with pytest.raises(ValueError, match="carrier-base-network-differs"):
        AttemptAttachment(
            attempt_chains(_LEASE_ID),
            AttemptNetwork(_ATTEMPT_NETWORK, _SUBNET),
            role,
            base_network,
        )


@pytest.mark.parametrize(
    "named",
    (
        pytest.param("attempt; rm -rf /", id="a-name-that-reads-as-a-second-command"),
        pytest.param("a" * 29, id="a-name-longer-than-a-chain-name"),
        pytest.param("attempt chain", id="a-name-carrying-a-space"),
    ),
)
def test_a_chain_name_the_policy_shell_could_read_as_more_is_refused(
    named: str,
) -> None:
    """Chain names are the one caller-supplied value that reaches the policy
    program's own `sh`, so the form is refused at construction rather than
    escaped at render time."""
    with pytest.raises(ValueError, match="carrier-chain-name-refused"):
        AttemptChains(named, "ATELIER2-A-OUT")
