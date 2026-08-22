from __future__ import annotations

import ast
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
            assert "docker " not in body


def test_stable_serve_files_do_not_adopt_candidate_packaging() -> None:
    """The disposable candidate ships beside the stable serve stack, never in it."""
    for name in STABLE_FILES:
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert "runner_candidate" not in text
        assert "Dockerfile.runner" not in text


def test_launcher_copies_public_bootstrap_and_keeps_core_inspect_read_only() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    assert (
        '/usr/bin/docker cp "$core:/var/lib/atelier2-candidate/bootstrap.json"' in text
    )
    assert "/usr/bin/docker cp" in text
    assert '"$root/handoff:/handoff:ro"' in text
    assert '"$root/handoff:/handoff"' not in text.replace(
        '"$root/handoff:/handoff:ro"', ""
    )
    assert "dst=/handoff,volume-nocopy" in text
    assert "--tmpfs /handoff:" not in text
    assert "unlink-private" in text
    assert "attest-inspect" in text
    assert '"$root/handoff/inspect-attested"' in text
    core = (PROJECT_ROOT / "tests/witness/runner_candidate_core.py").read_text(
        encoding="utf-8"
    )
    assert "inspect-attested" in core
    assert 'handoff / "inspect-attested"' in core
    assert "_write_json(inspect_attested" not in core
    assert 'handoff / "inspect-attested").write' not in core
