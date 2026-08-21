from __future__ import annotations

import copy
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
COMPOSE = PROJECT_ROOT / "compose.yaml"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
CONTAINER_UP = PROJECT_ROOT / "scripts" / "container_up.sh"
CONTAINER_SERVE = PROJECT_ROOT / "scripts" / "container_serve.sh"
OPERATIONS = PROJECT_ROOT / "docs" / "OPERATIONS.md"
PRODUCT = PROJECT_ROOT / "docs" / "PRODUCT.md"

SOURCE_COMMIT = "ATELIER2_SOURCE_COMMIT"
SOURCE_TREE = "ATELIER2_SOURCE_TREE"
DIRTY_TREE_REFUSAL = "container up: source tree must be clean"
PROJECT_NAME = re.compile(r"^atelier2-[0-9a-f]{16}$")

_FROM = re.compile(r"^FROM\s+", re.MULTILINE | re.IGNORECASE)
_USER = re.compile(r"^USER\s+(\S+)\s*$", re.MULTILINE | re.IGNORECASE)


def final_user(recipe: str) -> str:
    stages = list(_FROM.finditer(recipe))
    assert stages, "image recipe declares no stage"
    users = list(_USER.finditer(recipe[stages[-1].end() :]))
    assert users, "image recipe declares no final USER"
    return users[-1].group(1).split(":", 1)[0]


def source_labels() -> dict[str, str]:
    return {
        "atelier2.source.commit": "${ATELIER2_SOURCE_COMMIT:?source commit identity is missing}",
        "atelier2.source.tree": "${ATELIER2_SOURCE_TREE:?source tree identity is missing}",
    }


def assert_provider_free_recipe(recipe: str) -> None:
    assert final_user(recipe) == "atelier2", "image ends under another user"
    lowered = recipe.lower()
    for forbidden in (
        "claude",
        "codex",
        "grok",
        "credential",
        "scratch",
        "systemd",
        "dbus",
    ):
        assert forbidden not in lowered, f"image admits {forbidden}"
    assert "atelier2.source.commit" in recipe
    assert "atelier2.source.tree" in recipe
    assert "LABEL atelier2.source.commit=${ATELIER2_SOURCE_COMMIT}" in recipe
    assert "atelier2.source.tree=${ATELIER2_SOURCE_TREE}" in recipe
    runtime = recipe[list(_FROM.finditer(recipe))[-1].end() :]
    assert "groupadd --gid 10001 atelier2" in runtime
    assert (
        "useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin atelier2"
        in runtime
    )
    assert (
        "ENV PATH=/app/.venv/bin:/usr/local/bin:/usr/bin:/bin \\\n"
        "    ATELIER2_SOURCE_COMMIT=${ATELIER2_SOURCE_COMMIT} \\\n"
        "    ATELIER2_SOURCE_TREE=${ATELIER2_SOURCE_TREE}"
    ) in runtime
    for required in (
        "COPY frontend/package.json frontend/package-lock.json ./",
        "RUN npm ci --no-audit --no-fund",
        "COPY frontend/ ./",
        "RUN npm run build",
        "COPY pyproject.toml uv.lock README.md ./",
        "RUN uv sync --locked --no-dev --no-install-project",
        "COPY src/ ./src/",
        "COPY --from=frontend /build/frontend/dist ./frontend/dist",
        "RUN uv sync --locked --no-dev",
        "install --directory --owner atelier2 --group atelier2 --mode 0700 /var/lib/atelier2/store",
        "COPY scripts/container_serve.sh /app/container_serve.sh",
        "RUN chmod 0755 /app/container_serve.sh",
        'ENTRYPOINT ["/app/container_serve.sh"]',
    ):
        assert required in recipe, f"image omits {required}"


