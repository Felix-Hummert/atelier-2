from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONTAINER_LIVE = PROJECT_ROOT / "scripts" / "container_live.sh"
CONTAINER_SNAPSHOT = PROJECT_ROOT / "scripts" / "container_snapshot.sh"
CONTAINER_UP = PROJECT_ROOT / "scripts" / "container_up.sh"
CONTAINER_SERVE = PROJECT_ROOT / "scripts" / "container_serve.sh"
COMPOSE = PROJECT_ROOT / "compose.yaml"
DOCKERFILE = PROJECT_ROOT / "Dockerfile"

CONTAINER_ID = "a" * 64
IMAGE_ID = f"sha256:{'b' * 64}"
NETWORK_ID = "c" * 64
ENGINE_ID = "engine:local:test"
PROJECT_NAME = re.compile(r"^atelier2-live-[0-9a-f]{16}$")
RECORDED_START_STOP = [
    ["start", CONTAINER_ID],
    ["stop", "--time", "30", CONTAINER_ID],
]


def write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def install_docker_stub(directory: Path) -> None:
    write_stub(
        directory / "docker",
        f"""\
import json
import os
import signal
import shutil
import sys
import time
from pathlib import Path
CONTAINER_ID = {CONTAINER_ID!r}; IMAGE_ID = {IMAGE_ID!r}
NETWORK_ID = {NETWORK_ID!r}; ENGINE_ID = {ENGINE_ID!r}
arguments = sys.argv[1:]
state_path = Path(os.environ["ATELIER2_TEST_DOCKER_STATE"]); record_path = Path(os.environ["ATELIER2_TEST_DOCKER_RECORD"])
with record_path.open("a", encoding="utf-8") as output:
    output.write(json.dumps(arguments) + "\\n")
state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {{}}
drift = os.environ.get("ATELIER2_TEST_DRIFT", ""); list_failure = os.environ.get("ATELIER2_TEST_PROJECT_LIST_FAILURE", "")
def save() -> None:
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
def wait_at(phase: str) -> None:
    if phase not in os.environ.get("ATELIER2_TEST_WAIT_PHASE", "").split(","):
        return
    (Path(os.environ["ATELIER2_TEST_READY_DIRECTORY"]) / f"{{phase}}-ready").touch()
    if phase != "start-cleanup":
        signal.pause()
        return
    for interruption in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(interruption, signal.SIG_IGN)
    release = Path(os.environ["ATELIER2_TEST_READY_DIRECTORY"]) / "start-cleanup-release"
    while not release.exists():
        time.sleep(0.01)
def label(name: str) -> str:
    if drift == "label" and name == "atelier2.deployment":
        return "foreign"
    if name == "atelier2.deployment":
        return state["deployment"]
    if name == "atelier2.source.commit":
        return "0" * 40 if drift == "source" else state["source_commit"]
    if name == "atelier2.source.tree":
        return state["source_tree"]
    if name == "com.docker.compose.project":
        return state["project"]
    return ""
if arguments[:2] == ["info", "--format"]:
    print(os.environ.get("ATELIER2_TEST_INITIAL_ENGINE_ID", "different-engine" if drift == "engine" else ENGINE_ID))
    raise SystemExit(0)
if "com.docker.compose.project=" in arguments[-1]:
    if list_failure == "container" and arguments[:2] == ["ps", "--all"]: raise SystemExit(52)
    if list_failure in ("volume", "network") and arguments[:2] == [list_failure, "ls"]: raise SystemExit(52)
if arguments and arguments[0] == "compose":
    project = arguments[arguments.index("--project-name") + 1]
    if "build" in arguments:
        if os.environ.get("ATELIER2_TEST_REQUIRE_INTENT") == "1":
            record = Path(os.environ["XDG_STATE_HOME"]) / "atelier2/container-live/installation.state"
            if not record.is_file() or "state=INSTALLING\\n" not in record.read_text(encoding="utf-8"): raise SystemExit(61)
        wait_at("build")
        if os.environ.get("ATELIER2_TEST_FAIL_BUILD") == "1": raise SystemExit(41)
        context = Path(arguments[arguments.index("--project-directory") + 1])
        copy_to = os.environ.get("ATELIER2_TEST_CONTEXT")
        if copy_to: shutil.copytree(context, copy_to)
        raise SystemExit(0)
    if "up" in arguments:
        wait_at("up")
        state = {{
            "project": project,
            "deployment": os.environ["ATELIER2_DEPLOYMENT"],
            "source_commit": os.environ["ATELIER2_SOURCE_COMMIT"],
            "source_tree": os.environ["ATELIER2_SOURCE_TREE"],
            "status": "running",
            "health": "healthy",
        }}
        save()
        if os.environ.get("ATELIER2_TEST_FAIL_UP") == "1": raise SystemExit(42)
        raise SystemExit(0)
    if "ps" in arguments:
        if state: print(CONTAINER_ID)
        raise SystemExit(0)
    if "down" in arguments:
        if os.environ.get("ATELIER2_TEST_FAIL_DOWN") == "1": raise SystemExit(43)
        if state.get("project") == project: state = {{}}; save()
        raise SystemExit(0)
if arguments[:2] == ["ps", "--all"]:
    if os.environ.get("ATELIER2_TEST_FOREIGN_RESOURCE") == "1":
        print("d" * 64)
    elif state:
        wanted = arguments[-1].split("=", 1)[1]
        if wanted in ("local-live", state["project"]):
            print(CONTAINER_ID)
    raise SystemExit(0)
if arguments[:2] in (["volume", "ls"], ["network", "ls"]):
    if os.environ.get("ATELIER2_TEST_FOREIGN_RESOURCE") == "1":
        print("foreign")
    elif state:
        wanted = arguments[-1].split("=", 1)[1]
        if wanted in ("local-live", state["project"]):
            suffix = "store" if arguments[0] == "volume" else "serve"
            print(f"{{state['project']}}_{{suffix}}")
    raise SystemExit(0)
if arguments and arguments[0] == "inspect":
    if not state or arguments[-1] != CONTAINER_ID: raise SystemExit(44)
    template = arguments[arguments.index("--format") + 1]
    template = template.replace("{{{{", "{{").replace("}}}}", "}}")
    if template == "{{.Id}}":
        print("d" * 64 if drift == "container" else CONTAINER_ID)
    elif template == "{{.Image}}":
        print(os.environ.get("ATELIER2_TEST_INITIAL_IMAGE_ID", f"sha256:{{'e' * 64}}" if drift == "image" else IMAGE_ID))
    elif "index .Config.Labels" in template:
        name = template.split('"')[1]
        print(label(name))
    elif template == "{{.HostConfig.RestartPolicy.Name}}":
        print("always" if drift == "restart" else "unless-stopped")
    elif template == "{{.HostConfig.ReadonlyRootfs}}":
        print("true")
    elif template == "{{.HostConfig.Privileged}}":
        print("false")
    elif template == "{{json .HostConfig.CapDrop}}":
        print('["ALL"]')
    elif template == "{{json .HostConfig.SecurityOpt}}":
        print('["no-new-privileges:true"]')
    elif template == "{{len .HostConfig.PortBindings}}":
        print("1")
    elif "HostConfig.PortBindings" in template:
        print("127.0.0.1:9999" if drift == "port" else "127.0.0.1:8422")
    elif "range .Mounts" in template:
        print("bind||/var/lib/atelier2/store|true" if drift == "mount" else f"volume|{{state['project']}}_store|/var/lib/atelier2/store|true")
    elif template == "{{len .NetworkSettings.Networks}}":
        print("0" if drift == "network-detached" else "2" if drift == "network-extra" else "1")
    elif "index .NetworkSettings.Networks" in template:
        print("" if drift == "network-wrong" else "d" * 64 if drift == "network-attachment-id" else NETWORK_ID)
    elif template == "{{json .Config}}":
        configuration = {{"image": IMAGE_ID, "project": state["project"], "source": state["source_commit"]}}
        if drift == "config": configuration["changed"] = True
        print(json.dumps(configuration, sort_keys=True, separators=(",", ":")))
    elif template == "{{.State.Status}}":
        print(state["status"])
    elif template == "{{.State.Health.Status}}":
        wait_at("health")
        print("unhealthy" if drift == "health" else state["health"])
    else: raise SystemExit(45)
    raise SystemExit(0)
if arguments[:2] in (["volume", "inspect"], ["network", "inspect"]):
    resource_type = arguments[0]
    template = arguments[arguments.index("--format") + 1]
    template = template.replace("{{{{", "{{").replace("}}}}", "}}")
    name = arguments[-1]
    expected = f"{{state['project']}}_{{'store' if resource_type == 'volume' else 'serve'}}"
    if not state or name != expected: raise SystemExit(46)
    if template == "{{.Name}}":
        print(name)
    elif template == "{{.Id}}" and resource_type == "network":
        print(os.environ.get("ATELIER2_TEST_INITIAL_NETWORK_ID", "f" * 64 if drift == "network" else NETWORK_ID))
    elif "index .Labels" in template:
        print(label(template.split('"')[1]))
    else: raise SystemExit(47)
    raise SystemExit(0)
if arguments and arguments[0] == "stop":
    if not state or arguments[-1] != CONTAINER_ID: raise SystemExit(48)
    wait_at("start-cleanup")
    if os.environ.get("ATELIER2_TEST_FAIL_STOP") == "1": raise SystemExit(53)
    state["status"] = "exited"
    save()
    print(CONTAINER_ID)
    raise SystemExit(0)
if arguments and arguments[0] == "start":
    if not state or arguments[-1] != CONTAINER_ID: raise SystemExit(49)
    wait_at("start")
    state["status"] = "running"
    state["health"] = "healthy"
    save()
    if os.environ.get("ATELIER2_TEST_FAIL_START") == "1": raise SystemExit(51)
    print(CONTAINER_ID)
    raise SystemExit(0)
raise SystemExit(50)
""",
    )


