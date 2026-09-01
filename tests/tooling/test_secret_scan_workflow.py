from __future__ import annotations

from pathlib import Path

WORKFLOW_PATH = Path(__file__).parents[2] / ".github/workflows/ci.yml"
STEP_NAME = "Scan the event's own commits for secrets"
NEXT_STEP_NAME = "Upload the secret-scan report"


def _secret_scan_step_body() -> str:
    workflow = WORKFLOW_PATH.read_text(encoding="utf-8")
    start = workflow.index(f"- name: {STEP_NAME}")
    end = workflow.index(f"- name: {NEXT_STEP_NAME}", start)
    return workflow[start:end]


def test_secret_scan_command_pins_the_load_bearing_diff_options() -> None:
    log_opts_lines = [
        line.strip()
        for line in _secret_scan_step_body().splitlines()
        if line.strip().startswith("--log-opts=")
    ]

    assert len(log_opts_lines) == 1
    assert log_opts_lines[0] == (
        '--log-opts="${base_commit}..HEAD --diff-filter=uxdb -m" \\'
    )


def test_secret_scan_command_scopes_to_the_event_range_not_every_ref() -> None:
    # #968: scanning `--full-history --all` let an unmerged branch turn main and
    # every open pull request red the moment it was pushed, before it ever
    # landed. The gate must read only the triggering event's own commit range.
    step_body = _secret_scan_step_body()
    assert "--full-history" not in step_body
    assert "--all" not in step_body