def assert_isolated_compose(document: dict[str, Any]) -> None:
    assert "name" not in document, "candidate fixes a Compose project name"
    services = document["services"]
    assert list(services) == ["serve"], "candidate declares more than Serve"
    service = services["serve"]
    assert set(service) == {
        "build",
        "cap_drop",
        "read_only",
        "security_opt",
        "healthcheck",
        "ports",
        "volumes",
        "networks",
        "labels",
    }
    assert "container_name" not in service
    assert "network_mode" not in service
    assert "privileged" not in service
    assert "cap_add" not in service
    assert "user" not in service
    assert "environment" not in service
    assert "env_file" not in service
    assert service["cap_drop"] == ["ALL"]
    assert service["read_only"] is True
    assert "no-new-privileges:true" in service["security_opt"]
    assert service["security_opt"] == ["no-new-privileges:true"]
    assert len(service["ports"]) == 1
    port = service["ports"][0]
    assert port == {
        "target": 8422,
        "published": "0",
        "host_ip": "127.0.0.1",
        "protocol": "tcp",
    }
    assert service["healthcheck"] == {
        "test": [
            "CMD",
            "python",
            "-c",
            "import json, os; from urllib.request import urlopen; health = json.load(urlopen('http://127.0.0.1:8422/atelier/api/v1/health', timeout=2)); raise SystemExit(0 if health == {'status': 'serving', 'source_commit': os.environ['ATELIER2_SOURCE_COMMIT'], 'source_tree': os.environ['ATELIER2_SOURCE_TREE']} else 1)",
        ],
        "interval": "1s",
        "timeout": "2s",
        "retries": 15,
        "start_period": "2s",
    }
    mounts = service["volumes"]
    assert mounts == [
        {
            "type": "volume",
            "source": "store",
            "target": "/var/lib/atelier2/store",
            "volume": {"nocopy": False},
        }
    ]
    assert service["networks"] == ["serve"]
    assert set(document["volumes"]) == {"store"}
    assert set(document["networks"]) == {"serve"}
    assert document["volumes"]["store"] == {"labels": service["labels"]}
    assert document["networks"]["serve"] == {"labels": service["labels"]}
    expected_labels = source_labels()
    for labels in (
        service["labels"],
        document["volumes"]["store"]["labels"],
        document["networks"]["serve"]["labels"],
    ):
        assert labels == expected_labels
    assert service["build"] == {
        "context": ".",
        "dockerfile": "Dockerfile",
        "args": {
            SOURCE_COMMIT: expected_labels["atelier2.source.commit"],
            SOURCE_TREE: expected_labels["atelier2.source.tree"],
        },
    }


def assert_teardown_descriptor(document: dict[str, Any], project: str) -> None:
    assert document == {
        "name": project,
        "services": {
            "serve": {
                "build": ".",
                "volumes": [
                    {
                        "type": "volume",
                        "source": "store",
                        "target": "/var/lib/atelier2/store",
                        "volume": {"nocopy": False},
                    }
                ],
                "networks": ["serve"],
                "labels": source_labels(),
            }
        },
        "volumes": {"store": {"labels": source_labels()}},
        "networks": {"serve": {"labels": source_labels()}},
    }


def write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def install_docker_stub(directory: Path) -> None:
    write_stub(
        directory / "docker",
        """\
import json
import os
import signal
import shutil
import sys
import time
from pathlib import Path

record = Path(os.environ["ATELIER2_TEST_DOCKER_RECORD"])
with record.open("a", encoding="utf-8") as output:
    output.write(json.dumps(sys.argv[1:]) + "\\n")
if "down" in sys.argv:
    descriptor = Path(sys.argv[sys.argv.index("-f") + 1])
    project = sys.argv[sys.argv.index("--project-name") + 1]
    if not descriptor.is_file() or not descriptor.read_text(encoding="utf-8").startswith(f"name: {project}\\n"):
        raise SystemExit(20)
if "build" in sys.argv:
    mutation = os.environ.get("ATELIER2_TEST_MUTATE_AFTER_PREFLIGHT")
    if mutation:
        Path(mutation).write_text("changed after preflight\\n", encoding="utf-8")
    snapshot = Path(os.environ["ATELIER2_TEST_CONTEXT"])
    compose_file = Path(sys.argv[sys.argv.index("-f") + 1])
    shutil.copytree(compose_file.parent, snapshot)
if sys.argv[-3:] == ["port", "serve", "8422"]:
    status = int(os.environ.get("ATELIER2_TEST_PORT_STATUS", "0"))
    if status:
        raise SystemExit(status)
    print(os.environ.get("ATELIER2_TEST_PORT_OUTPUT", "127.0.0.1:49152"))
wait_phases = set(filter(None, os.environ.get("ATELIER2_TEST_WAIT_PHASES", "").split(",")))
for wait_phase in ("build", "up", "down"):
    if wait_phase not in wait_phases or wait_phase not in sys.argv:
        continue
    ready = Path(os.environ["ATELIER2_TEST_READY_DIRECTORY"]) / f"{wait_phase}-ready"
    ready.touch()
    if wait_phase == "down":
        for interruption in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
            signal.signal(interruption, signal.SIG_IGN)
        release = Path(os.environ["ATELIER2_TEST_DOWN_RELEASE"])
        while not release.exists():
            time.sleep(0.01)
    else:
        signal.pause()
if "up" in sys.argv:
    status = int(os.environ.get("ATELIER2_TEST_UP_STATUS", "0"))
    if status:
        raise SystemExit(status)
if sys.argv[-5:] == ["down", "--volumes", "--rmi", "local", "--remove-orphans"] and os.environ.get("ATELIER2_TEST_DOWN_FAILURE") == "1":
    raise SystemExit(19)
if sys.argv[-5:] == ["down", "--volumes", "--rmi", "local", "--remove-orphans"] and os.environ.get("ATELIER2_TEST_DESCRIPTOR_REMOVE_FAILURE") == "1":
    Path(sys.argv[sys.argv.index("-f") + 1]).parent.chmod(0o500)
""",
    )