def install_host_stubs(directory: Path) -> None:
    write_stub(
        directory / "systemctl",
        """\
import os
import sys

unit = next(argument for argument in sys.argv if argument.endswith(".service"))
state = os.environ.get("ATELIER2_TEST_HOST_UNIT", "off")
if state == "active":
    print("LoadState=loaded\\nActiveState=active\\nUnitFileState=enabled")
elif state == "enabled":
    print("LoadState=loaded\\nActiveState=inactive\\nUnitFileState=enabled")
elif unit == "atelier2-live.service":
    print("LoadState=loaded\\nActiveState=inactive\\nUnitFileState=disabled")
else:
    print("LoadState=not-found\\nActiveState=inactive\\nUnitFileState=")
""",
    )
    write_stub(
        directory / "ss",
        """\
import os

if os.environ.get("ATELIER2_TEST_PORT_BUSY") == "1":
    print("LISTEN 0 4096 127.0.0.1:8422 0.0.0.0:*")
""",
    )
    write_stub(directory / "sleep", "")
    real_stat = shutil.which("stat")
    assert real_stat is not None
    write_stub(
        directory / "stat",
        f"""\
import os
import sys

if os.environ.get("ATELIER2_TEST_WRONG_OWNER") == "1" and sys.argv[-1].endswith("installation.state"):
    print("999999:600")
    raise SystemExit(0)
os.execv({real_stat!r}, [{real_stat!r}, *sys.argv[1:]])
""",
    )


