from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts/check_product_status.py")
INDEX = Path("docs/PRODUCT.md")
SECTIONS = (
    "intent.md",
    "runtime.md",
    "workflow.md",
    "interfaces.md",
    "operations.md",
    "governance.md",
)


def copied_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    destination = project / GATE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / GATE, destination)
    (project / INDEX).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(PROJECT_ROOT / INDEX, project / INDEX)
    shutil.copytree(PROJECT_ROOT / "docs" / "product", project / "docs" / "product")
    return project


def run_gate(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE)],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )


def test_product_status_index_names_every_owned_section(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Product status index: 6 owned section(s)" in result.stdout


def test_product_status_index_refuses_a_missing_owned_section(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    (project / "docs" / "product" / "workflow.md").unlink()

    result = run_gate(project)

    assert result.returncode != 0
    assert "docs/product/workflow.md" in result.stderr


def test_product_status_index_refuses_an_unlisted_section(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    index = project / INDEX
    index.write_text(
        index.read_text(encoding="utf-8").replace(
            "- [Workflow execution](product/workflow.md)\n", ""
        ),
        encoding="utf-8",
    )

    result = run_gate(project)

    assert result.returncode != 0
    assert "docs/product/workflow.md" in result.stderr


def test_product_status_index_refuses_an_unowned_section(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    unexpected_section = project / "docs" / "product" / "unowned.md"
    unexpected_section.write_text("# Unowned\n", encoding="utf-8")

    result = run_gate(project)

    assert result.returncode != 0
    assert "unowned.md" in result.stderr
