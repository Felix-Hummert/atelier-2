from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from atelier2.adapters.docker_carrier import DockerCarrier
from atelier2.contracts.agent_attempts import RunnerInvocationId
from atelier2.contracts.runner_leases import RunnerLeaseId, lease_label
from tests.witness.runner_candidate_core import (
    _CHILD_OBSERVATION_FIELDS,
    _CHILD_PHASES,
    _CORE_STARTED_CHILD_RECORD,
    _CORE_STARTED_CUT_EVENT,
    _CORE_STARTED_CUT_EXIT_CODE,
    _CORE_STARTED_CUT_FENCED,
    _CORE_STARTED_CUT_FENCED_EVENT,
    _CORE_STARTED_CUT_RECORD_FIELDS,
    _CORE_STARTED_CUT_REQUEST,
    _CORE_WITNESS_BINDING_FIELDS,
    _SCENARIO_CORE_RESTART,
    _WITNESS_RECORD_FAMILY,
    _bootstrap,
    _read_witness_document,
    _require_exact_string_fields,
    _wait_for_reconnected_child_observation,
    _write_core_started_cut,
    _write_reconnected_core_started,
)
from tests.witness.runner_candidate_issuer import write_candidate_manifest

CORE_STARTED_CUT_EXIT_CODE = _CORE_STARTED_CUT_EXIT_CODE
_INVOCATION = RunnerInvocationId("runner-core-reconnect-invocation")


@dataclass(frozen=True, slots=True)
class ChildObservation:
    runner_container_id: str
    runner_process_id: str
    provider_child_pid: str
    provider_child_start_time_ticks: str
    provider_child_state: str
    provider_child_count: str
    runner_cgroup_pids_current: str
    runner_cgroup_pids_limit: str
    runner_cgroup_limit_hit_count: str


def _atomic_json(path: Path, document: object) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _process_identity(stat_path: Path) -> tuple[str, str]:
    try:
        stat = stat_path.read_text(encoding="ascii")
    except FileNotFoundError as error:
        raise RuntimeError("runner-core-reconnect-child-gone") from error
    closing_parenthesis = stat.rfind(")")
    fields_after_command = stat[closing_parenthesis + 2 :].split()
    if closing_parenthesis < 0 or len(fields_after_command) <= 19:
        raise RuntimeError("runner-core-reconnect-child-identity-unreadable")
    return fields_after_command[0], fields_after_command[19]


def observe_provider_child(
    runner_container_id: str,
    runner_process_id: int,
    runner_cgroup_pids_current: str,
    runner_cgroup_pids_limit: str,
    runner_cgroup_limit_hit_count: str,
    proc_root: Path = Path("/proc"),
) -> ChildObservation:
    try:
        current_process_count = int(runner_cgroup_pids_current)
        process_limit = int(runner_cgroup_pids_limit)
        limit_hit_count = int(runner_cgroup_limit_hit_count)
    except ValueError as error:
        raise RuntimeError(
            "runner-core-reconnect-cgroup-evidence-unreadable"
        ) from error
    if (
        current_process_count < 1
        or process_limit != current_process_count
        or limit_hit_count < 0
    ):
        raise RuntimeError("runner-core-reconnect-cgroup-evidence-unreadable")
    children_path = (
        proc_root
        / str(runner_process_id)
        / "task"
        / str(runner_process_id)
        / "children"
    )
    try:
        children = children_path.read_text(encoding="ascii").split()
    except FileNotFoundError as error:
        raise RuntimeError("runner-core-reconnect-child-gone") from error
    if not children:
        raise RuntimeError("runner-core-reconnect-child-gone")
    if len(children) != 1:
        raise RuntimeError("runner-core-reconnect-duplicate-provider-processes")
    child_pid = children[0]
    state, start_time_ticks = _process_identity(proc_root / child_pid / "stat")
    if state == "Z":
        raise RuntimeError("runner-core-reconnect-child-zombie")
    return ChildObservation(
        runner_container_id,
        str(runner_process_id),
        child_pid,
        start_time_ticks,
        state,
        "1",
        runner_cgroup_pids_current,
        runner_cgroup_pids_limit,
        runner_cgroup_limit_hit_count,
    )