def run_git(repository: Path, *arguments: str) -> str:
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "container-live-test",
        "GIT_AUTHOR_EMAIL": "container-live-test@invalid",
        "GIT_COMMITTER_NAME": "container-live-test",
        "GIT_COMMITTER_EMAIL": "container-live-test@invalid",
    }
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def lifecycle_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository with ; metacharacters"
    (repository / "scripts").mkdir(parents=True)
    for source, destination in (
        (CONTAINER_LIVE, repository / "scripts/container_live.sh"),
        (CONTAINER_SNAPSHOT, repository / "scripts/container_snapshot.sh"),
        (CONTAINER_UP, repository / "scripts/container_up.sh"),
        (CONTAINER_SERVE, repository / "scripts/container_serve.sh"),
        (COMPOSE, repository / "compose.yaml"),
        (DOCKERFILE, repository / "Dockerfile"),
    ):
        shutil.copy2(source, destination)
    (repository / "payload.txt").write_text("committed\n", encoding="utf-8")
    run_git(repository, "init", "--quiet", "--initial-branch=main")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "--quiet", "--message", "fixture")
    return repository


def lifecycle_environment(tmp_path: Path, **settings: str) -> dict[str, str]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(exist_ok=True)
    install_docker_stub(bin_directory)
    install_host_stubs(bin_directory)
    environment = {
        **os.environ,
        "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        "XDG_STATE_HOME": str(tmp_path / "state home ; metacharacters"),
        "TMPDIR": str(tmp_path),
        "ATELIER2_TEST_DOCKER_STATE": str(tmp_path / "docker-state.json"),
        "ATELIER2_TEST_DOCKER_RECORD": str(tmp_path / "docker-record.jsonl"),
        "ATELIER2_TEST_CONTEXT": str(tmp_path / "docker-context"),
        "ATELIER2_TEST_READY_DIRECTORY": str(tmp_path),
    }
    for variable in (
        "ATELIER2_DEPLOYMENT",
        "ATELIER2_PUBLISHED_PORT",
        "ATELIER2_RESTART_POLICY",
    ):
        environment.pop(variable, None)
    environment.update(settings)
    return environment