def install_git_stub(directory: Path) -> None:
    real_git = shutil.which("git")
    assert real_git is not None
    write_stub(
        directory / "git",
        f"""\\
import os
import sys

if os.environ.get("ATELIER2_TEST_GIT_STATUS_FAILURE") == "1" and "status" in sys.argv:
    raise SystemExit(17)
if os.environ.get("ATELIER2_TEST_GIT_ARCHIVE_FAILURE") == "1" and "archive" in sys.argv:
    raise SystemExit(18)
os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
""",
    )


def run_git(repository: Path, *arguments: str) -> str:
    environment = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "packaging",
        "GIT_AUTHOR_EMAIL": "packaging@invalid",
        "GIT_COMMITTER_NAME": "packaging",
        "GIT_COMMITTER_EMAIL": "packaging@invalid",
    }
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def packaging_repository(tmp_path: Path, name: str = "repository") -> Path:
    repository = tmp_path / name
    (repository / "scripts").mkdir(parents=True)
    for source, destination in (
        (CONTAINER_UP, repository / "scripts" / "container_up.sh"),
        (CONTAINER_SERVE, repository / "scripts" / "container_serve.sh"),
        (DOCKERFILE, repository / "Dockerfile"),
        (COMPOSE, repository / "compose.yaml"),
        (DOCKERIGNORE, repository / ".dockerignore"),
    ):
        shutil.copy2(source, destination)
    (repository / "payload.txt").write_text("committed\n", encoding="utf-8")
    (repository / "frontend").mkdir()
    (repository / "frontend" / "marker.txt").write_text("committed\n", encoding="utf-8")
    run_git(repository, "init", "--quiet", "--initial-branch=main")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "--quiet", "--message", "fixture")
    return repository


def container_environment(
    repository: Path,
    tmp_path: Path,
    *,
    port_output: str = "127.0.0.1:49152",
    port_status: int = 0,
    up_status: int = 0,
    down_fails: bool = False,
    descriptor_remove_fails: bool = False,
    git_status_fails: bool = False,
    archive_fails: bool = False,
    mutate_after_preflight: bool = False,
    wait_phases: tuple[str, ...] = (),
) -> dict[str, str]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(exist_ok=True)
    install_docker_stub(bin_directory)
    install_git_stub(bin_directory)
    record = tmp_path / "docker.jsonl"
    return {
        **os.environ,
        "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        "ATELIER2_TEST_DOCKER_RECORD": str(record),
        "ATELIER2_TEST_CONTEXT": str(tmp_path / "docker-context"),
        "ATELIER2_TEST_PORT_OUTPUT": port_output,
        "ATELIER2_TEST_PORT_STATUS": str(port_status),
        "ATELIER2_TEST_UP_STATUS": str(up_status),
        "ATELIER2_TEST_DOWN_FAILURE": "1" if down_fails else "0",
        "ATELIER2_TEST_DESCRIPTOR_REMOVE_FAILURE": (
            "1" if descriptor_remove_fails else "0"
        ),
        "ATELIER2_TEST_GIT_STATUS_FAILURE": "1" if git_status_fails else "0",
        "ATELIER2_TEST_GIT_ARCHIVE_FAILURE": "1" if archive_fails else "0",
        "ATELIER2_TEST_MUTATE_AFTER_PREFLIGHT": (
            str(repository / "frontend" / "marker.txt")
            if mutate_after_preflight
            else ""
        ),
        "ATELIER2_TEST_WAIT_PHASES": ",".join(wait_phases),
        "ATELIER2_TEST_READY_DIRECTORY": str(tmp_path),
        "ATELIER2_TEST_DOWN_RELEASE": str(tmp_path / "docker-down-release"),
        "TMPDIR": str(tmp_path),
    }