def require_claimed_lease(lease_directory: Path, lease_id: str) -> None:
    claimed = lease_directory / "claimed" / f"{lease_id}.json"
    foreign_states = (
        lease_directory / state / f"{lease_id}.json"
        for state in ("open", "released", "withdrawn")
    )
    if not claimed.is_file() or any(path.exists() for path in foreign_states):
        raise RuntimeError("runner-core-reconnect-lease-not-claimed")


def record_child_observation(
    output: Path,
    phase: str,
    observation: ChildObservation,
    binding: Mapping[str, str],
) -> None:
    if phase not in _CHILD_PHASES:
        raise ValueError("runner-core-reconnect-child-phase-unknown")
    if set(binding) != set(_CORE_WITNESS_BINDING_FIELDS) or any(
        not binding[field] for field in _CORE_WITNESS_BINDING_FIELDS
    ):
        raise ValueError("runner-core-reconnect-witness-binding-mismatch")
    document: dict[str, Any]
    if output.exists():
        try:
            loaded = json.loads(output.read_text(encoding="utf-8"))
        except Exception as error:
            raise RuntimeError(
                "runner-core-reconnect-child-witness-malformed"
            ) from error
        if not isinstance(loaded, dict):
            raise RuntimeError("runner-core-reconnect-child-witness-malformed")
        document = loaded
    else:
        document = {
            "record_family": _WITNESS_RECORD_FAMILY,
            **binding,
            "observations": {},
        }
    observations = document.get("observations")
    expected_existing_phases = (
        set() if phase == _CHILD_PHASES[0] else {_CHILD_PHASES[0]}
    )
    if (
        set(document)
        != {"record_family", *_CORE_WITNESS_BINDING_FIELDS, "observations"}
        or document.get("record_family") != _WITNESS_RECORD_FAMILY
        or any(document.get(field) != binding[field] for field in binding)
        or not isinstance(observations, dict)
        or set(observations) != expected_existing_phases
    ):
        raise RuntimeError("runner-core-reconnect-child-witness-malformed")
    if observations:
        first = next(iter(observations.values()))
        if not isinstance(first, dict) or set(first) != set(_CHILD_OBSERVATION_FIELDS):
            raise RuntimeError("runner-core-reconnect-child-witness-malformed")
    observations[phase] = asdict(observation)
    _atomic_json(output, document)


def _core_witness_binding(path: Path) -> dict[str, str]:
    document = _read_witness_document(
        path,
        "runner-core-reconnect-started-cut-missing",
        "runner-core-reconnect-started-cut-malformed",
    )
    _require_exact_string_fields(
        document,
        _CORE_STARTED_CUT_RECORD_FIELDS,
        "runner-core-reconnect-started-cut-malformed",
    )
    if document["record_family"] != _WITNESS_RECORD_FAMILY:
        raise RuntimeError("runner-core-reconnect-started-cut-malformed")
    return {field: cast(str, document[field]) for field in _CORE_WITNESS_BINDING_FIELDS}


def _read_cgroup_count(path: Path, minimum: int = 1) -> str:
    try:
        value = path.read_text(encoding="ascii").strip()
        if int(value) < minimum:
            raise ValueError("cgroup count is below its minimum")
    except (OSError, UnicodeDecodeError, ValueError) as error:
        raise RuntimeError(
            "runner-core-reconnect-cgroup-evidence-unreadable"
        ) from error
    return value


def _read_cgroup_limit(path: Path) -> str:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError(
            "runner-core-reconnect-cgroup-evidence-unreadable"
        ) from error
    if value == "max":
        return value
    return _read_cgroup_count_value(value, minimum=1)