def run_live(
    repository: Path,
    tmp_path: Path,
    command: str,
    **settings: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repository / "scripts/container_live.sh"), command],
        cwd=repository,
        env=lifecycle_environment(tmp_path, **settings),
        capture_output=True,
        text=True,
        check=False,
    )


def docker_invocations(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / "docker-record.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def installation_directory(tmp_path: Path) -> Path:
    return tmp_path / "state home ; metacharacters/atelier2/container-live"


def read_record(tmp_path: Path) -> dict[str, str]:
    path = installation_directory(tmp_path) / "installation.state"
    return dict(
        line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
    )


def installed_repository(tmp_path: Path) -> Path:
    repository = lifecycle_repository(tmp_path)
    completed = run_live(
        repository,
        tmp_path,
        "install",
        ATELIER2_TEST_REQUIRE_INTENT="1",
    )
    assert completed.returncode == 0, completed.stderr
    return repository


def stopped_repository(tmp_path: Path) -> tuple[Path, bytes]:
    repository = installed_repository(tmp_path)
    assert run_live(repository, tmp_path, "stop").returncode == 0
    record = (installation_directory(tmp_path) / "installation.state").read_bytes()
    (tmp_path / "docker-record.jsonl").write_text("", encoding="utf-8")
    return repository, record


def wait_until_exists(path: Path) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), f"stub did not reach {path.name}"


def docker_mutations(invocations: list[list[str]]) -> list[list[str]]:
    return [
        arguments
        for arguments in invocations
        if "build" in arguments
        or "up" in arguments
        or "down" in arguments
        or arguments[:1] in (["start"], ["stop"])
    ]


def test_status_without_an_installation_is_truthfully_incomplete(
    tmp_path: Path,
) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(repository, tmp_path, "status")

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "INCOMPLETE\n"
    assert not installation_directory(tmp_path).exists()
    assert docker_invocations(tmp_path) == []