def run_container_up(
    repository: Path,
    tmp_path: Path,
    **settings: Any,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(repository / "scripts" / "container_up.sh")],
        cwd=repository,
        env=container_environment(repository, tmp_path, **settings),
        capture_output=True,
        text=True,
        check=False,
    )


def docker_invocations(tmp_path: Path) -> list[list[str]]:
    record = tmp_path / "docker.jsonl"
    return (
        [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
        if record.exists()
        else []
    )


def project_from(arguments: list[str]) -> str:
    index = arguments.index("--project-name")
    return arguments[index + 1]


def snapshot_from(arguments: list[str]) -> Path:
    index = arguments.index("--project-directory")
    return Path(arguments[index + 1])


def lifecycle_directories(tmp_path: Path) -> list[Path]:
    return list(tmp_path.glob("atelier2-lifecycle.*"))


def test_recipe_is_provider_free_and_unprivileged() -> None:
    assert_provider_free_recipe(DOCKERFILE.read_text(encoding="utf-8"))


def test_recipe_guard_rejects_provider_or_privilege_mutations() -> None:
    recipe = DOCKERFILE.read_text(encoding="utf-8")
    for mutation in (
        recipe + "\nUSER root\n",
        recipe + "\nUSER nobody\n",
        recipe + "\nRUN echo claude\n",
        recipe.replace("COPY frontend/ ./", "COPY no-frontend/ ./"),
        recipe.replace("COPY src/ ./src/", "COPY no-source/ ./src/"),
        recipe.replace(
            "LABEL atelier2.source.commit=${ATELIER2_SOURCE_COMMIT}",
            "LABEL atelier2.source.commit=fixed",
        ),
        recipe.replace(
            "ATELIER2_SOURCE_COMMIT=${ATELIER2_SOURCE_COMMIT}",
            "ATELIER2_SOURCE_COMMIT=stale",
        ),
        recipe.replace(
            "ATELIER2_SOURCE_TREE=${ATELIER2_SOURCE_TREE}",
            "ATELIER2_SOURCE_TREE=stale",
        ),
        recipe.replace("groupadd --gid 10001 atelier2", "groupadd --gid 0 atelier2"),
        recipe.replace(
            "useradd --uid 10001 --gid 10001", "useradd --uid 0 --gid 10001"
        ),
        recipe.replace(
            "useradd --uid 10001 --gid 10001", "useradd --uid 10002 --gid 10001"
        ),
        recipe.replace("--no-create-home", "--non-unique --no-create-home"),
        recipe.replace("groupadd --gid 10001 atelier2", "true"),
        recipe.replace(
            "useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin atelier2",
            "true",
        ),
    ):
        with pytest.raises(AssertionError):
            assert_provider_free_recipe(mutation)


def test_ignore_excludes_provider_configuration_and_build_noise() -> None:
    ignored = DOCKERIGNORE.read_text(encoding="utf-8")
    for required in (
        ".env",
        ".credentials.json",
        ".claude",
        ".codex",
        ".grok",
        "tests",
    ):
        assert required in ignored


def test_compose_is_one_isolated_serve_candidate() -> None:
    assert_isolated_compose(yaml.safe_load(COMPOSE.read_text(encoding="utf-8")))


def test_compose_guard_rejects_authority_and_static_resource_mutations() -> None:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    mutations = []
    for key, value in (
        ("privileged", True),
        ("container_name", "atelier2"),
        ("network_mode", "host"),
        ("cap_add", ["NET_ADMIN"]),
        ("user", "root"),
        ("user", "nobody"),
        ("environment", {"PROVIDER_TOKEN": "secret"}),
        ("env_file", [".env"]),
    ):
        mutated = copy.deepcopy(document)
        mutated["services"]["serve"][key] = value
        mutations.append(mutated)
    static_port = copy.deepcopy(document)
    static_port["services"]["serve"]["ports"][0]["published"] = "8422"
    mutations.append(static_port)
    broad_port = copy.deepcopy(document)
    broad_port["services"]["serve"]["ports"].append(
        {"target": 8423, "published": "0", "host_ip": "0.0.0.0", "protocol": "tcp"}
    )
    mutations.append(broad_port)
    weakened_security = copy.deepcopy(document)
    weakened_security["services"]["serve"]["security_opt"].append("seccomp:unconfined")
    mutations.append(weakened_security)
    hardcoded_label = copy.deepcopy(document)
    hardcoded_label["services"]["serve"]["labels"]["atelier2.source.commit"] = "fixed"
    mutations.append(hardcoded_label)
    host_mount = copy.deepcopy(document)
    host_mount["services"]["serve"]["volumes"] = [
        {"type": "bind", "source": "/host", "target": "/var/lib/atelier2/store"}
    ]
    mutations.append(host_mount)
    extra_volume = copy.deepcopy(document)
    extra_volume["services"]["serve"]["volumes"].append(
        {"type": "volume", "source": "other", "target": "/other"}
    )
    mutations.append(extra_volume)
    extra_network = copy.deepcopy(document)
    extra_network["networks"]["outside"] = {"external": True}
    mutations.append(extra_network)
    external_network = copy.deepcopy(document)
    external_network["networks"]["serve"]["external"] = True
    mutations.append(external_network)
    unhealthy = copy.deepcopy(document)
    unhealthy["services"]["serve"]["healthcheck"]["test"][-1] = "raise SystemExit(1)"
    mutations.append(unhealthy)
    without_healthcheck = copy.deepcopy(document)
    del without_healthcheck["services"]["serve"]["healthcheck"]
    mutations.append(without_healthcheck)

    for mutated in mutations:
        with pytest.raises(AssertionError):
            assert_isolated_compose(mutated)


def test_serve_has_no_provider_or_runner_vector() -> None:
    serve = CONTAINER_SERVE.read_text(encoding="utf-8")
    assert "--host 0.0.0.0" in serve
    assert "--port 8422" in serve
    for forbidden in ("claude", "codex", "grok", "credential", "scratch", "runner"):
        assert forbidden not in serve.lower()


def test_clean_tree_starts_one_random_project_and_prints_scoped_teardown(
    tmp_path: Path,
) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(repository, tmp_path)

    assert completed.returncode == 0, completed.stderr
    invocations = docker_invocations(tmp_path)
    assert len(invocations) == 3
    project = project_from(invocations[0])
    assert PROJECT_NAME.fullmatch(project)
    assert all(project_from(arguments) == project for arguments in invocations)
    assert invocations[0][-1] == "build"
    assert invocations[1][-6:] == [
        "up",
        "--detach",
        "--wait",
        "--wait-timeout",
        "30",
        "--no-build",
    ]
    assert invocations[2][-3:] == ["port", "serve", "8422"]
    assert (
        completed.stdout.splitlines()[0]
        == "container up: cockpit -> http://127.0.0.1:49152/atelier/"
    )
    command = shlex.split(
        completed.stdout.splitlines()[1].removeprefix("container up: stop -> ")
    )
    assert command[:8] == [
        f"{SOURCE_COMMIT}={run_git(repository, 'rev-parse', 'HEAD')}",
        f"{SOURCE_TREE}={run_git(repository, 'rev-parse', 'HEAD^{tree}')}",
        "docker",
        "compose",
        "--project-name",
        project,
        "-f",
        command[7],
    ]
    assert command[8:] == [
        "down",
        "--volumes",
        "--rmi",
        "local",
        "--remove-orphans",
        "&&",
        "rm",
        "-f",
        "--",
        command[7],
        "&&",
        "rmdir",
        "--",
        str(Path(command[7]).parent),
    ]
    descriptor = Path(command[7])
    assert descriptor.is_file()
    assert descriptor.stat().st_mode & 0o777 == 0o600
    teardown = yaml.safe_load(descriptor.read_text(encoding="utf-8"))
    assert_teardown_descriptor(teardown, project)
    for reference in ("volumes", "networks"):
        incomplete = copy.deepcopy(teardown)
        del incomplete["services"]["serve"][reference]
        with pytest.raises(AssertionError):
            assert_teardown_descriptor(incomplete, project)
    assert not snapshot_from(invocations[0]).exists()
    assert "prune" not in CONTAINER_UP.read_text(encoding="utf-8")


def test_teardown_descriptor_renders_only_the_candidate_resources(
    tmp_path: Path,
) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(repository, tmp_path)

    assert completed.returncode == 0, completed.stderr
    command = shlex.split(
        completed.stdout.splitlines()[1].removeprefix("container up: stop -> ")
    )
    project = command[5]
    descriptor = command[7]
    rendered = subprocess.run(
        [
            "docker",
            "compose",
            "--project-name",
            project,
            "-f",
            descriptor,
            "config",
            "--format",
            "json",
        ],
        env={
            **os.environ,
            SOURCE_COMMIT: run_git(repository, "rev-parse", "HEAD"),
            SOURCE_TREE: run_git(repository, "rev-parse", "HEAD^{tree}"),
        },
        capture_output=True,
        check=False,
        text=True,
    )

    assert rendered.returncode == 0, rendered.stderr
    document = json.loads(rendered.stdout)
    assert document["volumes"]["store"]["name"] == f"{project}_store"
    assert document["networks"]["serve"]["name"] == f"{project}_serve"
    assert set(document["networks"]) == {"serve"}
    assert document["services"]["serve"]["volumes"] == [
        {
            "type": "volume",
            "source": "store",
            "target": "/var/lib/atelier2/store",
            "volume": {},
        }
    ]
    assert set(document["services"]["serve"]["networks"]) == {"serve"}


def test_dirty_or_untracked_source_is_refused_before_docker(tmp_path: Path) -> None:
    repository = packaging_repository(tmp_path)
    (repository / "ambient.txt").write_text("not committed\n", encoding="utf-8")

    completed = run_container_up(repository, tmp_path)

    assert completed.returncode != 0
    assert DIRTY_TREE_REFUSAL in completed.stderr
    assert docker_invocations(tmp_path) == []


def test_failed_start_tears_down_only_the_generated_project(tmp_path: Path) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(repository, tmp_path, port_status=23)

    assert completed.returncode == 23
    invocations = docker_invocations(tmp_path)
    project = project_from(invocations[0])
    assert all(project_from(arguments) == project for arguments in invocations)
    assert invocations[-1][-5:] == [
        "down",
        "--volumes",
        "--rmi",
        "local",
        "--remove-orphans",
    ]
    assert not snapshot_from(invocations[0]).exists()
    assert lifecycle_directories(tmp_path) == []


def test_unhealthy_or_exited_wait_refuses_and_tears_down(tmp_path: Path) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(repository, tmp_path, up_status=42)

    assert completed.returncode == 42
    assert docker_invocations(tmp_path)[-1][-5:] == [
        "down",
        "--volumes",
        "--rmi",
        "local",
        "--remove-orphans",
    ]


def test_status_failure_refuses_before_snapshot_or_docker(tmp_path: Path) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(repository, tmp_path, git_status_fails=True)

    assert completed.returncode != 0
    assert "source status is unavailable" in completed.stderr
    assert docker_invocations(tmp_path) == []


def test_archive_failure_removes_early_lifecycle_state(tmp_path: Path) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(repository, tmp_path, archive_fails=True)

    assert completed.returncode != 0
    assert docker_invocations(tmp_path) == []
    assert lifecycle_directories(tmp_path) == []


def test_snapshot_uses_committed_bytes_after_preflight(tmp_path: Path) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(repository, tmp_path, mutate_after_preflight=True)

    assert completed.returncode == 0, completed.stderr
    assert (repository / "frontend" / "marker.txt").read_text(
        encoding="utf-8"
    ) == "changed after preflight\n"
    assert (tmp_path / "docker-context" / "frontend" / "marker.txt").read_text(
        encoding="utf-8"
    ) == "committed\n"
    assert not snapshot_from(docker_invocations(tmp_path)[0]).exists()


def test_snapshot_proof_bites_when_build_context_uses_mutable_checkout(
    tmp_path: Path,
) -> None:
    repository = packaging_repository(tmp_path)
    script = repository / "scripts" / "container_up.sh"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            '-f "${snapshot}/compose.yaml"', '-f "${repository}/compose.yaml"'
        ),
        encoding="utf-8",
    )
    run_git(repository, "add", "scripts/container_up.sh")
    run_git(repository, "commit", "--quiet", "--message", "break snapshot context")

    completed = run_container_up(repository, tmp_path, mutate_after_preflight=True)

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "docker-context" / "frontend" / "marker.txt").read_text(
        encoding="utf-8"
    ) == "changed after preflight\n"