def _runner_cgroup_process_evidence(
    runner_process_id: int,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> tuple[str, str, str]:
    try:
        cgroup_lines = (
            (proc_root / str(runner_process_id) / "cgroup")
            .read_text(encoding="ascii")
            .splitlines()
        )
    except (OSError, UnicodeDecodeError) as error:
        raise RuntimeError(
            "runner-core-reconnect-cgroup-evidence-unreadable"
        ) from error
    unified_paths = [
        line.partition("::")[2]
        for line in cgroup_lines
        if line.startswith("0::") and line.partition("::")[2]
    ]
    if len(unified_paths) != 1:
        raise RuntimeError("runner-core-reconnect-cgroup-evidence-unavailable")
    relative_parts = Path(unified_paths[0]).parts[1:]
    if not relative_parts or ".." in relative_parts:
        raise RuntimeError("runner-core-reconnect-cgroup-evidence-unreadable")
    cgroup = cgroup_root.joinpath(*relative_parts)
    try:
        events = dict(
            line.split(maxsplit=1)
            for line in (cgroup / "pids.events")
            .read_text(encoding="ascii")
            .splitlines()
        )
        limit_hit_count = events["max"]
    except (OSError, UnicodeDecodeError, KeyError, ValueError) as error:
        raise RuntimeError(
            "runner-core-reconnect-cgroup-evidence-unreadable"
        ) from error
    return (
        _read_cgroup_count(cgroup / "pids.current"),
        _read_cgroup_limit(cgroup / "pids.max"),
        _read_cgroup_count_value(limit_hit_count, minimum=0),
    )


def _read_cgroup_count_value(value: str, minimum: int) -> str:
    try:
        if int(value) < minimum:
            raise ValueError("cgroup count is below its minimum")
    except ValueError as error:
        raise RuntimeError(
            "runner-core-reconnect-cgroup-evidence-unreadable"
        ) from error
    return value


def _enforce_runner_process_ceiling(
    policy_image: str, container: str, process_limit: str
) -> None:
    DockerCarrier(policy_image)._run(
        ["container", "update", "--pids-limit", process_limit, container]
    )


def _docker_provider_child_observation(
    container: str,
    policy_image: str | None = None,
) -> ChildObservation:
    container_id, runner_process_id = _docker_runner_identity(container)
    current_process_count, process_limit, limit_hit_count = (
        _runner_cgroup_process_evidence(runner_process_id)
    )
    if policy_image is not None:
        _enforce_runner_process_ceiling(policy_image, container, current_process_count)
        current_process_count, process_limit, limit_hit_count = (
            _runner_cgroup_process_evidence(runner_process_id)
        )
    if process_limit != current_process_count:
        raise RuntimeError("runner-core-reconnect-cgroup-process-limit-mismatch")
    return observe_provider_child(
        container_id,
        runner_process_id,
        current_process_count,
        process_limit,
        limit_hit_count,
    )


def _child_observation_from_fields(
    document: Mapping[str, object], refusal: str
) -> ChildObservation:
    if set(document) != set(_CHILD_OBSERVATION_FIELDS):
        raise RuntimeError(refusal)
    try:
        values = {field: document[field] for field in _CHILD_OBSERVATION_FIELDS}
    except KeyError as error:
        raise RuntimeError(refusal) from error
    if any(not isinstance(value, str) or not value for value in values.values()):
        raise RuntimeError(refusal)
    return ChildObservation(**cast(dict[str, str], values))


def _started_child_observation(path: Path) -> ChildObservation:
    document = _read_witness_document(
        path,
        "runner-core-reconnect-started-cut-missing",
        "runner-core-reconnect-started-cut-malformed",
    )
    _require_exact_string_fields(
        document,
        _CORE_STARTED_CUT_RECORD_FIELDS,
        "runner-core-reconnect-started-cut-malformed",
    )
    if document["record_family"] != _WITNESS_RECORD_FAMILY:
        raise RuntimeError("runner-core-reconnect-started-cut-malformed")
    return _child_observation_from_fields(
        {field: document[field] for field in _CHILD_OBSERVATION_FIELDS},
        "runner-core-reconnect-started-cut-malformed",
    )


def _observation_identity(observation: ChildObservation) -> str:
    return (
        f"{observation.runner_container_id}\t{observation.runner_process_id}\t"
        f"{observation.provider_child_pid}\t"
        f"{observation.provider_child_start_time_ticks}\t"
        f"{observation.provider_child_count}\t"
        f"{observation.runner_cgroup_pids_current}\t"
        f"{observation.runner_cgroup_pids_limit}\t"
        f"{observation.runner_cgroup_limit_hit_count}"
    )


def _docker_runner_identity(container: str) -> tuple[str, int]:
    completed = subprocess.run(
        ["docker", "container", "inspect", container],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("runner-core-reconnect-child-gone")
    try:
        documents = json.loads(completed.stdout)
        document = documents[0]
        if len(documents) != 1 or not document["State"]["Running"]:
            raise ValueError("runner container is not running")
        return str(document["Id"]), int(document["State"]["Pid"])
    except Exception as error:
        raise RuntimeError("runner-core-reconnect-child-gone") from error


def _prepare_handoff(root: Path) -> Path:
    handoff = root / "handoff"
    if not (handoff / "manifest").is_file():
        write_candidate_manifest(
            handoff,
            "a" * 40,
            "sha256:" + "b" * 64,
        )
    return handoff


def _started_provider_identity(connection: socket.socket) -> ChildObservation:
    channel = connection.makefile("rwb", buffering=0)
    channel.write(b"LAUNCH\n")
    fields = channel.readline().decode("ascii").split()
    if len(fields) != 5 or fields[0] != "STARTED":
        raise RuntimeError("runner-core-reconnect-started-missing")
    return ChildObservation(
        fields[1], fields[2], fields[3], fields[4], "S", "1", "2", "2", "0"
    )


def _seed_and_crash(root: Path, connection_file_descriptor: int | None) -> None:
    bootstrap = _bootstrap(
        root / "core-store", _prepare_handoff(root), _SCENARIO_CORE_RESTART
    )
    bootstrap.store.arm_runner_invocation(
        bootstrap.execution, bootstrap.binding, _INVOCATION
    )
    started_child = ChildObservation(
        "runner-container-one", "1", "2", "1", "S", "1", "2", "2", "0"
    )
    if connection_file_descriptor is not None:
        with socket.socket(fileno=connection_file_descriptor) as connection:
            started_child = _started_provider_identity(connection)
    _write_core_started_cut(
        root / "core-store",
        bootstrap.binding,
        _INVOCATION,
        _SCENARIO_CORE_RESTART,
        asdict(started_child),
    )
    os._exit(CORE_STARTED_CUT_EXIT_CODE)


def _restart(root: Path, connection_file_descriptor: int | None) -> None:
    bootstrap = _bootstrap(
        root / "core-store", _prepare_handoff(root), _SCENARIO_CORE_RESTART
    )
    try:
        if not bootstrap.restarted:
            raise RuntimeError("runner-core-reconnect-witness-did-not-restart")
        durable = bootstrap.store.load(bootstrap.execution.attempt_id)
        if durable.runner_invocation_id != _INVOCATION:
            raise RuntimeError("runner-core-reconnect-witness-binding-mismatch")
        if connection_file_descriptor is None:
            raise RuntimeError("runner-core-reconnect-connection-missing")
        with socket.socket(fileno=connection_file_descriptor) as connection:
            _started_provider_identity(connection)
            _write_reconnected_core_started(
                root / "core-store",
                bootstrap.binding,
                _INVOCATION,
                _SCENARIO_CORE_RESTART,
            )
            _wait_for_reconnected_child_observation(root / "core-store")
            connection.sendall(b"FINISH\n")
            if connection.makefile("rb", buffering=0).readline() != b"RELEASED\n":
                raise RuntimeError("runner-core-reconnect-session-did-not-finish")
            _atomic_json(
                root / "core-store" / "core-reconnect-session-finished.json",
                {"record_family": _WITNESS_RECORD_FAMILY},
            )
    finally:
        bootstrap.close()


def _start_provider_child() -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal; signal.signal(signal.SIGTERM, signal.SIG_DFL); signal.pause()",
        ]
    )