def test_install_publishes_private_exact_identity_before_handoff(
    tmp_path: Path,
) -> None:
    repository = installed_repository(tmp_path)

    record = read_record(tmp_path)
    directory = installation_directory(tmp_path)
    assert record["state"] == "INSTALLED"
    assert record["deployment"] == "local-live"
    assert record["published_port"] == "8422"
    assert record["restart_policy"] == "unless-stopped"
    assert PROJECT_NAME.fullmatch(record["project"])
    assert record["source_commit"] == run_git(repository, "rev-parse", "HEAD")
    assert record["source_tree"] == run_git(repository, "rev-parse", "HEAD^{tree}")
    assert record["engine_id"] == ENGINE_ID
    assert record["container_id"] == CONTAINER_ID
    assert record["image_id"] == IMAGE_ID
    assert record["network_id"] == NETWORK_ID
    assert record["volume_name"] == f"{record['project']}_store"
    assert record["network_name"] == f"{record['project']}_serve"
    assert (directory.stat().st_mode & 0o777) == 0o700
    for filename in ("lifecycle.lock", "installation.state", "compose.yaml"):
        path = directory / filename
        assert path.is_file() and not path.is_symlink()
        assert (path.stat().st_mode & 0o777) == 0o600
    assert not list(directory.glob(".*.??????"))
    assert not list(tmp_path.glob("atelier2-live.*"))
    invocations = docker_invocations(tmp_path)
    assert all("prune" not in arguments for arguments in invocations)
    build = next(arguments for arguments in invocations if "build" in arguments)
    up = next(arguments for arguments in invocations if "up" in arguments)
    assert build[build.index("--project-name") + 1] == record["project"]
    assert up[-6:] == ["up", "--detach", "--wait", "--wait-timeout", "30", "--no-build"]


