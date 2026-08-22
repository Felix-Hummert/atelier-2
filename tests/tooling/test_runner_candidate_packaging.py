from __future__ import annotations

import ast
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "atelier2"
LAUNCHER = PROJECT_ROOT / "scripts" / "runner_candidate.sh"
STABLE_FILES = (
    "Dockerfile",
    "compose.yaml",
    "scripts/container_live.sh",
    "scripts/container_up.sh",
)


def _python_files(root: Path) -> tuple[Path, ...]:
    return tuple(sorted(root.rglob("*.py")))


def test_core_and_runner_source_have_no_docker_client() -> None:
    for path in _python_files(SOURCE_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".", 1)[0] != "docker" for alias in node.names
                )
            if isinstance(node, ast.ImportFrom) and node.module is not None:
                assert node.module.split(".", 1)[0] != "docker"


def test_launcher_is_the_only_candidate_docker_caller() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "/usr/bin/docker" in text
    assert "docker run" in text
    for path in (PROJECT_ROOT / "src").rglob("*"):
        if path.suffix in {".py", ".sh"} and path.is_file():
            body = path.read_text(encoding="utf-8")
            assert "docker " not in body or path.name == "runner_candidate.sh"


def test_candidate_packaging_does_not_touch_stable_serve_files() -> None:
    diff = subprocess.check_output(
        ["git", "diff", "--", *STABLE_FILES],
        cwd=PROJECT_ROOT,
        text=True,
    )
    assert diff == ""