def _work_under_way(
    child: subprocess.Popen[bytes] | None,
) -> subprocess.Popen[bytes]:
    return _start_provider_child() if child is None else child


def _drive_runner_connection(
    connection_file_descriptor: int,
    child: subprocess.Popen[bytes] | None,
) -> tuple[subprocess.Popen[bytes], bool]:
    with socket.socket(fileno=connection_file_descriptor) as connection:
        channel = connection.makefile("rwb", buffering=0)
        if channel.readline() != b"LAUNCH\n":
            raise RuntimeError("runner-core-reconnect-launch-missing")
        child = _work_under_way(child)
        _state, start_time_ticks = _process_identity(Path(f"/proc/{child.pid}/stat"))
        channel.write(
            f"STARTED runner-container-one {os.getpid()} {child.pid} "
            f"{start_time_ticks}\n".encode("ascii")
        )
        command = channel.readline()
        if command == b"FINISH\n":
            channel.write(b"RELEASED\n")
            return child, True
        if command:
            raise RuntimeError("runner-core-reconnect-command-unknown")
        return child, False


def _run_provider_child(
    first_connection_file_descriptor: int,
    restarted_connection_file_descriptor: int,
) -> None:
    child: subprocess.Popen[bytes] | None = None
    try:
        child, released = _drive_runner_connection(
            first_connection_file_descriptor, child
        )
        if released:
            raise RuntimeError("runner-core-reconnect-cut-missing")
        child, released = _drive_runner_connection(
            restarted_connection_file_descriptor, child
        )
        if not released:
            raise RuntimeError("runner-core-reconnect-session-did-not-finish")
    finally:
        if child is not None:
            if child.poll() is None:
                child.send_signal(signal.SIGTERM)
            child.wait(timeout=10)


