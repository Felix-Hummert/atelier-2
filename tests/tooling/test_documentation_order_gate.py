from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from scripts.requirement_contract import approval_bytes

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts/check_documentation_order.py")
CONTRACT = Path("scripts/requirement_contract.py")
REQUIREMENTS = Path("docs/requirements")
REGISTRY = REQUIREMENTS / "revisions.toml"
DOCUMENTATION = REQUIREMENTS / "README.md"
LEGACY_DOCUMENT = REQUIREMENTS / "0008-example.md"
BOUND_START = "<!-- documentation-order-gate-bound:start -->"
BOUND_END = "<!-- documentation-order-gate-bound:end -->"


def copied_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    for relative in (GATE, CONTRACT):
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(PROJECT_ROOT / relative, destination)
    shutil.copytree(PROJECT_ROOT / REQUIREMENTS, project / REQUIREMENTS)
    return project


def copied_legacy_project(tmp_path: Path) -> Path:
    project = copied_project(tmp_path)
    content = (
        "# Legacy requirement\n\n"
        "### REQ-LEGACY-01: Legacy bytes remain frozen.\n"
        "Quelle: DESK — test fixture\n"
    ).encode()
    (project / LEGACY_DOCUMENT).write_bytes(content)
    registry = project / REGISTRY
    with registry.open("a", encoding="utf-8") as stream:
        stream.write("\n" + legacy_table("0008", LEGACY_DOCUMENT, digest(content)))
    return project


def run_gate(
    project: Path,
    *,
    base_revision: str | None = None,
    github_actions: bool = False,
) -> subprocess.CompletedProcess[str]:
    base = [] if base_revision is None else ["--base-revision", base_revision]
    environment = os.environ.copy()
    environment["GITHUB_ACTIONS"] = "true" if github_actions else "false"
    return subprocess.run(
        [sys.executable, str(GATE), *base],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def commit_project(project: Path) -> str:
    subprocess.run(["git", "init", "--quiet"], cwd=project, check=True)
    subprocess.run(["git", "add", "."], cwd=project, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=test-builder",
            "-c",
            "user.email=test-builder@invalid",
            "commit",
            "--quiet",
            "-m",
            "base requirement shelf",
        ],
        cwd=project,
        check=True,
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def legacy_table(document: str, path: Path, content_digest: str) -> str:
    return (
        f'[[legacy]]\ndocument = "{document}"\npath = "{path}"\n'
        f'content_sha256 = "{content_digest}"\n'
    )


def revision_table(
    content_digest: str,
    *,
    predecessor: str = "GENESIS",
    comment: int = 123456,
) -> str:
    approval = digest(approval_bytes("0008", content_digest))
    return (
        '[[revision]]\ndocument = "0008"\n'
        f'path = "{LEGACY_DOCUMENT}"\ncontent_sha256 = "{content_digest}"\n'
        f'approval_comment_id = {comment}\napproval_sha256 = "{approval}"\n'
        f'predecessor = "{predecessor}"\n'
    )


def strict_content(sentence: str = "Adoption remains controlled.") -> bytes:
    return (
        "# Controlled adoption\n\n## Intent\n\nThe operator controls adoption.\n\n"
        f"## Rules\n\n### REQ-MIGRATED-01: {sentence}\n"
        "Quelle: OPERATOR — issue 1 comment 2\n"
    ).encode()


def migrate_legacy(project: Path, content: bytes | None = None) -> str:
    document = project / LEGACY_DOCUMENT
    old_digest = digest(document.read_bytes())
    migrated = content or strict_content()
    document.write_bytes(migrated)
    current_digest = digest(migrated)
    registry = project / REGISTRY
    original = registry.read_text(encoding="utf-8")
    before = legacy_table("0008", LEGACY_DOCUMENT, old_digest)
    assert before in original
    registry.write_text(
        original.replace(before, revision_table(current_digest)), encoding="utf-8"
    )
    return current_digest


def load_gate() -> ModuleType:
    scripts = str(PROJECT_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    specification = importlib.util.spec_from_file_location(
        "check_documentation_order", PROJECT_ROOT / GATE
    )
    assert specification is not None
    assert specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    sys.modules[specification.name] = module
    specification.loader.exec_module(module)
    return module


def test_the_current_requirement_contract_passes_both_wrappers(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "7 document(s), 96 rule(s), 6 frozen legacy, 1 approval-backed" in result.stdout
    )


def test_the_documentation_wrapper_names_a_contract_refusal(tmp_path: Path) -> None:
    project = copied_legacy_project(tmp_path)
    document = project / LEGACY_DOCUMENT
    document.write_bytes(document.read_bytes() + b"\n")

    result = run_gate(project)

    assert result.returncode != 0
    assert "Documentation-order gate refused:" in result.stderr
    assert str(LEGACY_DOCUMENT) in result.stderr
    assert "legacy content digest" in result.stderr


@pytest.mark.proves("legacy-requirement-bytes-are-frozen-until-migration")
def test_same_change_legacy_bytes_and_matching_registry_repin_are_refused(
    tmp_path: Path,
) -> None:
    project = copied_legacy_project(tmp_path)
    registry = project / REGISTRY
    registry_text = registry.read_text(encoding="utf-8")
    registry.unlink()
    base_revision = commit_project(project)
    document = project / LEGACY_DOCUMENT
    old_digest = digest(document.read_bytes())
    document.write_bytes(document.read_bytes() + b"\n")
    new_digest = digest(document.read_bytes())
    registry.write_text(registry_text.replace(old_digest, new_digest), encoding="utf-8")

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert "requirement 0008" in result.stderr
    assert str(LEGACY_DOCUMENT) in result.stderr
    assert "migrate" in result.stderr


def test_a_legacy_pin_may_be_removed_only_into_a_revision(tmp_path: Path) -> None:
    project = copied_legacy_project(tmp_path)
    base_revision = commit_project(project)
    migrate_legacy(project)

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode == 0, result.stdout + result.stderr


def test_pre_registry_bootstrap_with_exact_legacy_pins_passes(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    registry = project / REGISTRY
    content = registry.read_bytes()
    registry.unlink()
    base_revision = commit_project(project)
    registry.write_bytes(content)

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.proves("legacy-requirement-bytes-are-frozen-until-migration")
def test_a_new_legacy_document_after_the_registry_is_refused(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    base_revision = commit_project(project)
    path = REQUIREMENTS / "0008-new-legacy.md"
    content = (
        "# New legacy\n\n### REQ-NEWLEGACY-01: New work is revisioned.\n"
        "Quelle: DESK — issue 460\n"
    ).encode()
    (project / path).write_bytes(content)
    with (project / REGISTRY).open("a", encoding="utf-8") as registry:
        registry.write("\n" + legacy_table("0008", path, digest(content)))

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert "new legacy requirement 0008" in result.stderr


@pytest.mark.proves("a-requirement-revision-registry-is-linear-and-bound")
def test_an_approval_backed_successor_extends_history(tmp_path: Path) -> None:
    project = copied_legacy_project(tmp_path)
    predecessor = migrate_legacy(project)
    base_revision = commit_project(project)
    content = strict_content("A successor remains controlled.")
    (project / LEGACY_DOCUMENT).write_bytes(content)
    with (project / REGISTRY).open("a", encoding="utf-8") as registry:
        registry.write("\n" + revision_table(digest(content), predecessor=predecessor))

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.proves("a-requirement-revision-registry-is-linear-and-bound")
def test_approval_backed_history_cannot_be_rewritten_in_place(tmp_path: Path) -> None:
    project = copied_legacy_project(tmp_path)
    migrate_legacy(project)
    base_revision = commit_project(project)
    registry = project / REGISTRY
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            "approval_comment_id = 123456", "approval_comment_id = 123457"
        ),
        encoding="utf-8",
    )

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert "revision 0008" in result.stderr
    assert "changed or deleted" in result.stderr


@pytest.mark.proves("a-requirement-revision-registry-is-linear-and-bound")
def test_approval_backed_history_cannot_be_deleted_and_restarted(
    tmp_path: Path,
) -> None:
    project = copied_legacy_project(tmp_path)
    old_digest = migrate_legacy(project)
    base_revision = commit_project(project)
    content = strict_content("Replacement history is forbidden.")
    (project / LEGACY_DOCUMENT).write_bytes(content)
    registry = project / REGISTRY
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            revision_table(old_digest), revision_table(digest(content))
        ),
        encoding="utf-8",
    )

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert "revision 0008" in result.stderr
    assert "changed or deleted" in result.stderr


