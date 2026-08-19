from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts/check_documentation_order.py")
REQUIREMENTS = Path("docs/requirements")
KATALOG_DOCUMENT = REQUIREMENTS / "0005-katalog-und-benannte-workflows.md"
A_TEMPLATE_REQUIREMENT = "REQ-KATALOG-01"
DOCUMENTATION = REQUIREMENTS / "README.md"
BOUND_START = "<!-- documentation-order-gate-bound:start -->"
BOUND_END = "<!-- documentation-order-gate-bound:end -->"
DISTILLED_FROM_BLOCK = (
    "Distilled-From: 5301973340 — the quoted operator sentence (rule 01)\n"
    "                #22 body, sha256\n"
    "                9cf109a2f2915116a8c32f6d74b46579a96513932ca2d03af1814a36cbac43e7\n"
    "                #6 — named, versioned, proven chains (Intent)\n"
    "                ADR 0007 Decisions 1, 2, 3 and 4 and section 9 (rules 02-05,\n"
    "                open questions)\n"
    "                #63 — owner of resolver, admission and picker (Offen)\n"
)
OPERATOR_QUELLE = (
    "Quelle:     OPERATOR — 5301973340, quoting him: „Ich würde gerne "
    'bestimmen können, welche Skills und Agenten ich habe und wo sie herkommen"'
)


def copied_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / GATE).parent.mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / GATE, project / GATE)
    shutil.copytree(PROJECT_ROOT / REQUIREMENTS, project / REQUIREMENTS)
    return project


def run_gate(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE)],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )


def load_gate() -> ModuleType:
    specification = importlib.util.spec_from_file_location(
        "check_documentation_order", PROJECT_ROOT / GATE
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def rewrite(project: Path, relative: Path, before: str, after: str) -> None:
    edited = project / relative
    text = edited.read_text(encoding="utf-8")
    assert before in text, f"{relative} no longer contains {before!r}"
    edited.write_text(text.replace(before, after), encoding="utf-8")


def remove_distilled_from(project: Path) -> None:
    rewrite(project, KATALOG_DOCUMENT, DISTILLED_FROM_BLOCK, "")


def empty_distilled_from(project: Path) -> None:
    rewrite(project, KATALOG_DOCUMENT, DISTILLED_FROM_BLOCK, "Distilled-From:\n")


def remove_quelle_source(project: Path) -> None:
    rewrite(project, KATALOG_DOCUMENT, OPERATOR_QUELLE, "Quelle:     OPERATOR")


def remove_quelle_grade(project: Path) -> None:
    rewrite(
        project,
        KATALOG_DOCUMENT,
        OPERATOR_QUELLE,
        "Quelle:     5301973340, quoting him: „Ich würde gerne bestimmen können, "
        'welche Skills und Agenten ich habe und wo sie herkommen"',
    )


def remove_quelle_field(project: Path) -> None:
    rewrite(project, KATALOG_DOCUMENT, OPERATOR_QUELLE + "\n", "")


def empty_the_documents(project: Path) -> None:
    for document in (project / REQUIREMENTS).glob("*.md"):
        if document.name != "README.md":
            document.unlink()


def test_the_current_requirement_shelf_passes_the_gate(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Documentation order:" in result.stdout
    assert "Distilled-From present" in result.stdout
    assert load_gate().render_honesty_bound() in result.stdout


@pytest.mark.proves("a-requirement-document-names-its-distilled-from-watermark")
@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (remove_distilled_from, "Distilled-From"),
        (empty_distilled_from, "empty Distilled-From"),
    ],
    ids=["missing-watermark", "empty-watermark"],
)
def test_a_document_missing_or_empty_distilled_from_fails(
    tmp_path: Path, mutate: Callable[[Path], None], needle: str
) -> None:
    project = copied_project(tmp_path)
    mutate(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Documentation-order gate failed:" in result.stderr
    assert str(KATALOG_DOCUMENT) in result.stderr
    assert needle in result.stderr


@pytest.mark.proves("a-requirement-rule-names-its-source-and-degree")
@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (remove_quelle_source, "source pointer"),
        (remove_quelle_grade, "OPERATOR or DESK"),
        (remove_quelle_field, "without Quelle"),
    ],
    ids=["no-source", "no-grade", "missing-quelle"],
)
def test_a_rule_whose_quelle_lacks_grade_or_source_fails(
    tmp_path: Path, mutate: Callable[[Path], None], needle: str
) -> None:
    project = copied_project(tmp_path)
    mutate(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Documentation-order gate failed:" in result.stderr
    assert A_TEMPLATE_REQUIREMENT in result.stderr
    assert needle in result.stderr


def test_a_shelf_without_numbered_documents_is_refused(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    empty_the_documents(project)

    result = run_gate(project)

    assert result.returncode != 0, result.stdout + result.stderr
    assert "Documentation-order gate refused:" in result.stderr
    assert "no numbered requirement document" in result.stderr


@pytest.mark.proves("the-documentation-order-gate-states-the-bound-of-what-it-proves")
def test_the_gate_and_the_documentation_state_the_same_bound(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))
    assert result.returncode == 0, result.stdout + result.stderr
    documentation = (PROJECT_ROOT / DOCUMENTATION).read_text(encoding="utf-8")
    documented_bound = documentation.split(BOUND_START, 1)[1].split(BOUND_END, 1)[0]

    stated_bound = load_gate().render_honesty_bound()

    assert documented_bound.strip() == stated_bound.strip()
    assert stated_bound.strip() in result.stdout
    assert "live thread" in stated_bound
    assert "half B" in stated_bound
