from __future__ import annotations

import re
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

_USER_INSTRUCTION = re.compile(r"^USER\s+(\S+)\s*$", re.MULTILINE | re.IGNORECASE)
_PRIVILEGED_USERS = frozenset({"root", "0"})


def last_user_name(recipe: str) -> str:
    declared = [
        match.group(1).split(":", 1)[0] for match in _USER_INSTRUCTION.finditer(recipe)
    ]
    assert declared, "image recipe declares no USER"
    return declared[-1]


def assert_recipe_runs_unprivileged(recipe: str) -> None:
    assert last_user_name(recipe) not in _PRIVILEGED_USERS


def test_the_image_recipe_exists_and_runs_unprivileged() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")

    assert_recipe_runs_unprivileged(text)
    assert "uv sync --locked --no-dev" in text
    assert "frontend/dist" in text
    assert "npm run build" in text


def test_a_recipe_that_ends_as_numeric_root_is_refused() -> None:
    recipe = DOCKERFILE.read_text(encoding="utf-8") + "\nUSER 0\n"

    with pytest.raises(AssertionError):
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
    assert "do not build the image" in runbook