def _restart_private_core(
    policy_image: str, container: str, attempt_network: str
) -> str:
    carrier = DockerCarrier(policy_image)
    subnet = carrier._network_subnet(attempt_network)
    carrier.restart_private_container(container)
    return subnet


def _record_inventory(lease_id: str, output: Path) -> None:
    label = lease_label(RunnerLeaseId(lease_id))
    commands = {
        "containers": [
            "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label={label}",
        ],
        "volumes": ["docker", "volume", "ls", "--quiet", "--filter", f"label={label}"],
        "networks": [
            "docker",
            "network",
            "ls",
            "--quiet",
            "--filter",
            f"label={label}",
        ],
    }
    inventory: dict[str, list[str]] = {}
    for kind, command in commands.items():
        completed = subprocess.run(command, capture_output=True, check=False, text=True)
        if completed.returncode != 0:
            raise RuntimeError("runner-core-reconnect-inventory-unreadable")
        inventory[kind] = completed.stdout.split()
    _atomic_json(
        output,
        {"record_family": _WITNESS_RECORD_FAMILY, "label": label, **inventory},
    )
    if any(inventory.values()):
        raise RuntimeError("runner-core-reconnect-labelled-objects-remain")


def _freeze_started_child(
    policy_image: str,
    container: str,
    lease_directory: Path,
    lease_id: str,
    root: Path,
) -> ChildObservation:
    request = root / _CORE_STARTED_CUT_EVENT
    fenced = root / _CORE_STARTED_CUT_FENCED_EVENT
    try:
        with request.open("rb", buffering=0) as channel:
            marker = channel.readline()
    except OSError as error:
        raise RuntimeError("runner-core-reconnect-cut-fence-event-missing") from error
    if marker != _CORE_STARTED_CUT_REQUEST:
        raise RuntimeError("runner-core-reconnect-cut-fence-marker-mismatch")
    require_claimed_lease(lease_directory, lease_id)
    observation = _docker_provider_child_observation(container, policy_image)
    _atomic_json(root / _CORE_STARTED_CHILD_RECORD, asdict(observation))
    try:
        with fenced.open("wb", buffering=0) as channel:
            channel.write(_CORE_STARTED_CUT_FENCED)
    except OSError as error:
        raise RuntimeError("runner-core-reconnect-cut-fence-event-missing") from error
    return observation


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("seed-and-crash", "restart"):
        command = commands.add_parser(name)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--connection", type=int)
    observe = commands.add_parser("observe-child")
    observe.add_argument("--container", required=True)
    observe.add_argument("--lease-directory", type=Path, required=True)
    observe.add_argument("--lease-id", required=True)
    observe.add_argument("--output", type=Path, required=True)
    observe.add_argument("--binding-witness", type=Path, required=True)
    observe.add_argument("--enforce-current-process-limit", action="store_true")
    observe.add_argument("--policy-image")
    observe.add_argument("--phase", choices=_CHILD_PHASES, required=True)
    freeze = commands.add_parser("freeze-started-child")
    freeze.add_argument("--container", required=True)
    freeze.add_argument("--lease-directory", type=Path, required=True)
    freeze.add_argument("--lease-id", required=True)
    freeze.add_argument("--root", type=Path, required=True)
    freeze.add_argument("--policy-image", required=True)
    freeze.add_argument("--enforce-current-process-limit", action="store_true")
    read_started = commands.add_parser("read-started-child")
    read_started.add_argument("--binding-witness", type=Path, required=True)
    inventory = commands.add_parser("record-inventory")
    inventory.add_argument("--lease-id", required=True)
    inventory.add_argument("--output", type=Path, required=True)
    runner = commands.add_parser("runner")
    runner.add_argument("--first-connection", type=int, required=True)
    runner.add_argument("--restarted-connection", type=int, required=True)
    restart_private = commands.add_parser("restart-private-core")
    restart_private.add_argument("--policy-image", required=True)
    restart_private.add_argument("--container", required=True)
    restart_private.add_argument("--attempt-network", required=True)
    parsed = parser.parse_args(arguments)
    if parsed.command == "seed-and-crash":
        _seed_and_crash(parsed.root, parsed.connection)
    elif parsed.command == "restart":
        _restart(parsed.root, parsed.connection)
    elif parsed.command == "runner":
        _run_provider_child(parsed.first_connection, parsed.restarted_connection)
    elif parsed.command == "restart-private-core":
        print(
            _restart_private_core(
                parsed.policy_image, parsed.container, parsed.attempt_network
            )
        )
    elif parsed.command == "observe-child":
        require_claimed_lease(parsed.lease_directory, parsed.lease_id)
        if parsed.enforce_current_process_limit and parsed.policy_image is None:
            raise ValueError("runner-core-reconnect-policy-image-missing")
        observation = _docker_provider_child_observation(
            parsed.container,
            parsed.policy_image if parsed.enforce_current_process_limit else None,
        )
        record_child_observation(
            parsed.output,
            parsed.phase,
            observation,
            _core_witness_binding(parsed.binding_witness),
        )
        print(_observation_identity(observation))
    elif parsed.command == "freeze-started-child":
        if not parsed.enforce_current_process_limit:
            raise ValueError("runner-core-reconnect-cgroup-fence-not-requested")
        print(
            _observation_identity(
                _freeze_started_child(
                    parsed.policy_image,
                    parsed.container,
                    parsed.lease_directory,
                    parsed.lease_id,
                    parsed.root,
                )
            )
        )
    elif parsed.command == "read-started-child":
        print(_observation_identity(_started_child_observation(parsed.binding_witness)))
    else:
        _record_inventory(parsed.lease_id, parsed.output)
    return 0


if __name__ == "__main__":
    try:
        status = main()
    except (RuntimeError, ValueError) as refusal:
        print(refusal, file=sys.stderr)
        status = 1
    raise SystemExit(status)
