from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "atelier2"
LAUNCHER = PROJECT_ROOT / "scripts" / "runner_candidate.sh"
CARRIER = SOURCE_ROOT / "adapters" / "docker_carrier.py"
COMPOSE = PROJECT_ROOT / "compose.yaml"
ENGINE_EXECUTABLE = "/usr/bin/docker"
# Every spelling by which a program reaches the engine: its own name, however it
# would be found on `PATH` or addressed outright, and the socket that is the
# engine's actual authority. A module that names none of these has no way to
# call it -- including through an argument vector, which a substring search over
# whole files would have walked straight past.
ENGINE_NAMES = frozenset(
    {"docker", ENGINE_EXECUTABLE, "/var/run/docker.sock", "docker.sock"}
)
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


def _engine_names_in(module: Path) -> set[str]:
    """Every engine spelling this module could hand to anything, at all.

    Read out of the parsed source rather than searched for in the text, because
    the way a program calls the engine is an argument vector -- `["docker",
    "run", ...]` -- in which no substring of a call ever appears. A comment or
    a docstring naming Docker is not a call and is not counted; a string
    constant that *is* one of these names is, wherever it sits.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in ENGINE_NAMES
    } - _docstrings(tree)


def _docstrings(tree: ast.Module) -> set[str]:
    documented = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        text
        for node in ast.walk(tree)
        if isinstance(node, documented) and (text := ast.get_docstring(node, False))
    }


def test_the_carrier_is_the_only_source_module_that_can_reach_the_engine() -> None:
    """Docker authority is spent in one owner and nowhere else in the source.

    The host launcher's carrier holds it (ADR 0009 sec. 2, `#540` ruling B);
    every other source module ships inside a Core or Runner image, where the
    ability to reach the engine would be exactly the privilege the whole
    arrangement exists to withhold. The witness drives its launcher operations
    through that same owner rather than through a second, shell-shaped copy.
    """
    assert ENGINE_EXECUTABLE in _engine_names_in(CARRIER)
    for path in _python_files(SOURCE_ROOT):
        if path != CARRIER:
            assert _engine_names_in(path) == set(), path
    for path in (PROJECT_ROOT / "src").rglob("*.sh"):
        assert not {name for name in ENGINE_NAMES if name in path.read_text("utf-8")}
    assert "python -m atelier2.adapters.docker_carrier" in LAUNCHER.read_text(
        encoding="utf-8"
    )


def test_the_stable_console_is_given_no_engine_authority() -> None:
    """The console runs the product; it never gets to run containers.

    This is ADR 0009 sec. 2's actual stop condition, and the reason the host
    launcher exists at all: a Serve compromise must not be host root. A mounted
    engine socket or a privileged service would hand it exactly that, so the
    deployment descriptor is read for both.
    """
    compose = COMPOSE.read_text(encoding="utf-8")

    assert "docker.sock" not in compose
    assert "privileged" not in compose
    assert "cap_add" not in compose


def test_stable_serve_files_do_not_adopt_candidate_packaging() -> None:
    """The disposable candidate ships beside the stable serve stack, never in it."""
    for name in STABLE_FILES:
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert "runner_candidate" not in text
        assert "Dockerfile.runner" not in text


def test_the_witness_copies_public_bootstrap_and_keeps_core_handoff_read_only() -> None:
    """Core reads its handoff and never writes it.

    The public bootstrap leaves Core the same way it always did -- copied out
    of the container -- and the directory it is copied into is bound back into
    Core read-only, so the attestation Core reads from it is one nothing inside
    Core could have written.
    """
    text = LAUNCHER.read_text(encoding="utf-8")
    assert "carrier copy-from --container" in text
    assert "--source /var/lib/atelier2-candidate/bootstrap.json" in text
    assert '"$root/handoff:/handoff:ro"' in text
    assert '"$root/handoff:/handoff"' not in text.replace(
        '"$root/handoff:/handoff:ro"', ""
    )
    assert "unlink-private" in text
    core = (PROJECT_ROOT / "tests/witness/runner_candidate_core.py").read_text(
        encoding="utf-8"
    )
    assert "inspect-attested" in core
    assert 'handoff / "inspect-attested"' in core
    assert "_write_json(inspect_attested" not in core
    assert 'handoff / "inspect-attested").write' not in core


def test_the_witness_records_the_attempt_network_the_launcher_reported() -> None:
    """A concurrent `clean` must never see a recorded network before it exists,
    or it could mistake a still-running witness for a released one. The witness
    does not name the network at all any more: it records the one the launcher
    says it created, which cannot precede the creation."""
    text = LAUNCHER.read_text(encoding="utf-8")
    reported_at = text.index("sed -n 's/^attempt-network=//p'")
    recorded_at = text.index('>"$root/network"')
    assert reported_at < recorded_at
