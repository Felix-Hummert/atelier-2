from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "atelier2"
LAUNCHER = PROJECT_ROOT / "scripts" / "runner_candidate.sh"
CARRIER = SOURCE_ROOT / "adapters" / "docker_carrier.py"
ENGINE_EXECUTABLE = "/usr/bin/docker"
# The engine's own command vocabulary, as it reads in a call: `docker network`,
# `docker run`, and every other verb. `docker_carrier` never matches it, so a
# module that merely imports the carrier is not a module that calls the engine.
ENGINE_CALL = "docker "
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


def test_the_carrier_is_the_only_source_module_that_addresses_the_engine() -> None:
    """Docker authority is spent in one owner and nowhere else in the source.

    The host launcher's carrier holds it (ADR 0009 sec. 2, `#540` ruling B);
    every other source module ships inside a Core or Runner image, where an
    engine call would be exactly the privilege the whole arrangement exists to
    withhold. The witness drives its launcher operations through that same
    owner rather than through a second, shell-shaped copy of it.
    """
    assert ENGINE_EXECUTABLE in CARRIER.read_text(encoding="utf-8")
    for path in (PROJECT_ROOT / "src").rglob("*"):
        if path == CARRIER or path.suffix not in {".py", ".sh"} or not path.is_file():
            continue
        body = path.read_text(encoding="utf-8")
        assert ENGINE_CALL not in body
        assert ENGINE_EXECUTABLE not in body
    assert "python -m atelier2.adapters.docker_carrier" in LAUNCHER.read_text(
        encoding="utf-8"
    )


def test_stable_serve_files_do_not_adopt_candidate_packaging() -> None:
    """The disposable candidate ships beside the stable serve stack, never in it."""
    for name in STABLE_FILES:
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert "runner_candidate" not in text
        assert "Dockerfile.runner" not in text


def test_launcher_copies_public_bootstrap_and_keeps_core_inspect_read_only() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "carrier copy-from --container" in text
    assert "--source /var/lib/atelier2-candidate/bootstrap.json" in text
    assert '"$root/handoff:/handoff:ro"' in text
    assert '"$root/handoff:/handoff"' not in text.replace(
        '"$root/handoff:/handoff:ro"', ""
    )
    assert '--volume "$handoff_volume:/handoff:rw"' in text
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


def test_launcher_records_the_witness_network_only_after_it_is_created() -> None:
    """A concurrent `clean` must never see a recorded network before it exists,
    or it could mistake a still-running witness for a released one."""
    text = LAUNCHER.read_text(encoding="utf-8")
    network_created_at = text.index('carrier create-network --name "$network"')
    network_recorded_at = text.index('>"$root/network"')
    assert network_created_at < network_recorded_at
