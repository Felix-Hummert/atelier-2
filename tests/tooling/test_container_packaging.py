from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from atelier2.adapters.claude_subscription import (
    CONFORMANT_CLAUDE_VERSIONS,
    CREDENTIAL_RECORD_ENTRY,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = PROJECT_ROOT / "Dockerfile"
COMPOSE = PROJECT_ROOT / "compose.yaml"
DOCKERIGNORE = PROJECT_ROOT / ".dockerignore"
CONTAINER_UP = PROJECT_ROOT / "scripts" / "container_up.sh"
CONTAINER_SERVE = PROJECT_ROOT / "scripts" / "container_serve.sh"
CI_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
FOUNDATION_WORKFLOW = PROJECT_ROOT / ".github" / "workflows" / "foundation.yml"
OPERATIONS = PROJECT_ROOT / "docs" / "OPERATIONS.md"
DOCUMENTATION_MAP = PROJECT_ROOT / "docs" / "README.md"

CONTAINER_SCRATCH_ROOT = "/var/lib/atelier2/scratch"
CONTAINER_CREDENTIAL_DIRECTORY = "/run/atelier2/claude"
CONTAINER_CLAUDE_EXECUTABLE = "/usr/local/bin/claude"
LISTEN_ADDRESS = "127.0.0.1"
LISTEN_PORT = "8422"
HOST_STATE_ROOT = "${XDG_STATE_HOME:-$HOME/.local/state}/atelier2"
LIVE_UNIT = "atelier2-live.service"

CONFORMANT_CLAUDE_VERSION = ".".join(
    str(part) for part in max(CONFORMANT_CLAUDE_VERSIONS)
)

SOURCE_COMMIT = "ATELIER2_SOURCE_COMMIT"
SOURCE_TREE = "ATELIER2_SOURCE_TREE"
UNKNOWN_IDENTITY = "unknown"
COMMITTED_PAYLOAD_NAME = "payload.txt"
COMMITTED_PAYLOAD = "committed-payload-bytes\n"
DIRTY_TRACKED_PAYLOAD = "dirty-tracked-bytes\n"
UNTRACKED_NAME = "ambient-untracked.txt"
UNTRACKED_PAYLOAD = "untracked-ambient-bytes\n"
MISSING_COMMIT_REFUSAL = "container up: source commit identity is missing"
UNKNOWN_COMMIT_REFUSAL = "container up: source commit identity is unknown"
UNKNOWN_TREE_REFUSAL = "container up: source tree identity is unknown"
FOREIGN_TREE_REFUSAL = "container up: source tree does not belong to source commit"

_ISOLATED_GIT = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "packaging",
    "GIT_AUTHOR_EMAIL": "packaging@invalid",
    "GIT_COMMITTER_NAME": "packaging",
    "GIT_COMMITTER_EMAIL": "packaging@invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}

_FROM_INSTRUCTION = re.compile(r"^FROM\s+", re.MULTILINE | re.IGNORECASE)
_USER_INSTRUCTION = re.compile(r"^USER\s+(\S+)\s*$", re.MULTILINE | re.IGNORECASE)
_PRIVILEGED_USERS = frozenset({"root", "0"})
_NO_USER_MESSAGE = "image recipe declares no USER"
_PRIVILEGED_USER_MESSAGE = "image recipe ends as privileged USER"


def last_user_name(recipe: str) -> str:
    last_from = None
    for match in _FROM_INSTRUCTION.finditer(recipe):
        last_from = match
    last_stage = recipe[last_from.end() :] if last_from is not None else ""
    declared = [
        match.group(1).split(":", 1)[0]
        for match in _USER_INSTRUCTION.finditer(last_stage)
    ]
    assert declared, _NO_USER_MESSAGE
    return declared[-1]


def assert_recipe_runs_unprivileged(recipe: str) -> None:
    user = last_user_name(recipe)
    assert user not in _PRIVILEGED_USERS, f"{_PRIVILEGED_USER_MESSAGE} {user}"


def isolated_git_environment() -> dict[str, str]:
    environment = {**os.environ, **_ISOLATED_GIT}
    environment.pop("GIT_DIR", None)
    environment.pop("GIT_WORK_TREE", None)
    environment.pop("GIT_INDEX_FILE", None)
    environment.pop("GIT_OBJECT_DIRECTORY", None)
    return environment


def run_git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        env=isolated_git_environment(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def install_command_stubs(bin_directory: Path) -> None:
    write_stub(
        bin_directory / "docker",
        """\
import json
import os
import shutil
import sys
from pathlib import Path

record_root = Path(os.environ["ATELIER2_TEST_DOCKER_RECORD"])
record_root.mkdir(parents=True, exist_ok=True)
argv = sys.argv[1:]
payload = {
    "argv": argv,
    "cwd": os.getcwd(),
    "source_commit": os.environ.get("ATELIER2_SOURCE_COMMIT"),
    "source_tree": os.environ.get("ATELIER2_SOURCE_TREE"),
}
with (record_root / "invocations.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps(payload) + "\\n")
if argv[:2] == ["compose", "build"]:
    snapshot = record_root / "context"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    shutil.copytree(os.getcwd(), snapshot, ignore_dangling_symlinks=True)
raise SystemExit(0)
""",
    )
    write_stub(
        bin_directory / "systemctl",
        """\
raise SystemExit(1)
""",
    )
    real_git = shutil.which("git")
    assert real_git is not None
    write_stub(
        bin_directory / "git",
        f"""\
import os
import subprocess
import sys

real_git = {real_git!r}
mode = os.environ.get("ATELIER2_TEST_GIT_MODE", "passthrough")
args = sys.argv[1:]
if mode != "passthrough" and "rev-parse" in args:
    revision = args[-1]
    if mode == "unknown-commit" and "tree" not in revision:
        print("unknown")
        raise SystemExit(0)
    if mode == "unknown-tree" and "tree" in revision:
        print("unknown")
        raise SystemExit(0)
    if mode == "unknown-object" and revision.startswith("HEAD"):
        print("0" * 40)
        raise SystemExit(0)
    if mode == "mismatch" and revision in ("HEAD^{{tree}}", "HEAD^tree"):
        print(os.environ["ATELIER2_TEST_FOREIGN_TREE"])
        raise SystemExit(0)
raise SystemExit(subprocess.call([real_git, *args]))
""",
    )


def packaging_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(CONTAINER_UP, scripts / "container_up.sh")
    shutil.copy2(COMPOSE, repository / "compose.yaml")
    shutil.copy2(DOCKERFILE, repository / "Dockerfile")
    shutil.copy2(DOCKERIGNORE, repository / ".dockerignore")
    (repository / COMMITTED_PAYLOAD_NAME).write_text(
        COMMITTED_PAYLOAD, encoding="utf-8"
    )
    run_git(repository, "init", "--quiet", "--initial-branch=main")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "--quiet", "--message", "packaging-fixture")
    return repository


def run_container_up(
    repository: Path,
    tmp_path: Path,
    *,
    git_mode: str = "passthrough",
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bin_directory = tmp_path / "bin"
    record_root = tmp_path / "docker-record"
    home = tmp_path / "home"
    tmpdir = tmp_path / "tmp"
    state = tmp_path / "state"
    credentials = tmp_path / ".credentials.json"
    bin_directory.mkdir(exist_ok=True)
    record_root.mkdir(exist_ok=True)
    home.mkdir(exist_ok=True)
    tmpdir.mkdir(exist_ok=True)
    credentials.write_text("{}\n", encoding="utf-8")
    install_command_stubs(bin_directory)
    environment = isolated_git_environment()
    environment.pop(SOURCE_COMMIT, None)
    environment.pop(SOURCE_TREE, None)
    environment.update(
        {
            "PATH": f"{bin_directory}{os.pathsep}{environment.get('PATH', '')}",
            "HOME": str(home),
            "TMPDIR": str(tmpdir),
            "ATELIER2_STATE": str(state),
            "ATELIER2_CLAUDE_CREDENTIALS": str(credentials),
            "ATELIER2_TEST_DOCKER_RECORD": str(record_root),
            "ATELIER2_TEST_GIT_MODE": git_mode,
        }
    )
    if extra_environment:
        environment.update(extra_environment)
    return subprocess.run(
        ["bash", str(repository / "scripts" / "container_up.sh")],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def docker_invocations(tmp_path: Path) -> list[dict[str, object]]:
    log = tmp_path / "docker-record" / "invocations.jsonl"
    if not log.is_file():
        return []
    return [
        json.loads(line)
        for line in log.read_text(encoding="utf-8").splitlines()
        if line
    ]


def compose_build(tmp_path: Path) -> dict[str, object]:
    builds: list[dict[str, object]] = []
    for invocation in docker_invocations(tmp_path):
        argv = invocation.get("argv")
        if isinstance(argv, list) and argv[:2] == ["compose", "build"]:
            builds.append(invocation)
    assert builds, "docker compose build was not invoked"
    return builds[0]


def candidate_context(tmp_path: Path) -> Path:
    context = tmp_path / "docker-record" / "context"
    assert context.is_dir(), "candidate context was not captured"
    return context


def test_the_image_recipe_exists_and_runs_unprivileged() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert_recipe_runs_unprivileged(text)
    assert "uv sync --locked --no-dev" in text
    assert "frontend/dist" in text
    assert "npm run build" in text


def test_a_recipe_that_drops_the_final_stage_user_is_refused() -> None:
    recipe = DOCKERFILE.read_text(encoding="utf-8")
    last_user = None
    for match in _USER_INSTRUCTION.finditer(recipe):
        last_user = match
    assert last_user is not None
    mutated = recipe[: last_user.start()] + recipe[last_user.end() :]

    with pytest.raises(AssertionError, match=_NO_USER_MESSAGE):
        assert_recipe_runs_unprivileged(mutated)


@pytest.mark.parametrize(
    "instruction",
    (
        "USER 0",
        "USER root",
        "USER root:root",
        "USER 0:0",
    ),
)
def test_a_recipe_that_ends_as_privileged_is_refused(instruction: str) -> None:
    recipe = DOCKERFILE.read_text(encoding="utf-8") + f"\n{instruction}\n"

    with pytest.raises(AssertionError, match=_PRIVILEGED_USER_MESSAGE):
        assert_recipe_runs_unprivileged(recipe)


def test_the_image_pins_the_one_conformant_claude_and_no_other_provider() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    declared = re.search(r"^ARG CLAUDE_VERSION=(\S+)$", text, re.MULTILINE)
    assert declared is not None
    assert declared.group(1) == CONFORMANT_CLAUDE_VERSION
    lowered = text.lower()
    assert "codex" not in lowered
    assert "grok" not in lowered


def test_the_image_does_not_copy_host_secrets() -> None:
    ignore = DOCKERIGNORE.read_text(encoding="utf-8")
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")

    assert CREDENTIAL_RECORD_ENTRY in ignore
    assert ".env" in ignore
    assert not re.search(
        r"^COPY .*credentials", dockerfile, re.MULTILINE | re.IGNORECASE
    )
    assert not re.search(r"^COPY .*\.env", dockerfile, re.MULTILINE | re.IGNORECASE)


def test_compose_binds_loopback_via_the_host_network_and_mounts_state() -> None:
    document = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    service = document["services"]["atelier2"]

    assert service["network_mode"] == "host"
    assert "ports" not in service
    mounts = service["volumes"]
    assert any(
        str(mount).startswith("${ATELIER2_STATE}:/var/lib/atelier2") for mount in mounts
    )
    credential_mount = next(
        str(mount) for mount in mounts if CREDENTIAL_RECORD_ENTRY in str(mount)
    )
    assert credential_mount.endswith(
        f"{CONTAINER_CREDENTIAL_DIRECTORY}/{CREDENTIAL_RECORD_ENTRY}:ro"
    )
    assert all("codex" not in str(mount).lower() for mount in mounts)
    assert all("grok" not in str(mount).lower() for mount in mounts)
    assert all(".claude.json" not in str(mount) for mount in mounts)
    assert all("/.claude:" not in str(mount) for mount in mounts)


def test_the_in_image_serve_declares_scratch_claude_and_loopback() -> None:
    text = CONTAINER_SERVE.read_text(encoding="utf-8")

    assert "--agent-scratch-root" in text
    assert CONTAINER_SCRATCH_ROOT in text
    assert "--claude-executable" in text
    assert CONTAINER_CLAUDE_EXECUTABLE in text
    assert "--claude-credential-directory" in text
    assert CONTAINER_CREDENTIAL_DIRECTORY in text
    assert f"--host {LISTEN_ADDRESS}" in text
    assert f"--port {LISTEN_PORT}" in text
    assert "--source-commit" in text
    assert "--source-tree" in text
    assert f"${{{SOURCE_COMMIT}" in text
    assert f"${{{SOURCE_TREE}" in text
    assert "--grok-" not in text
    assert "--codex-" not in text
    assert "HOME" in Path(DOCKERFILE).read_text(encoding="utf-8")


def test_the_host_start_script_prepares_state_and_does_not_cut_over_live() -> None:
    text = CONTAINER_UP.read_text(encoding="utf-8")

    assert HOST_STATE_ROOT in text
    assert "chmod 0700" in text
    assert CREDENTIAL_RECORD_ENTRY in text
    assert LIVE_UNIT in text
    assert "systemctl stop" not in text
    assert "systemctl start" not in text
    assert "systemctl restart" not in text
    assert "docker compose" in text


def test_ci_does_not_build_the_image() -> None:
    workflows = CI_WORKFLOW.read_text(encoding="utf-8") + FOUNDATION_WORKFLOW.read_text(
        encoding="utf-8"
    )

    assert "docker build" not in workflows
    assert "docker compose build" not in workflows
    assert "docker compose up" not in workflows


def test_operations_owns_the_container_runbook() -> None:
    mapping = DOCUMENTATION_MAP.read_text(encoding="utf-8")
    runbook = OPERATIONS.read_text(encoding="utf-8")

    assert "OPERATIONS.md" in mapping
    assert LIVE_UNIT in runbook
    assert "ADR 0009" in runbook
    assert CREDENTIAL_RECORD_ENTRY in runbook
    assert "does not build a real image" in runbook
    assert "exact git commit" in runbook
    assert "missing or unknown" in runbook


def test_compose_does_not_default_source_identity_to_unknown() -> None:
    arguments = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"][
        "atelier2"
    ]["build"]["args"]

    for name in (SOURCE_COMMIT, SOURCE_TREE):
        value = arguments[name]
        assert UNKNOWN_IDENTITY not in value
        assert name in value


def test_image_labels_and_health_inputs_share_the_source_identity_owner() -> None:
    recipe = DOCKERFILE.read_text(encoding="utf-8")
    serve = CONTAINER_SERVE.read_text(encoding="utf-8")
    arguments = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"][
        "atelier2"
    ]["build"]["args"]

    for name in (SOURCE_COMMIT, SOURCE_TREE):
        assert re.search(rf"^ARG {name}$", recipe, re.MULTILINE)
        assert re.search(rf"^ARG {name}=", recipe, re.MULTILINE) is None
        assert f"{name}=${{{name}}}" in recipe
        assert name in arguments[name]
        assert UNKNOWN_IDENTITY not in arguments[name]
    assert re.search(
        rf"LABEL[\s\S]*{SOURCE_COMMIT}=\${{{SOURCE_COMMIT}}}[\s\S]*"
        rf"{SOURCE_TREE}=\${{{SOURCE_TREE}}}",
        recipe,
    )
    assert f'--source-commit "${{{SOURCE_COMMIT}' in serve
    assert f'--source-tree "${{{SOURCE_TREE}' in serve
    identity_region = recipe[recipe.index(f"ARG {SOURCE_COMMIT}") :]
    assert "atelier2-container" not in identity_region


def test_dirty_tracked_and_untracked_bytes_do_not_enter_the_candidate_context(
    tmp_path: Path,
) -> None:
    repository = packaging_repository(tmp_path)
    (repository / COMMITTED_PAYLOAD_NAME).write_text(
        DIRTY_TRACKED_PAYLOAD, encoding="utf-8"
    )
    (repository / UNTRACKED_NAME).write_text(UNTRACKED_PAYLOAD, encoding="utf-8")

    completed = run_container_up(repository, tmp_path)

    assert completed.returncode == 0, completed.stderr
    context = candidate_context(tmp_path)
    assert (context / COMMITTED_PAYLOAD_NAME).read_text(
        encoding="utf-8"
    ) == COMMITTED_PAYLOAD
    assert not (context / UNTRACKED_NAME).exists()
    context_bytes = "\n".join(
        path.read_text(encoding="utf-8")
        for path in context.rglob("*")
        if path.is_file()
    )
    assert DIRTY_TRACKED_PAYLOAD not in context_bytes
    assert UNTRACKED_PAYLOAD not in context_bytes


def test_candidate_context_identity_matches_the_git_owner(tmp_path: Path) -> None:
    repository = packaging_repository(tmp_path)
    commit = run_git(repository, "rev-parse", "HEAD")
    tree = run_git(repository, "rev-parse", "HEAD^{tree}")

    completed = run_container_up(repository, tmp_path)

    assert completed.returncode == 0, completed.stderr
    build = compose_build(tmp_path)
    assert build["source_commit"] == commit
    assert build["source_tree"] == tree
    assert build["source_commit"] != "atelier2-container"
    assert build["source_tree"] != "atelier2-container"


def test_a_missing_source_commit_identity_is_refused_before_build(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "not-a-repository"
    scripts = repository / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(CONTAINER_UP, scripts / "container_up.sh")

    completed = run_container_up(repository, tmp_path)

    assert completed.returncode != 0
    assert MISSING_COMMIT_REFUSAL in completed.stderr
    assert docker_invocations(tmp_path) == []


def test_an_unknown_source_commit_identity_is_refused_before_build(
    tmp_path: Path,
) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(repository, tmp_path, git_mode="unknown-commit")

    assert completed.returncode != 0
    assert UNKNOWN_COMMIT_REFUSAL in completed.stderr
    assert docker_invocations(tmp_path) == []


def test_an_unknown_source_tree_identity_is_refused_before_build(
    tmp_path: Path,
) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(repository, tmp_path, git_mode="unknown-tree")

    assert completed.returncode != 0
    assert UNKNOWN_TREE_REFUSAL in completed.stderr
    assert docker_invocations(tmp_path) == []


def test_an_unknown_git_object_identity_is_refused_before_build(
    tmp_path: Path,
) -> None:
    repository = packaging_repository(tmp_path)

    completed = run_container_up(repository, tmp_path, git_mode="unknown-object")

    assert completed.returncode != 0
    assert UNKNOWN_COMMIT_REFUSAL in completed.stderr
    assert docker_invocations(tmp_path) == []


def test_a_tree_that_does_not_belong_to_the_commit_is_refused_before_build(
    tmp_path: Path,
) -> None:
    repository = packaging_repository(tmp_path)
    foreign_tree = run_git(repository, "rev-parse", "HEAD^{tree}")
    (repository / COMMITTED_PAYLOAD_NAME).write_text(
        "second-commit\n", encoding="utf-8"
    )
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "--quiet", "--message", "second")

    completed = run_container_up(
        repository,
        tmp_path,
        git_mode="mismatch",
        extra_environment={"ATELIER2_TEST_FOREIGN_TREE": foreign_tree},
    )

    assert completed.returncode != 0
    assert FOREIGN_TREE_REFUSAL in completed.stderr
    assert docker_invocations(tmp_path) == []
