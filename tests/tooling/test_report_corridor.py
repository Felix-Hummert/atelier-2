"""The corridor report: AGENTS.md's slice size, always reported and never red.

The gate is driven as its own process over a scratch git repository -- the
report reads a real `git diff --numstat`, so the sentence under test is what
the tool prints for a real range, not how it sums two numbers.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]
GATE = Path("scripts") / "report_corridor.py"
GIT_IDENTITY = ("-c", "user.name=test-builder", "-c", "user.email=test-builder@invalid")


def scratch_repository(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    shutil.copy2(PROJECT_ROOT / GATE, project / GATE)
    _git(project, "init", "--quiet")
    return project


def _git(project: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=project, check=True, capture_output=True, text=True
    )


def write_files(project: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def delete_files(project: Path, relatives: list[str]) -> None:
    for relative in relatives:
        (project / relative).unlink()


def commit(project: Path, message: str) -> str:
    _git(project, "add", "-A")
    _git(project, *GIT_IDENTITY, "commit", "--quiet", "-m", message)
    return _git(project, "rev-parse", "HEAD").stdout.strip()


def numbered_lines(count: int) -> str:
    return "\n".join(f"line_{index} = {index}" for index in range(count)) + "\n"


def run_gate(
    project: Path, base: str, *, step_summary: Path | None = None
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment.pop("GITHUB_REPOSITORY", None)
    if step_summary is None:
        environment.pop("GITHUB_STEP_SUMMARY", None)
    else:
        environment["GITHUB_STEP_SUMMARY"] = str(step_summary)
    return subprocess.run(
        [sys.executable, str(GATE), "--base", base],
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_under_corridor_reports_and_skips_the_step_summary(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    write_files(
        project,
        {"src/atelier2/thing.py": "a = 1\n", "frontend/src/thing.ts": "const a = 1;\n"},
    )
    base = commit(project, "base")
    write_files(
        project,
        {
            "src/atelier2/thing.py": "a = 1\nb = 2\n",
            "frontend/src/thing.ts": "const a = 1;\nconst b = 2;\n",
        },
    )
    commit(project, "small change")
    step_summary = tmp_path / "summary.md"

    result = run_gate(project, base, step_summary=step_summary)

    assert result.returncode == 0
    assert (
        "corridor: 2 production files, +2/-0 lines (limit 3 files, 100 lines)"
        in result.stdout
    )
    assert not step_summary.exists()


def test_over_corridor_by_file_count_writes_the_step_summary(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    base = commit(project, "base")
    write_files(
        project, {f"src/atelier2/mod{index}.py": "a = 1\n" for index in range(4)}
    )
    commit(project, "four new files")
    step_summary = tmp_path / "summary.md"

    result = run_gate(project, base, step_summary=step_summary)

    assert result.returncode == 0
    summary_line = (
        "corridor: 4 production files, +4/-0 lines (limit 3 files, 100 lines)"
    )
    assert summary_line in result.stdout
    assert summary_line in step_summary.read_text(encoding="utf-8")


def test_over_corridor_by_line_count_writes_the_step_summary(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    base = commit(project, "base")
    write_files(project, {"src/atelier2/thing.py": numbered_lines(101)})
    commit(project, "one large file")
    step_summary = tmp_path / "summary.md"

    result = run_gate(project, base, step_summary=step_summary)

    assert result.returncode == 0
    summary_line = (
        "corridor: 1 production files, +101/-0 lines (limit 3 files, 100 lines)"
    )
    assert summary_line in result.stdout
    assert summary_line in step_summary.read_text(encoding="utf-8")


def test_exclusions_honoured_ignores_tests_and_docs(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    base = commit(project, "base")
    write_files(
        project,
        {
            "tests/tooling/test_thing.py": numbered_lines(60),
            "docs/notes.md": numbered_lines(60),
            "src/atelier2/thing.py": "value = 1\n",
        },
    )
    commit(project, "mixed change")

    result = run_gate(project, base)

    assert result.returncode == 0
    assert (
        "corridor: 1 production files, +1/-0 lines (limit 3 files, 100 lines)"
        in result.stdout
    )


def test_deletion_only_diff_counts_only_removed_lines(tmp_path: Path) -> None:
    project = scratch_repository(tmp_path)
    write_files(project, {"src/atelier2/thing.py": numbered_lines(3)})
    base = commit(project, "base")
    delete_files(project, ["src/atelier2/thing.py"])
    commit(project, "delete the module")

    result = run_gate(project, base)

    assert result.returncode == 0
    assert (
        "corridor: 1 production files, +0/-3 lines (limit 3 files, 100 lines)"
        in result.stdout
    )
