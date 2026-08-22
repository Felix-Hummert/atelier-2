from __future__ import annotations

import shlex
from pathlib import Path


def test_secret_scan_command_pins_merge_parent_diff_options() -> None:
    workflow = (Path(__file__).parents[2] / ".github/workflows/ci.yml").read_text(
        encoding="utf-8"
    )
    commands = [
        line.strip().removeprefix("run: ")
        for line in workflow.splitlines()
        if line.strip().startswith("run: ./gitleaks git ")
    ]

    assert len(commands) == 1
    arguments = shlex.split(commands[0])
    assert [
        argument for argument in arguments if argument.startswith("--log-opts=")
    ] == ["--log-opts=--full-history --all --diff-filter=uxdb -m"]
