from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.check_screenshot_review import (
    GOVERNED_REQUIREMENT,
    LEDGER_LOCATION,
    SCOPE,
    approval_bytes,
    compute_scope_digest,
)

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts/check_screenshot_review.py")

FIXTURE_CONTENT = b"fixture content unrelated to the real repository\n"


def copied_project(tmp_path: Path) -> Path:
    """A project whose scoped files are small fixture stubs, never real
    repository content: the gate's own contract is that the digest covers
    source bytes, not what those bytes mean -- so a fixture stub proves the
    mechanism exactly as well as a copy of the real tree, without coupling
    this test to how those files evolve."""
    project = tmp_path / "project"
    (project / GATE).parent.mkdir(parents=True, exist_ok=True)
    (project / GATE).write_bytes((PROJECT_ROOT / GATE).read_bytes())
    for relative in SCOPE:
        target = project / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(FIXTURE_CONTENT + relative.as_posix().encode() + b"\n")
    ledger = project / LEDGER_LOCATION
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("schema_version = 1\n", encoding="utf-8")
    return project


def run_gate(project: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE)],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )


def approval_table(requirement: str, scope_digest: str, *, comment: int = 555) -> str:
    approval_digest = hashlib.sha256(
        approval_bytes(requirement, scope_digest)
    ).hexdigest()
    return (
        "\n[[approval]]\n"
        f'requirement = "{requirement}"\n'
        f'scope_sha256 = "{scope_digest}"\n'
        f"approval_comment_id = {comment}\n"
        f'approval_sha256 = "{approval_digest}"\n'
    )


@pytest.mark.proves(
    "the-screenshot-review-gate-proves-a-mechanical-red-green-transition"
)
def test_the_gate_moves_from_red_to_green_only_once_the_ledger_matches_the_scope_digest(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    scope_digest = compute_scope_digest(project)

    red = run_gate(project)

    assert red.returncode != 0
    assert "Screenshot review gate refused:" in red.stderr
    assert str(LEDGER_LOCATION) in red.stderr
    assert (
        f"APPROVE SCREENSHOT REVIEW {GOVERNED_REQUIREMENT} sha256:{scope_digest}"
        in red.stderr
    )

    ledger = project / LEDGER_LOCATION
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + approval_table(GOVERNED_REQUIREMENT, scope_digest),
        encoding="utf-8",
    )
    green = run_gate(project)

    assert green.returncode == 0, green.stdout + green.stderr
    assert GOVERNED_REQUIREMENT in green.stdout
    assert scope_digest in green.stdout


def test_the_gate_refuses_when_a_scoped_file_changes_after_approval(
    tmp_path: Path,
) -> None:
    project = copied_project(tmp_path)
    approved_digest = compute_scope_digest(project)
    ledger = project / LEDGER_LOCATION
    ledger.write_text(
        ledger.read_text(encoding="utf-8")
        + approval_table(GOVERNED_REQUIREMENT, approved_digest),
        encoding="utf-8",
    )
    changed_file = project / SCOPE[0]
    changed_file.write_bytes(changed_file.read_bytes() + b"changed\n")

    result = run_gate(project)

    assert result.returncode != 0
    current_digest = compute_scope_digest(project)
    assert current_digest != approved_digest
    assert current_digest in result.stderr


def test_an_approval_entry_with_a_forged_digest_is_refused(tmp_path: Path) -> None:
    project = copied_project(tmp_path)
    scope_digest = compute_scope_digest(project)
    ledger = project / LEDGER_LOCATION
    forged = (
        "\n[[approval]]\n"
        f'requirement = "{GOVERNED_REQUIREMENT}"\n'
        f'scope_sha256 = "{scope_digest}"\n'
        "approval_comment_id = 1\n"
        f'approval_sha256 = "{"0" * 64}"\n'
    )
    ledger.write_text(ledger.read_text(encoding="utf-8") + forged, encoding="utf-8")

    result = run_gate(project)

    assert result.returncode != 0
    assert "approval digest" in result.stderr


def test_the_scope_names_only_regular_non_symlink_files_under_the_real_repository() -> (
    None
):
    for relative in SCOPE:
        target = PROJECT_ROOT / relative
        assert not target.is_symlink(), relative
        assert target.is_file(), relative