@pytest.mark.parametrize(
    "address",
    ("", "0.0.0.0:49152", "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:49152 extra"),
)
def test_invalid_port_output_refuses_and_tears_down(
    tmp_path: Path, address: str
) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(repository, tmp_path, port_output=address)

    assert completed.returncode != 0
    assert "invalid loopback port" in completed.stderr
    assert docker_invocations(tmp_path)[-1][-5:] == [
        "down",
        "--volumes",
        "--rmi",
        "local",
        "--remove-orphans",
    ]


def test_cleanup_failure_prints_replayable_exact_teardown(tmp_path: Path) -> None:
    repository = packaging_repository(tmp_path, "repository with ; metacharacters")

    completed = run_container_up(repository, tmp_path, port_status=23, down_fails=True)

    assert completed.returncode == 23
    command = completed.stderr.split("container up: cleanup failed; run: ", 1)[
        1
    ].strip()
    parsed = shlex.split(command)
    down = parsed.index("down")
    assert parsed[down : down + 5] == [
        "down",
        "--volumes",
        "--rmi",
        "local",
        "--remove-orphans",
    ]
    descriptor = Path(parsed[parsed.index("-f") + 1])
    assert descriptor.is_file()
    environment = container_environment(repository, tmp_path)
    environment.pop(SOURCE_COMMIT, None)
    environment.pop(SOURCE_TREE, None)
    replay = subprocess.run(["bash", "-c", command], env=environment, check=False)
    assert replay.returncode == 0
    assert not descriptor.exists()
    assert not descriptor.parent.exists()
    invocations = docker_invocations(tmp_path)
    project = project_from(invocations[0])
    assert {project_from(arguments) for arguments in invocations} == {project}


