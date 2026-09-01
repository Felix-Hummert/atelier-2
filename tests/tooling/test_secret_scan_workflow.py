from __future__ import annotations

import shlex
from pathlib import Path


def _gitleaks_command() -> list[str]:
    workflow = (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    lines = workflow.splitlines()
    start = next(
        index
        for index, line in enumerate(lines)
        if line.strip().startswith("./gitleaks git ")
    )

    command_lines: list[str] = []
    for line in lines[start:]:
        stripped = line.strip()
        continues = stripped.endswith("\\")
        command_lines.append(stripped.removesuffix("\\").strip())
        if not continues:
            break

    # `|| scan_status=$?` is the shell's own error handling, not an argument
    # to gitleaks.
    command = " ".join(command_lines).split(" || ", 1)[0]
    return shlex.split(command)


def test_secret_scan_pins_merge_diff_options_and_scans_only_the_event_range() -> None:
    arguments = _gitleaks_command()

    log_opts = [
        argument for argument in arguments if argument.startswith("--log-opts=")
    ]
    assert len(log_opts) == 1
    opts = log_opts[0].removeprefix("--log-opts=").split()

    # `-m` is load-bearing: without it, a secret that lives only in a merge
    # commit's conflict resolution is invisible to the scan.
    assert "-m" in opts
    assert "--diff-filter=uxdb" in opts

    # #968: `--full-history --all` scanned every ref on origin, so an
    # unmerged branch could turn main and every open pull request red before
    # it ever landed. The scan must read a bounded range from a resolved
    # base commit, never the whole repository.
    assert "--full-history" not in opts
    assert "--all" not in opts
    assert opts[0] == "${base_commit}..HEAD"