def test_status_is_read_only_and_distinguishes_running_and_stopped(
    tmp_path: Path,
) -> None:
    repository = installed_repository(tmp_path)
    record_path = tmp_path / "docker-record.jsonl"
    before_record = (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes()
    before_descriptor = (installation_directory(tmp_path) / "compose.yaml").read_bytes()
    record_path.write_text("", encoding="utf-8")

    running = run_live(repository, tmp_path, "status")

    assert running.returncode == 0, running.stderr
    assert running.stdout == "RUNNING\n"
    assert docker_mutations(docker_invocations(tmp_path)) == []
    assert (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes() == before_record
    assert (
        installation_directory(tmp_path) / "compose.yaml"
    ).read_bytes() == before_descriptor

    stopped = run_live(repository, tmp_path, "stop")
    assert stopped.returncode == 0, stopped.stderr
    record_path.write_text("", encoding="utf-8")
    status = run_live(repository, tmp_path, "status")
    assert status.stdout == "STOPPED\n"
    assert docker_mutations(docker_invocations(tmp_path)) == []


def test_stop_and_start_use_only_the_recorded_container_id(tmp_path: Path) -> None:
    repository = installed_repository(tmp_path)
    record_path = tmp_path / "docker-record.jsonl"
    record_path.write_text("", encoding="utf-8")

    stopped = run_live(repository, tmp_path, "stop")
    started = run_live(repository, tmp_path, "start")

    assert stopped.returncode == 0, stopped.stderr
    assert stopped.stdout == "STOPPED\n"
    assert started.returncode == 0, started.stderr
    assert started.stdout == "RUNNING\n"
    mutations = docker_mutations(docker_invocations(tmp_path))
    assert mutations == [
        ["stop", "--time", "30", CONTAINER_ID],
        ["start", CONTAINER_ID],
    ]
    assert all("compose" not in arguments for arguments in mutations)


@pytest.mark.parametrize(
    "settings",
    (
        {"ATELIER2_TEST_PORT_BUSY": "1"},
        {"ATELIER2_TEST_HOST_UNIT": "active"},
        {"ATELIER2_TEST_HOST_UNIT": "enabled"},
        {"ATELIER2_TEST_FOREIGN_RESOURCE": "1"},
        {"ATELIER2_DEPLOYMENT": "disposable"},
        {"ATELIER2_PUBLISHED_PORT": "9999"},
        {"ATELIER2_RESTART_POLICY": "always"},
        {"ATELIER2_TEST_INITIAL_ENGINE_ID": ""},
        {"ATELIER2_TEST_INITIAL_ENGINE_ID": "engine\nmalformed"},
    ),
)
def test_install_refuses_collisions_and_ambient_modes_before_docker_mutation(
    tmp_path: Path, settings: dict[str, str]
) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(repository, tmp_path, "install", **settings)

    assert completed.returncode != 0
    assert docker_mutations(docker_invocations(tmp_path)) == []
    assert not (installation_directory(tmp_path) / "installation.state").exists()


def test_dirty_source_refuses_before_docker_and_durable_intent(tmp_path: Path) -> None:
    repository = lifecycle_repository(tmp_path)
    (repository / "ambient.txt").write_text("dirty\n", encoding="utf-8")

    completed = run_live(repository, tmp_path, "install")

    assert completed.returncode != 0
    assert "source tree must be clean" in completed.stderr
    assert docker_mutations(docker_invocations(tmp_path)) == []
    assert not (installation_directory(tmp_path) / "installation.state").exists()
    assert not list(tmp_path.glob("atelier2-live.*"))


@pytest.mark.parametrize(
    "drift",
    (
        "engine",
        "image",
        "container",
        "label",
        "source",
        "restart",
        "port",
        "mount",
        "network",
        "network-detached",
        "network-wrong",
        "network-extra",
        "network-attachment-id",
        "config",
    ),
)
def test_identity_drift_refuses_status_and_exact_operations(
    tmp_path: Path, drift: str
) -> None:
    repository = installed_repository(tmp_path)
    record_path = tmp_path / "docker-record.jsonl"
    record_path.write_text("", encoding="utf-8")

    status = run_live(repository, tmp_path, "status", ATELIER2_TEST_DRIFT=drift)
    stopped = run_live(repository, tmp_path, "stop", ATELIER2_TEST_DRIFT=drift)
    started = run_live(repository, tmp_path, "start", ATELIER2_TEST_DRIFT=drift)

    assert status.returncode == 0
    assert status.stdout == "DRIFTED\n"
    assert stopped.returncode != 0
    assert "drifted" in stopped.stderr
    assert started.returncode != 0
    assert "drifted" in started.stderr
    assert docker_mutations(docker_invocations(tmp_path)) == []


@pytest.mark.parametrize(
    "damage",
    (
        "owner",
        "mode",
        "oversized",
        "symlink",
        "state-downgrade",
        "injection",
        "descriptor",
    ),
    ids=(
        "record-wrong-owner",
        "record-wrong-mode",
        "record-oversized",
        "record-symlink",
        "record-state-downgrade",
        "record-injection",
        "descriptor-content",
    ),
)
def test_record_boundary_refuses_unsafe_or_drifted_state(
    tmp_path: Path, damage: str
) -> None:
    repository = installed_repository(tmp_path)
    record_path = installation_directory(tmp_path) / "installation.state"
    original = record_path.read_bytes()
    if damage == "owner":
        pass
    elif damage == "mode":
        record_path.chmod(0o644)
    elif damage == "oversized":
        record_path.write_bytes(b"x" * 16385)
    elif damage == "symlink":
        target = installation_directory(tmp_path) / "record-target"
        target.write_bytes(original)
        target.chmod(0o600)
        record_path.unlink()
        record_path.symlink_to(target)
    elif damage == "state-downgrade":
        record_path.write_bytes(
            original.replace(b"state=INSTALLED", b"state=INSTALLING")
        )
    elif damage == "injection":
        marker = tmp_path / "record-was-executed"
        record_path.write_bytes(original + f"project=$(touch {marker})\n".encode())
    else:
        (installation_directory(tmp_path) / "compose.yaml").write_text(
            "changed\n", encoding="utf-8"
        )
    if damage in ("oversized", "injection"):
        record_path.chmod(0o600)
    before = docker_invocations(tmp_path)

    completed = run_live(
        repository,
        tmp_path,
        "status",
        ATELIER2_TEST_WRONG_OWNER="1" if damage == "owner" else "0",
    )

    assert completed.returncode == 0
    assert completed.stdout == "DRIFTED\n"
    assert docker_mutations(docker_invocations(tmp_path)[len(before) :]) == []
    assert not (tmp_path / "record-was-executed").exists()


def test_nonblocking_lock_refuses_concurrent_lifecycle_command(tmp_path: Path) -> None:
    repository = installed_repository(tmp_path)
    lock_path = installation_directory(tmp_path) / "lifecycle.lock"
    with lock_path.open("r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        completed = run_live(repository, tmp_path, "status")

    assert completed.returncode != 0
    assert "lifecycle is busy" in completed.stderr


@pytest.mark.parametrize(
    "settings",
    (
        {"ATELIER2_TEST_FAIL_UP": "1"},
        {"ATELIER2_TEST_INITIAL_IMAGE_ID": "sha256:invalid"},
        {"ATELIER2_TEST_INITIAL_NETWORK_ID": "invalid"},
    ),
)
def test_failed_install_cleans_only_its_intent_owned_project(
    tmp_path: Path, settings: dict[str, str]
) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(repository, tmp_path, "install", **settings)

    assert completed.returncode != 0
    assert "cockpit ->" not in completed.stdout
    invocations = docker_invocations(tmp_path)
    project = next(
        arguments[arguments.index("--project-name") + 1]
        for arguments in invocations
        if "build" in arguments
    )
    down = next(arguments for arguments in invocations if "down" in arguments)
    assert down[down.index("--project-name") + 1] == project
    assert down[-5:] == ["down", "--volumes", "--rmi", "local", "--remove-orphans"]
    assert not (installation_directory(tmp_path) / "installation.state").exists()
    assert json.loads((tmp_path / "docker-state.json").read_text()) == {}


@pytest.mark.parametrize(
    "settings",
    (
        {"ATELIER2_TEST_FAIL_DOWN": "1"},
        {"ATELIER2_TEST_PROJECT_LIST_FAILURE": "container"},
        {"ATELIER2_TEST_PROJECT_LIST_FAILURE": "volume"},
        {"ATELIER2_TEST_PROJECT_LIST_FAILURE": "network"},
    ),
    ids=(
        "teardown-command-failure",
        "container-inventory-failure",
        "volume-inventory-failure",
        "network-inventory-failure",
    ),
)
def test_failed_cleanup_keeps_durable_intent_for_recovery(
    tmp_path: Path, settings: dict[str, str]
) -> None:
    repository = lifecycle_repository(tmp_path)

    completed = run_live(
        repository,
        tmp_path,
        "install",
        ATELIER2_TEST_FAIL_UP="1",
        **settings,
    )

    assert completed.returncode == 42
    assert "cleanup is incomplete" in completed.stderr
    assert read_record(tmp_path)["state"] == "INSTALLING"
    status = run_live(repository, tmp_path, "status")
    assert status.stdout == "INCOMPLETE\n"
    if "ATELIER2_TEST_PROJECT_LIST_FAILURE" in settings:
        assert not any("down" in item for item in docker_invocations(tmp_path))


@pytest.mark.parametrize(
    ("command", "phase", "interruption", "status", "repeated"),
    (
        ("install", "build", signal.SIGHUP, 129, False),
        ("install", "up", signal.SIGINT, 130, False),
        ("start", "start", signal.SIGHUP, 129, False),
        ("start", "health", signal.SIGINT, 130, False),
        ("start", "start", signal.SIGINT, 130, True),
    ),
)
def test_lifecycle_signal_cleans_only_the_exact_owned_runtime(
    tmp_path: Path,
    command: str,
    phase: str,
    interruption: signal.Signals,
    status: int,
    repeated: bool,
) -> None:
    if command == "install":
        repository, before_record = lifecycle_repository(tmp_path), b""
    else:
        repository, before_record = stopped_repository(tmp_path)
    wait_phases = f"{phase},start-cleanup" if repeated else phase
    environment = lifecycle_environment(tmp_path, ATELIER2_TEST_WAIT_PHASE=wait_phases)
    process = subprocess.Popen(
        ["bash", str(repository / "scripts/container_live.sh"), command],
        cwd=repository,
        env=environment,
        start_new_session=True,
    )
    ready = tmp_path / f"{phase}-ready"
    wait_until_exists(ready)
    os.killpg(process.pid, interruption)
    if repeated:
        wait_until_exists(tmp_path / "start-cleanup-ready")
        os.killpg(process.pid, signal.SIGTERM)
        (tmp_path / "start-cleanup-release").touch()

    assert process.wait(timeout=5) == status
    mutations = docker_mutations(docker_invocations(tmp_path))
    if command == "start":
        assert mutations == RECORDED_START_STOP
        assert (
            installation_directory(tmp_path) / "installation.state"
        ).read_bytes() == before_record
        return
    project = mutations[0][mutations[0].index("--project-name") + 1]
    assert mutations[-1][-5:] == [
        "down",
        "--volumes",
        "--rmi",
        "local",
        "--remove-orphans",
    ]
    assert all(
        arguments[arguments.index("--project-name") + 1] == project
        for arguments in mutations
    )
    assert not (installation_directory(tmp_path) / "installation.state").exists()


@pytest.mark.parametrize(
    ("settings", "expected_state", "cleanup_incomplete"),
    (
        ({"ATELIER2_TEST_DRIFT": "health"}, "exited", False),
        ({"ATELIER2_TEST_FAIL_START": "1"}, "exited", False),
        (
            {"ATELIER2_TEST_FAIL_START": "1", "ATELIER2_TEST_FAIL_STOP": "1"},
            "running",
            True,
        ),
    ),
    ids=(
        "unhealthy-health-check",
        "ambiguous-start-failure",
        "ambiguous-start-and-cleanup-failure",
    ),
)
def test_failed_start_stops_the_exact_recorded_container_and_keeps_state(
    tmp_path: Path,
    settings: dict[str, str],
    expected_state: str,
    cleanup_incomplete: bool,
) -> None:
    repository, before_record = stopped_repository(tmp_path)

    completed = run_live(repository, tmp_path, "start", **settings)

    assert completed.returncode == 1
    assert docker_mutations(docker_invocations(tmp_path)) == RECORDED_START_STOP
    state = json.loads((tmp_path / "docker-state.json").read_text())["status"]
    assert state == expected_state
    assert (
        installation_directory(tmp_path) / "installation.state"
    ).read_bytes() == before_record
    assert ("cleanup is incomplete" in completed.stderr) is cleanup_incomplete


LIFECYCLE_GUARDS = (
    "flock --nonblock 9",
    '[state]="INSTALLING"',
    "sync -f",
    'mv -f -- "${temporary_record}" "${record_file}"',
    '[[ -f "${path}" && ! -L "${path}" ]]',
    "((${#record[@]} == ${#required[@]}))",
    'resources="$(docker ps --all --quiet',
    '[[ -z "${temporary_descriptor}" ]] || rm -f -- "${temporary_descriptor}"',
    'docker stop --time 30 "${record[container_id]}"',
    'docker start "${record[container_id]}"',
)


def assert_lifecycle_guards(script: str) -> None:
    for required in LIFECYCLE_GUARDS:
        assert required in script


def test_lifecycle_guard_mutations_bite_the_contract() -> None:
    script = CONTAINER_LIVE.read_text(encoding="utf-8")
    assert_lifecycle_guards(script)
    for required in LIFECYCLE_GUARDS:
        with pytest.raises(AssertionError):
            assert_lifecycle_guards(script.replace(required, "removed"))


def test_live_script_has_no_broad_or_deferred_lifecycle_authority() -> None:
    script = CONTAINER_LIVE.read_text(encoding="utf-8").lower()
    for forbidden in (
        "prune",
        "systemctl --user start",
        "systemctl --user stop",
        "docker restart",
        "update_container",
        "migrate",
        "rollback",
        "retire",
        "provider",
        "runner",
        "songmaker",
    ):
        assert forbidden not in script