def test_descriptor_removal_failure_keeps_recovery_state(tmp_path: Path) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(
        repository, tmp_path, port_status=23, descriptor_remove_fails=True
    )

    assert completed.returncode == 23
    command = completed.stderr.split(
        "container up: lifecycle descriptor cleanup failed; run: ", 1
    )[1].strip()
    descriptor = Path(shlex.split(command)[7])
    assert descriptor.is_file()
    assert not snapshot_from(docker_invocations(tmp_path)[0]).exists()
    descriptor.parent.chmod(0o700)
    environment = container_environment(repository, tmp_path)
    environment.pop(SOURCE_COMMIT, None)
    environment.pop(SOURCE_TREE, None)
    replay = subprocess.run(["bash", "-c", command], env=environment, check=False)
    assert replay.returncode == 0
    assert not descriptor.exists()
    assert not descriptor.parent.exists()


def test_printed_stop_command_replays_without_ambient_identity(tmp_path: Path) -> None:
    repository = packaging_repository(tmp_path, "repository with ; metacharacters")
    completed = run_container_up(repository, tmp_path)

    assert completed.returncode == 0, completed.stderr
    command = completed.stdout.splitlines()[1].removeprefix("container up: stop -> ")
    descriptor = Path(shlex.split(command)[7])
    (repository / "compose.yaml").rename(repository / "obsolete-compose.yaml")
    environment = container_environment(repository, tmp_path)
    environment.pop(SOURCE_COMMIT, None)
    environment.pop(SOURCE_TREE, None)
    replay = subprocess.run(["bash", "-c", command], env=environment, check=False)
    assert replay.returncode == 0
    assert not descriptor.exists()
    assert not descriptor.parent.exists()
    invocations = docker_invocations(tmp_path)
    project = project_from(invocations[0])
    assert {project_from(arguments) for arguments in invocations} == {project}