@pytest.mark.proves("a-requirement-revision-registry-is-linear-and-bound")
@pytest.mark.parametrize("relative", (LEGACY_DOCUMENT, REGISTRY))
def test_base_snapshot_refuses_nonregular_git_tree_modes(
    tmp_path: Path, relative: Path
) -> None:
    project = copied_legacy_project(tmp_path)
    target = project / relative
    content = target.read_bytes()
    outside = tmp_path / f"base-{target.name}"
    outside.write_bytes(content)
    target.unlink()
    target.symlink_to(outside)
    base_revision = commit_project(project)
    target.unlink()
    target.write_bytes(content)

    result = run_gate(project, base_revision=base_revision)

    assert result.returncode != 0
    assert str(relative) in result.stderr
    assert "regular Git file" in result.stderr


def test_github_actions_refuses_an_unbound_base_revision(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path), github_actions=True)

    assert result.returncode != 0
    assert "exact base revision" in result.stderr


def test_an_unresolvable_exact_base_revision_fails_closed(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path), base_revision="0" * 40)

    assert result.returncode != 0
    assert "absent or unresolvable" in result.stderr


@pytest.mark.proves("the-documentation-order-gate-states-the-bound-of-what-it-proves")
def test_the_gate_and_the_documentation_state_the_same_bound(tmp_path: Path) -> None:
    result = run_gate(copied_project(tmp_path))
    documentation = (PROJECT_ROOT / DOCUMENTATION).read_text(encoding="utf-8")
    documented = documentation.split(BOUND_START, 1)[1].split(BOUND_END, 1)[0]

    assert result.returncode == 0, result.stdout + result.stderr
    assert documented.strip() == load_gate().render_honesty_bound().strip()
    assert documented.strip() in result.stdout
