from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
CONTRACT_VIEW_START = "<!-- architecture-contract-view:start -->"
CONTRACT_VIEW_END = "<!-- architecture-contract-view:end -->"


def copied_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", project / "pyproject.toml")
    shutil.copytree(PROJECT_ROOT / "src", project / "src")
    (project / "scripts").mkdir()
    shutil.copy2(
        PROJECT_ROOT / "scripts" / "check_architecture.py",
        project / "scripts" / "check_architecture.py",
    )
    return project


def run_gate(
    project: Path, environment: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "scripts/check_architecture.py"],
        cwd=project,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def add_contract_to_host_import(project: Path) -> None:
    (project / "src/atelier2/contracts/violation.py").write_text(
        "from atelier2.host import main\n",
        encoding="utf-8",
    )


def add_root_to_application_package_import(project: Path) -> None:
    with (project / "src/atelier2/__init__.py").open("a", encoding="utf-8") as package:
        package.write("import atelier2.application\n")


def add_root_to_application_leaf_import(project: Path) -> None:
    with (project / "src/atelier2/__init__.py").open("a", encoding="utf-8") as package:
        package.write(
            "from atelier2.application.answer_wait import answer_wait_result\n"
        )


def add_api_to_dbos_import(project: Path) -> None:
    (project / "src/atelier2/api/violation.py").write_text(
        "from dbos import DBOS\n",
        encoding="utf-8",
    )


def add_empty_rogue_package(project: Path) -> None:
    rogue = project / "src/atelier2/rogue"
    rogue.mkdir()
    (rogue / "__init__.py").touch()


def add_wire_to_port_import(project: Path) -> None:
    (project / "src/atelier2/api/wire/violation.py").write_text(
        "from atelier2.ports.run_queries import RunProjection\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "violation",
    [
        add_contract_to_host_import,
        add_root_to_application_package_import,
        add_root_to_application_leaf_import,
        add_api_to_dbos_import,
        add_empty_rogue_package,
    ],
    ids=[
        "contracts-to-host",
        "root-to-application-package",
        "root-to-application-deep-leaf",
        "api-to-dbos",
        "empty-rogue",
    ],
)
def test_forbidden_inward_and_dbos_owner_imports_fail(
    tmp_path: Path, violation: Callable[[Path], None]
) -> None:
    project = copied_project(tmp_path)
    violation(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Contracts: 3 kept, 1 broken." in result.stdout


@pytest.mark.proves("wire-schemas-name-no-port-type")
def test_a_wire_schema_module_that_names_a_port_fails(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    add_wire_to_port_import(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Contracts: 3 kept, 1 broken." in result.stdout


def test_green_gate_reports_positive_source_contract_layer_and_native_graph_counts(
    tmp_path: Path,
) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    preflight_counts = re.search(
        r"Architecture preflight: (\d+) source modules, (\d+) contracts, (\d+) layer members",
        result.stdout,
    )
    assert preflight_counts is not None
    source_count, contract_count, layer_count = map(int, preflight_counts.groups())
    script = load_architecture_script()
    assert source_count == script.source_module_count(PROJECT_ROOT / "src/atelier2")
    assert source_count >= script.EXPECTED_SOURCE_MODULE_FLOOR
    assert (contract_count, layer_count) == (4, 7)

    native_counts = re.search(
        r"Analyzed (\d+) files, (\d+) dependencies\.", result.stdout
    )
    assert native_counts is not None
    assert all(count > 0 for count in map(int, native_counts.groups()))
    assert "Contracts: 4 kept, 0 broken." in result.stdout


def empty_source_scan(project: Path) -> None:
    for source in (project / "src/atelier2").rglob("*.py"):
        source.unlink()


def shrink_source_scan(project: Path) -> None:
    (project / "src/atelier2/contracts/hashing.py").unlink()


def remove_contract(project: Path) -> None:
    configuration = project / "pyproject.toml"
    text = configuration.read_text(encoding="utf-8")
    start = text.index('id = "dbos-owner"')
    block_start = text.rfind("[[tool.importlinter.contracts]]", 0, start)
    configuration.write_text(text[:block_start], encoding="utf-8")


def change_layer(project: Path) -> None:
    configuration = project / "pyproject.toml"
    text = configuration.read_text(encoding="utf-8")
    configuration.write_text(
        text.replace('"application",\n    "ports",', '"ports",\n    "application",'),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "change",
    [empty_source_scan, shrink_source_scan, remove_contract, change_layer],
    ids=["empty", "below-floor", "missing-contract", "changed-layer"],
)
def test_empty_shrunken_or_changed_contract_scan_fails(
    tmp_path: Path, change: Callable[[Path], None]
) -> None:
    project = copied_project(tmp_path)
    change(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Architecture preflight refused:" in result.stderr


def load_architecture_script() -> ModuleType:
    script_path = PROJECT_ROOT / "scripts/check_architecture.py"
    specification = importlib.util.spec_from_file_location(
        "check_architecture", script_path
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_architecture_decision_and_executable_contract_share_the_exact_layers_and_owners(
    tmp_path: Path,
) -> None:
    result = run_gate(copied_project(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    decision = (
        PROJECT_ROOT / "docs/decisions/0005-enforced-package-boundaries.md"
    ).read_text(encoding="utf-8")
    documented_view = decision.split(CONTRACT_VIEW_START, 1)[1].split(
        CONTRACT_VIEW_END, 1
    )[0]
    script = load_architecture_script()

    expected_view = script.render_contract_view(
        script.read_architecture_configuration(PROJECT_ROOT / "pyproject.toml")
    )

    assert documented_view.strip() == expected_view.strip()


def test_gate_runs_with_minimal_environment_and_no_network_or_service(
    tmp_path: Path,
) -> None:
    environment = {
        "PATH": os.environ["PATH"],
        "PYTHONIOENCODING": "utf-8",
    }

    result = run_gate(copied_project(tmp_path), environment)

    assert result.returncode == 0, result.stdout + result.stderr