@pytest.mark.parametrize(
    ("phase", "interruption", "status"),
    (
        ("build", signal.SIGHUP, 129),
        ("up", signal.SIGINT, 130),
        ("up", signal.SIGTERM, 143),
    ),
)
def test_signals_preserve_status_and_teardown_exact_project(
    tmp_path: Path, phase: str, interruption: signal.Signals, status: int
) -> None:
    repository = packaging_repository(tmp_path)
    environment = container_environment(repository, tmp_path, wait_phases=(phase,))
    process = subprocess.Popen(
        ["bash", str(repository / "scripts" / "container_up.sh")],
        cwd=repository,
        env=environment,
        start_new_session=True,
    )
    ready = tmp_path / f"{phase}-ready"
    deadline = time.monotonic() + 5
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), "Docker stub did not reach the launch boundary"
    os.killpg(process.pid, interruption)
    assert process.wait(timeout=5) == status
    invocations = docker_invocations(tmp_path)
    project = project_from(invocations[0])
    assert all(project_from(arguments) == project for arguments in invocations)
    assert invocations[-1][-5:] == [
        "down",
        "--volumes",
        "--rmi",
        "local",
        "--remove-orphans",
    ]
    assert not snapshot_from(invocations[0]).exists()
    assert lifecycle_directories(tmp_path) == []


def test_repeated_signal_during_cleanup_preserves_first_signal_status(
    tmp_path: Path,
) -> None:
    repository = packaging_repository(tmp_path)
    environment = container_environment(
        repository, tmp_path, wait_phases=("up", "down")
    )
    process = subprocess.Popen(
        ["bash", str(repository / "scripts" / "container_up.sh")],
        cwd=repository,
        env=environment,
        start_new_session=True,
    )
    up_ready = tmp_path / "up-ready"
    deadline = time.monotonic() + 5
    while not up_ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert up_ready.exists(), "Docker stub did not reach the launch boundary"
    os.killpg(process.pid, signal.SIGINT)
    down_ready = tmp_path / "down-ready"
    while not down_ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert down_ready.exists(), "Docker stub did not reach cleanup"
    os.killpg(process.pid, signal.SIGTERM)
    (tmp_path / "docker-down-release").touch()
    assert process.wait(timeout=5) == 130
    invocations = docker_invocations(tmp_path)
    project = project_from(invocations[0])
    assert {project_from(arguments) for arguments in invocations} == {project}
    assert invocations[-1][-5:] == [
        "down",
        "--volumes",
        "--rmi",
        "local",
        "--remove-orphans",
    ]
    assert not snapshot_from(invocations[0]).exists()
    assert lifecycle_directories(tmp_path) == []


def test_clean_tree_refusal_bites_when_its_preflight_is_removed(tmp_path: Path) -> None:
    repository = packaging_repository(tmp_path)
    script = repository / "scripts" / "container_up.sh"
    script.write_text(
        script.read_text(encoding="utf-8").replace(
            'git -C "${repository}" status --porcelain --untracked-files=all',
            "printf ''",
        ),
        encoding="utf-8",
    )
    (repository / "ambient.txt").write_text("not committed\n", encoding="utf-8")

    completed = run_container_up(repository, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert docker_invocations(tmp_path), "mutation should expose dirty source to Docker"


def test_docs_state_the_candidate_boundary() -> None:
    operations = OPERATIONS.read_text(encoding="utf-8")
    product = PRODUCT.read_text(encoding="utf-8")
    for text in (operations, product):
        assert "provider-free" in text
        assert "Runner" in text
        assert "loopback" in text
        assert "archiv" in text
        assert "health" in text
    for predecessor in (
        "ATELIER2_CLAUDE_CREDENTIALS",
        "atelier2-live.service",
        "host network",
        "systemctl",
    ):
        assert predecessor.lower() not in operations.lower()
