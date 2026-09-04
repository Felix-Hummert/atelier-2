from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUTO_REDEPLOY = PROJECT_ROOT / "scripts" / "auto_redeploy.sh"
AUTO_REDEPLOY_SERVICE = PROJECT_ROOT / "scripts" / "atelier2-auto-redeploy.service"

BLOCKING_RUN = {
    "public_run_reference": "cnVuLTE",
    "state": "STARTED",
    "started_at": "2026-09-03T07:15:00Z",
}
"""The one running run's own projected fields, as `RunResourceV3` carries them."""

BLOCKING_RUN_SENTENCE = "cnVuLTE STARTED since 2026-09-03T07:15:00Z"
"""How the watcher must name that run: reference, state, since when."""


def run_row(run: Mapping[str, object]) -> dict[str, object]:
    """A `/runs` list row whose own projection could be told (#1042, #1109)."""
    return {"kind": "run", "run": run}


DEFECTIVE_RUN_ROW = {
    "kind": "defective",
    "public_run_reference": "cnVuLTI",
    "problem_code": "durable-state-corrupt",
    "detail": "durable projection unreadable",
}
"""A `/runs` list row whose own projection failed (#1042); never an active run."""

GIT_IDENTITY = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "auto-redeploy-test",
    "GIT_AUTHOR_EMAIL": "auto-redeploy-test@invalid",
    "GIT_COMMITTER_NAME": "auto-redeploy-test",
    "GIT_COMMITTER_EMAIL": "auto-redeploy-test@invalid",
}


def write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def run_git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        env={**os.environ, **GIT_IDENTITY},
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def install_serve_live_update_stub(
    scripts_directory: Path, *, marker: str = ""
) -> None:
    # `marker` lets a test install a second, observably different revision of
    # this stub as a later commit — proving which commit's script actually
    # ran, the way test_the_target_commits_own_serve_live_update_script_runs
    # does below.
    marker_write = (
        f'Path(os.environ["ATELIER2_TEST_SERVE_SCRIPT_MARKER_FILE"])'
        f'.write_text({marker!r}, encoding="utf-8")\n'
        if marker
        else ""
    )
    write_stub(
        scripts_directory / "serve_live_update.sh",
        """\
import json
import os
import subprocess
import sys
from pathlib import Path

log_path = Path(os.environ["ATELIER2_TEST_COMMAND_LOG"])
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(["serve_live_update", *sys.argv[1:]]) + "\\n")

if os.environ.get("ATELIER2_TEST_UPDATE_FAILS") == "1":
    print("serve live update: refused", file=sys.stderr)
    raise SystemExit(1)

target_commit = sys.argv[1]
repository = Path(__file__).resolve().parent.parent
# Mirrors production serve_live_update.sh's own clean-checkout preflight: if
# the watcher ever staged the materialised script somewhere `git status`
# can see, this refusal is what would have caught it.
worktree_status = subprocess.run(
    ["git", "-C", str(repository), "status", "--porcelain"],
    check=True,
    capture_output=True,
    text=True,
).stdout
if worktree_status:
    print(
        "serve live update: the deploy checkout is not clean; refusing to touch operator work",
        file=sys.stderr,
    )
    raise SystemExit(1)
current_head = subprocess.run(
    ["git", "-C", str(repository), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
if current_head == target_commit:
    print(
        "serve live update: checkout was already at the target commit; "
        "the watcher must not fast-forward itself",
        file=sys.stderr,
    )
    raise SystemExit(1)
subprocess.run(
    ["git", "-C", str(repository), "merge", "--ff-only", "--quiet", target_commit],
    check=True,
    capture_output=True,
    text=True,
)
Path(os.environ["ATELIER2_TEST_SERVED_COMMIT_FILE"]).write_text(
    target_commit, encoding="utf-8"
)
"""
        + marker_write
        + """\
print(f"serve live update: now serves {target_commit}")
if os.environ.get("ATELIER2_TEST_UPDATE_INTAKE_REFUSED") == "1":
    print("serve live update: WORKFLOW INTAKE REFUSED", file=sys.stderr)
    raise SystemExit(3)
""",
    )


def install_curl_stub(bin_directory: Path) -> None:
    write_stub(
        bin_directory / "curl",
        """\
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

url = sys.argv[-1]
with Path(os.environ["ATELIER2_TEST_COMMAND_LOG"]).open(
    "a", encoding="utf-8"
) as handle:
    handle.write(json.dumps(["curl", *sys.argv[1:]]) + "\\n")

if url.endswith("/atelier/api/v1/health"):
    served_path = Path(os.environ["ATELIER2_TEST_SERVED_COMMIT_FILE"])
    if os.environ.get("ATELIER2_TEST_HEALTH_UNREADABLE") == "1":
        raise SystemExit(22)
    if os.environ.get("ATELIER2_TEST_HEALTH_MALFORMED") == "1":
        print("not-json")
        raise SystemExit(0)
    if not served_path.exists():
        raise SystemExit(7)
    print(
        json.dumps(
            {
                "status": os.environ.get("ATELIER2_TEST_HEALTH_STATUS", "serving"),
                "source_commit": served_path.read_text(encoding="utf-8").strip(),
                "source_tree": "0" * 40,
            }
        )
    )
    raise SystemExit(0)

parsed = urlparse(url)
if parsed.path != "/atelier/api/v1/runs":
    raise SystemExit(2)
query = parse_qs(parsed.query)
state = query.get("state", [""])[0]

counter_path = Path(os.environ["ATELIER2_TEST_RUN_REQUEST_COUNT"])
request_count = int(counter_path.read_text(encoding="utf-8")) if counter_path.exists() else 0
counter_path.write_text(str(request_count + 1), encoding="utf-8")

if os.environ.get("ATELIER2_TEST_RUNS_UNREADABLE_STATE") == state:
    raise SystemExit(22)
if os.environ.get("ATELIER2_TEST_RUNS_MALFORMED_STATE") == state:
    print("not-json")
    raise SystemExit(0)

busy_state = os.environ.get("ATELIER2_TEST_BUSY_STATE")
busy_after_first_check = os.environ.get("ATELIER2_TEST_BUSY_AFTER_FAST_FORWARD") == "1"
is_second_check = request_count >= 1
is_busy = state == busy_state and (is_second_check or not busy_after_first_check)
items = json.loads(os.environ["ATELIER2_TEST_BUSY_RUNS"]) if is_busy else []
next_after = os.environ.get("ATELIER2_TEST_BUSY_NEXT_PAGE") or None
print(json.dumps({"items": items, "next_after": next_after}))
""",
    )


def install_gh_stub(bin_directory: Path) -> None:
    write_stub(
        bin_directory / "gh",
        """\
import json
import os
import sys
from pathlib import Path

with Path(os.environ["ATELIER2_TEST_COMMAND_LOG"]).open(
    "a", encoding="utf-8"
) as handle:
    handle.write(json.dumps(["gh", *sys.argv[1:]]) + "\\n")

scenarios = {
    "green": [("completed", "success"), ("completed", "success")],
    "queued": [("queued", "")],
    "in_progress": [("in_progress", "")],
    "failure": [("completed", "failure")],
    "cancelled": [("completed", "cancelled")],
    "timed_out": [("completed", "timed_out")],
    "neutral_and_skipped": [("completed", "neutral"), ("completed", "skipped")],
    "none": [],
}
# A sequence lets a test give the walk over first-parent history a different
# scenario per commit it examines (newest candidate first), which a single
# ATELIER2_TEST_CHECKS value cannot express; each call consumes the next
# name and the last one repeats for any call beyond the list.
sequence = os.environ.get("ATELIER2_TEST_CHECKS_SEQUENCE")
if sequence:
    names = sequence.split(",")
    counter_path = Path(os.environ["ATELIER2_TEST_CHECK_REQUEST_COUNT"])
    call_index = int(counter_path.read_text(encoding="utf-8")) if counter_path.exists() else 0
    counter_path.write_text(str(call_index + 1), encoding="utf-8")
    scenario_name = names[min(call_index, len(names) - 1)]
else:
    scenario_name = os.environ.get("ATELIER2_TEST_CHECKS", "green")
checks = scenarios[scenario_name]
print(f"envelope\\t{len(checks)}")
for status, conclusion in checks:
    print(f"check\\t{status}\\t{conclusion}")
""",
    )


def install_logger_stub(bin_directory: Path) -> None:
    write_stub(
        bin_directory / "logger",
        """\
import json
import os
import sys
from pathlib import Path

with Path(os.environ["ATELIER2_TEST_COMMAND_LOG"]).open(
    "a", encoding="utf-8"
) as handle:
    handle.write(json.dumps(["logger", *sys.argv[1:]]) + "\\n")
""",
    )


def install_flock_stub(bin_directory: Path) -> None:
    write_stub(
        bin_directory / "flock",
        """\
import json
import os
import sys
from pathlib import Path

with Path(os.environ["ATELIER2_TEST_COMMAND_LOG"]).open(
    "a", encoding="utf-8"
) as handle:
    handle.write(json.dumps(["flock", *sys.argv[1:]]) + "\\n")
raise SystemExit(1 if os.environ.get("ATELIER2_TEST_LOCK_BUSY") == "1" else 0)
""",
    )


def install_timeout_stub(bin_directory: Path) -> None:
    write_stub(
        bin_directory / "timeout",
        """\
import json
import os
import subprocess
import sys
from pathlib import Path

with Path(os.environ["ATELIER2_TEST_COMMAND_LOG"]).open(
    "a", encoding="utf-8"
) as handle:
    handle.write(json.dumps(["timeout", *sys.argv[1:]]) + "\\n")
if os.environ.get("ATELIER2_TEST_CHECK_LOOKUP_TIMEOUT") == "1":
    raise SystemExit(124)
completed = subprocess.run(sys.argv[2:], env=os.environ, check=False)
raise SystemExit(completed.returncode)
""",
    )


def install_date_stub(bin_directory: Path) -> None:
    write_stub(
        bin_directory / "date",
        """\
import json
import os
import sys
from pathlib import Path

with Path(os.environ["ATELIER2_TEST_COMMAND_LOG"]).open(
    "a", encoding="utf-8"
) as handle:
    handle.write(json.dumps(["date", *sys.argv[1:]]) + "\\n")
if sys.argv[1:] != ["+%s"]:
    raise SystemExit(2)
print(os.environ["ATELIER2_TEST_NOW_SECONDS"])
""",
    )


def install_command_stubs(bin_directory: Path) -> None:
    install_curl_stub(bin_directory)
    install_gh_stub(bin_directory)
    install_logger_stub(bin_directory)
    install_flock_stub(bin_directory)
    install_timeout_stub(bin_directory)
    install_date_stub(bin_directory)


@dataclass(frozen=True)
class AutoRedeployHarness:
    tmp_path: Path
    origin: Path
    deploy: Path
    bin_directory: Path

    @classmethod
    def create(cls, tmp_path: Path) -> AutoRedeployHarness:
        origin = tmp_path / "origin"
        scripts = origin / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "auto_redeploy.sh").write_text(
            AUTO_REDEPLOY.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (scripts / "auto_redeploy.sh").chmod(0o755)
        install_serve_live_update_stub(scripts)
        (origin / "payload.txt").write_text("v1\n", encoding="utf-8")
        run_git(origin, "init", "--quiet", "--initial-branch=main")
        run_git(origin, "add", "--all")
        run_git(origin, "commit", "--quiet", "--message", "initial")

        deploy = tmp_path / "deploy"
        subprocess.run(
            ["git", "clone", "--quiet", str(origin), str(deploy)],
            env={**os.environ, **GIT_IDENTITY},
            check=True,
            capture_output=True,
            text=True,
        )
        bin_directory = tmp_path / "bin"
        bin_directory.mkdir()
        install_command_stubs(bin_directory)
        return cls(tmp_path, origin, deploy, bin_directory)

    def commit(self, content: str = "v2\n") -> str:
        (self.origin / "payload.txt").write_text(content, encoding="utf-8")
        run_git(self.origin, "add", "--all")
        run_git(self.origin, "commit", "--quiet", "--message", "update payload")
        return run_git(self.origin, "rev-parse", "HEAD")

    def commit_new_serve_live_update(self, marker: str) -> str:
        # The deploy checkout stays on the commit cloned in create(); only
        # this new commit's git object carries the marker-writing revision,
        # so a marker file proves the watcher ran the TARGET commit's script.
        install_serve_live_update_stub(self.origin / "scripts", marker=marker)
        run_git(self.origin, "add", "--all")
        run_git(
            self.origin, "commit", "--quiet", "--message", "update serve_live_update.sh"
        )
        return run_git(self.origin, "rev-parse", "HEAD")

    def seed_served_commit(self, commit: str | None = None) -> str:
        served_commit = commit or run_git(self.origin, "rev-parse", "HEAD")
        self.served_commit_file.write_text(served_commit, encoding="utf-8")
        return served_commit

    def run(self, **settings: str) -> subprocess.CompletedProcess[str]:
        environment = {
            **os.environ,
            **GIT_IDENTITY,
            "PATH": f"{self.bin_directory}{os.pathsep}{os.environ['PATH']}",
            "ATELIER2_TEST_COMMAND_LOG": str(self.command_log),
            "ATELIER2_TEST_RUN_REQUEST_COUNT": str(self.tmp_path / "run-request-count"),
            "ATELIER2_TEST_CHECK_REQUEST_COUNT": str(
                self.tmp_path / "check-request-count"
            ),
            "ATELIER2_TEST_SERVED_COMMIT_FILE": str(self.served_commit_file),
            "ATELIER2_TEST_NOW_SECONDS": "2000000000",
            "ATELIER2_TEST_BUSY_RUNS": json.dumps([run_row(BLOCKING_RUN)]),
        }
        environment.update(settings)
        return subprocess.run(
            ["bash", str(self.deploy / "scripts/auto_redeploy.sh")],
            cwd=self.deploy,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

    @property
    def served_commit_file(self) -> Path:
        return self.tmp_path / "served-commit.txt"

    @property
    def command_log(self) -> Path:
        return self.tmp_path / "commands.jsonl"

    @property
    def git_admin_directory(self) -> Path:
        return Path(run_git(self.deploy, "rev-parse", "--absolute-git-dir"))

    @property
    def staged_serve_live_update(self) -> Path:
        return self.git_admin_directory / "serve_live_update.sh"

    def checkout_status(self) -> str:
        return run_git(self.deploy, "status", "--porcelain")

    def state(self, name: str) -> int:
        path = self.git_admin_directory / name
        return int(path.read_text(encoding="utf-8")) if path.exists() else 0

    def invocations(self, command: str) -> list[list[str]]:
        if not self.command_log.exists():
            return []
        records = [
            json.loads(line)
            for line in self.command_log.read_text(encoding="utf-8").splitlines()
        ]
        return [record for record in records if record[0] == command]

    def priorities(self) -> list[str]:
        return [
            invocation[invocation.index("-p") + 1]
            for invocation in self.invocations("logger")
        ]

    def journal(self, priority: str | None = None) -> list[str]:
        """Every message the watcher logged, narrowed to one priority on request."""
        return [
            invocation[-1]
            for invocation in self.invocations("logger")
            if priority is None or invocation[invocation.index("-p") + 1] == priority
        ]


def prepare_pending_deploy(tmp_path: Path) -> tuple[AutoRedeployHarness, str, str]:
    harness = AutoRedeployHarness.create(tmp_path)
    previous_commit = harness.seed_served_commit()
    target_commit = harness.commit()
    return harness, previous_commit, target_commit


def test_a_green_commit_is_handed_to_the_loopback_update_without_the_watcher_moving_the_checkout(
    tmp_path: Path,
) -> None:
    harness, _, target_commit = prepare_pending_deploy(tmp_path)

    completed = harness.run()

    # serve_live_update_stub refuses if the checkout is already at target_commit
    # when it is invoked, so a returncode of 0 proves the watcher itself never
    # fast-forwarded the checkout; only the stub's own ff-only merge did.
    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == target_commit
    assert harness.served_commit_file.read_text(encoding="utf-8") == target_commit
    assert harness.invocations("serve_live_update") == [
        ["serve_live_update", target_commit]
    ]
    assert harness.invocations("gh")[0][1:4] == [
        "api",
        "--paginate",
        f"repos/FlexOr2/atelier-2/commits/{target_commit}/check-runs",
    ]
    assert harness.state("auto-redeploy.failures") == 0
    assert harness.state("auto-redeploy.busy") == 0
    assert not list(harness.git_admin_directory.glob("*deployed*"))


def test_the_target_commits_own_serve_live_update_script_runs_not_the_stale_checkout_copy(
    tmp_path: Path,
) -> None:
    # Git replaces a tracked file by unlink-then-create: a shell that already
    # opened the deploy checkout's serve_live_update.sh keeps reading the OLD
    # bytes until it exits. The deploy checkout here is never fast-forwarded
    # before invocation, so its on-disk serve_live_update.sh is still the
    # revision with no marker-writing logic at all; only the fetched target
    # commit's git object carries the marker. A marker file appearing proves
    # the watcher ran the TARGET commit's script, not the stale checkout copy.
    harness = AutoRedeployHarness.create(tmp_path)
    harness.seed_served_commit()
    marker_file = tmp_path / "new-script-marker.txt"
    target_commit = harness.commit_new_serve_live_update(marker="new script ran")

    completed = harness.run(ATELIER2_TEST_SERVE_SCRIPT_MARKER_FILE=str(marker_file))

    assert completed.returncode == 0, completed.stderr
    assert marker_file.read_text(encoding="utf-8") == "new script ran"
    assert run_git(harness.deploy, "rev-parse", "HEAD") == target_commit
    assert harness.served_commit_file.read_text(encoding="utf-8") == target_commit
    # The materialised script is staged under .git/, so it is never a reason
    # the checkout looks dirty (see the stub's own preflight above), and the
    # EXIT trap removes it once the tick finishes.
    assert not harness.staged_serve_live_update.exists()
    assert harness.checkout_status() == ""


def test_the_busy_check_asks_only_for_running_runs_before_and_after_the_github_checks(
    tmp_path: Path,
) -> None:
    harness, _, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run()

    assert completed.returncode == 0, completed.stderr
    run_urls = [invocation[-1] for invocation in harness.invocations("curl")][1:]
    assert (
        run_urls
        == ["http://127.0.0.1:8422/atelier/api/v1/runs?state=STARTED&limit=5"] * 2
    )


def test_a_running_run_defers_the_deploy_without_failing_or_touching_the_checkout(
    tmp_path: Path,
) -> None:
    harness, previous_commit, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run(ATELIER2_TEST_BUSY_STATE="STARTED")

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.busy") == 1
    assert harness.state("auto-redeploy.failures") == 0


@pytest.mark.parametrize("state", ("WAITING_INPUT", "WAITING_RECONCILIATION"))
def test_a_run_parked_on_a_person_never_defers_the_deploy(
    tmp_path: Path, state: str
) -> None:
    """A conversation waits for its operator for as long as it likes, and its
    answer survives the new `--application-version` the deploy hands the serve
    (`tests/integration/test_wait_survives_version_change.py`), so it is not
    work the watcher may stand behind."""

    harness, _, target_commit = prepare_pending_deploy(tmp_path)

    completed = harness.run(ATELIER2_TEST_BUSY_STATE=state)

    assert completed.returncode == 0, completed.stderr
    assert harness.invocations("serve_live_update") == [
        ["serve_live_update", target_commit]
    ]
    assert harness.state("auto-redeploy.busy") == 0


@pytest.mark.parametrize(
    ("runs", "next_page", "sentence"),
    (
        ([run_row(BLOCKING_RUN)], "", BLOCKING_RUN_SENTENCE),
        (
            [run_row({**BLOCKING_RUN, "started_at": None})],
            "",
            "cnVuLTE STARTED since an unrecorded time",
        ),
        (
            [run_row(BLOCKING_RUN)],
            "next-page-cursor",
            f"{BLOCKING_RUN_SENTENCE}, and further runs",
        ),
        (
            [run_row(BLOCKING_RUN), DEFECTIVE_RUN_ROW],
            "",
            f"{BLOCKING_RUN_SENTENCE}, and 1 defective row(s) ignored",
        ),
    ),
    ids=(
        "one-known-run",
        "a-run-without-a-start-time",
        "more-than-the-page-holds",
        "one-known-run-and-one-defective-row",
    ),
)
def test_a_busy_deferral_names_what_it_is_waiting_for(
    tmp_path: Path, runs: list[dict[str, object]], next_page: str, sentence: str
) -> None:
    harness, _, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run(
        ATELIER2_TEST_BUSY_STATE="STARTED",
        ATELIER2_TEST_BUSY_RUNS=json.dumps(runs),
        ATELIER2_TEST_BUSY_NEXT_PAGE=next_page,
    )

    assert completed.returncode == 0, completed.stderr
    assert f"busy count now 1; waiting for {sentence}" in harness.journal()


def test_a_run_starting_after_the_checks_pass_still_defers_without_touching_the_checkout(
    tmp_path: Path,
) -> None:
    harness, previous_commit, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run(
        ATELIER2_TEST_BUSY_STATE="STARTED",
        ATELIER2_TEST_BUSY_AFTER_FAST_FORWARD="1",
    )

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.busy") == 1


@pytest.mark.parametrize("failure", ("unreadable", "malformed"))
def test_an_unreadable_run_list_fails_closed_and_counts_the_tick(
    tmp_path: Path, failure: str
) -> None:
    harness, previous_commit, _ = prepare_pending_deploy(tmp_path)
    setting = f"ATELIER2_TEST_RUNS_{failure.upper()}_STATE"

    completed = harness.run(**{setting: "STARTED"})

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.failures") == 1


def test_a_run_row_with_an_unrecognised_kind_fails_closed_and_counts_the_tick(
    tmp_path: Path,
) -> None:
    """A shape the watcher does not know is a parse failure, never a silently
    ignored row -- only `kind == "defective"` (#1042) may be skipped."""
    harness, previous_commit, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run(
        ATELIER2_TEST_BUSY_STATE="STARTED",
        ATELIER2_TEST_BUSY_RUNS=json.dumps([{"kind": "unknown"}]),
    )

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.failures") == 1


def test_only_defective_rows_are_not_active_runs_and_the_deploy_proceeds(
    tmp_path: Path,
) -> None:
    harness, _, target_commit = prepare_pending_deploy(tmp_path)

    completed = harness.run(
        ATELIER2_TEST_BUSY_STATE="STARTED",
        ATELIER2_TEST_BUSY_RUNS=json.dumps([DEFECTIVE_RUN_ROW]),
    )

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == target_commit
    assert harness.state("auto-redeploy.busy") == 0


@pytest.mark.parametrize("checks", ("queued", "in_progress"))
def test_a_running_check_run_waits_without_counting_a_failure(
    tmp_path: Path, checks: str
) -> None:
    harness, previous_commit, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run(ATELIER2_TEST_CHECKS=checks)

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.failures") == 0


@pytest.mark.parametrize("checks", ("failure", "cancelled", "timed_out"))
def test_a_red_check_run_never_deploys_and_counts_a_failure(
    tmp_path: Path, checks: str
) -> None:
    harness, previous_commit, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run(ATELIER2_TEST_CHECKS=checks)

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.failures") == 1


def test_a_still_waiting_head_defers_to_a_green_predecessor(tmp_path: Path) -> None:
    harness = AutoRedeployHarness.create(tmp_path)
    harness.seed_served_commit()
    predecessor_commit = harness.commit("v2\n")
    head_commit = harness.commit("v3\n")

    completed = harness.run(ATELIER2_TEST_CHECKS_SEQUENCE="queued,green")

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == predecessor_commit
    assert harness.served_commit_file.read_text(encoding="utf-8") == predecessor_commit
    assert harness.invocations("serve_live_update") == [
        ["serve_live_update", predecessor_commit]
    ]
    assert (
        f"HEAD {head_commit} still waiting or red; "
        f"deploying newest green ancestor {predecessor_commit}"
    ) in harness.journal()


def test_a_green_head_deploys_without_even_checking_a_red_predecessor(
    tmp_path: Path,
) -> None:
    harness = AutoRedeployHarness.create(tmp_path)
    harness.seed_served_commit()
    harness.commit("v2\n")  # predecessor would classify red, but must never be queried
    head_commit = harness.commit("v3\n")

    completed = harness.run(ATELIER2_TEST_CHECKS_SEQUENCE="green,failure")

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == head_commit
    assert harness.invocations("serve_live_update") == [
        ["serve_live_update", head_commit]
    ]
    assert len(harness.invocations("gh")) == 1


def test_every_candidate_still_pending_defers_without_failing(tmp_path: Path) -> None:
    harness = AutoRedeployHarness.create(tmp_path)
    previous_commit = harness.seed_served_commit()
    harness.commit("v2\n")
    harness.commit("v3\n")

    completed = harness.run(ATELIER2_TEST_CHECKS_SEQUENCE="queued,queued")

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.failures") == 0


def test_the_newest_green_ancestor_already_served_is_a_no_op(tmp_path: Path) -> None:
    harness = AutoRedeployHarness.create(tmp_path)
    initial_commit = run_git(harness.deploy, "rev-parse", "HEAD")
    served_commit = harness.commit("v2\n")
    harness.seed_served_commit(served_commit)
    head_commit = harness.commit("v3\n")  # newest, still red or pending; never deployed

    completed = harness.run(ATELIER2_TEST_CHECKS_SEQUENCE="failure,green")

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == initial_commit
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.failures") == 0
    assert (
        f"HEAD {head_commit} is still waiting or red, and its newest green "
        f"ancestor {served_commit} is already served; nothing to deploy"
    ) in harness.journal("user.warning")


def test_an_unreadable_ancestry_check_refuses_the_tick_instead_of_deploying(
    tmp_path: Path,
) -> None:
    # merge-base --is-ancestor exits >1 (not just the ordinary 0/1) when it
    # cannot even read one of the objects, such as a served commit the local
    # clone never fetched -- that must never be read as "not an ancestor,
    # deploy the green ancestor anyway".
    harness = AutoRedeployHarness.create(tmp_path)
    unreadable_served_commit = "f" * 40
    harness.seed_served_commit(unreadable_served_commit)
    head_commit = harness.commit("v2\n")

    completed = harness.run(ATELIER2_TEST_CHECKS="green")

    assert completed.returncode == 0, completed.stderr
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.failures") == 1
    assert (
        f"cannot determine whether {head_commit} is an ancestor of served "
        f"commit {unreadable_served_commit} (git merge-base --is-ancestor "
        "exited 128)"
    ) in harness.journal()


def test_neutral_and_skipped_check_runs_are_not_red(tmp_path: Path) -> None:
    harness, _, target_commit = prepare_pending_deploy(tmp_path)

    completed = harness.run(ATELIER2_TEST_CHECKS="neutral_and_skipped")

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == target_commit
    assert harness.invocations("serve_live_update") == [
        ["serve_live_update", target_commit]
    ]


def test_no_check_runs_wait_during_the_appearance_grace_period(
    tmp_path: Path,
) -> None:
    harness, previous_commit, target_commit = prepare_pending_deploy(tmp_path)
    commit_time = int(
        run_git(harness.origin, "show", "-s", "--format=%ct", target_commit)
    )

    completed = harness.run(
        ATELIER2_TEST_CHECKS="none",
        ATELIER2_TEST_NOW_SECONDS=str(commit_time + 1799),
    )

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.state("auto-redeploy.failures") == 0


def test_no_check_runs_after_the_appearance_deadline_count_as_red(
    tmp_path: Path,
) -> None:
    harness, previous_commit, target_commit = prepare_pending_deploy(tmp_path)
    commit_time = int(
        run_git(harness.origin, "show", "-s", "--format=%ct", target_commit)
    )

    completed = harness.run(
        ATELIER2_TEST_CHECKS="none",
        ATELIER2_TEST_NOW_SECONDS=str(commit_time + 1800),
    )

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.state("auto-redeploy.failures") == 1


def test_check_run_lookup_is_bounded_to_sixty_seconds_and_fails_closed(
    tmp_path: Path,
) -> None:
    harness, previous_commit, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run(ATELIER2_TEST_CHECK_LOOKUP_TIMEOUT="1")

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.state("auto-redeploy.failures") == 1
    assert harness.invocations("timeout")[0][1] == "60"


def test_only_the_third_failure_and_hourly_repeats_fail_the_unit(
    tmp_path: Path,
) -> None:
    harness, _, _ = prepare_pending_deploy(tmp_path)

    first = harness.run(
        ATELIER2_TEST_CHECKS="failure", ATELIER2_TEST_NOW_SECONDS="10000"
    )
    second = harness.run(
        ATELIER2_TEST_CHECKS="failure", ATELIER2_TEST_NOW_SECONDS="10001"
    )
    third = harness.run(
        ATELIER2_TEST_CHECKS="failure", ATELIER2_TEST_NOW_SECONDS="10002"
    )
    suppressed = harness.run(
        ATELIER2_TEST_CHECKS="failure", ATELIER2_TEST_NOW_SECONDS="13601"
    )
    repeated = harness.run(
        ATELIER2_TEST_CHECKS="failure", ATELIER2_TEST_NOW_SECONDS="13602"
    )

    assert [run.returncode for run in (first, second, third, suppressed, repeated)] == [
        0,
        0,
        1,
        0,
        1,
    ]
    assert harness.state("auto-redeploy.failures") == 5
    assert harness.priorities().count("user.err") == 2
    assert (harness.git_admin_directory / "auto-redeploy.last-alert").exists()
    assert all(
        invocation[1:3] == ["-t", "atelier2-autodeploy"]
        for invocation in harness.invocations("logger")
    )


def test_the_tenth_busy_tick_says_loudly_what_blocks_it_and_repeats_hourly(
    tmp_path: Path,
) -> None:
    harness, _, _ = prepare_pending_deploy(tmp_path)

    def defer(now_seconds: str) -> subprocess.CompletedProcess[str]:
        return harness.run(
            ATELIER2_TEST_BUSY_STATE="STARTED",
            ATELIER2_TEST_NOW_SECONDS=now_seconds,
        )

    quiet = [defer("10000") for _ in range(9)]
    tenth = defer("10001")
    suppressed = defer("13600")
    repeated = defer("13602")

    assert all(run.returncode == 0 for run in (*quiet, tenth, suppressed, repeated))
    assert harness.state("auto-redeploy.busy") == 12
    assert harness.journal("user.warning") == [
        f"ALERT: deploy deferred on 10 ticks in a row, waiting for {BLOCKING_RUN_SENTENCE}",
        f"ALERT: deploy deferred on 12 ticks in a row, waiting for {BLOCKING_RUN_SENTENCE}",
    ]
    assert "user.err" not in harness.priorities()


@pytest.mark.parametrize("guard", ("dirty", "branch"))
def test_dirty_or_non_main_checkouts_warn_and_count_without_failing(
    tmp_path: Path, guard: str
) -> None:
    harness, _, _ = prepare_pending_deploy(tmp_path)
    if guard == "dirty":
        (harness.deploy / "local.txt").write_text("operator work\n", encoding="utf-8")
    else:
        run_git(harness.deploy, "switch", "--quiet", "-c", "operator-work")

    completed = harness.run()

    assert completed.returncode == 0, completed.stderr
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.failures") == 1
    assert "user.warning" in harness.priorities()


def test_successful_deploy_and_nothing_to_do_reset_both_counters(
    tmp_path: Path,
) -> None:
    harness, _, target_commit = prepare_pending_deploy(tmp_path)
    admin = harness.git_admin_directory
    alert_files = (
        admin / "auto-redeploy.last-alert",
        admin / "auto-redeploy.last-busy-alert",
    )

    def seed_a_standing_streak() -> None:
        (admin / "auto-redeploy.failures").write_text("2", encoding="utf-8")
        (admin / "auto-redeploy.busy").write_text("9", encoding="utf-8")
        for alert_file in alert_files:
            alert_file.write_text("10000", encoding="utf-8")

    seed_a_standing_streak()
    deployed = harness.run()
    seed_a_standing_streak()
    current = harness.run()

    assert deployed.returncode == 0, deployed.stderr
    assert current.returncode == 0, current.stderr
    assert harness.served_commit_file.read_text(encoding="utf-8") == target_commit
    assert harness.state("auto-redeploy.failures") == 0
    assert harness.state("auto-redeploy.busy") == 0
    assert not any(alert_file.exists() for alert_file in alert_files)
    assert any(
        "already current" in " ".join(call) for call in harness.invocations("logger")
    )


def test_a_failed_loopback_update_counts_without_calling_a_container_command(
    tmp_path: Path,
) -> None:
    harness, _, target_commit = prepare_pending_deploy(tmp_path)

    completed = harness.run(ATELIER2_TEST_UPDATE_FAILS="1")

    assert completed.returncode == 0
    assert harness.invocations("serve_live_update") == [
        ["serve_live_update", target_commit]
    ]
    assert harness.state("auto-redeploy.failures") == 1
    assert any(
        "(exit 1)" in " ".join(invocation)
        for invocation in harness.invocations("logger")
    )
    assert "container_live" not in AUTO_REDEPLOY.read_text(encoding="utf-8")
    assert "deployed.sha" not in AUTO_REDEPLOY.read_text(encoding="utf-8")
    # The EXIT trap's staged-file cleanup must never mask the tick's real
    # exit code: three ticks in a row still fail the unit on the third, the
    # same threshold test_only_the_third_failure_and_hourly_repeats_fail_the_unit
    # pins for a red GitHub check.
    assert harness.run(ATELIER2_TEST_UPDATE_FAILS="1").returncode == 0
    assert harness.run(ATELIER2_TEST_UPDATE_FAILS="1").returncode == 1
    assert harness.state("auto-redeploy.failures") == 3
    assert not harness.staged_serve_live_update.exists()
    assert harness.checkout_status() == ""


def test_a_refused_workflow_intake_is_a_warning_not_a_failure_tick(
    tmp_path: Path,
) -> None:
    harness, _, target_commit = prepare_pending_deploy(tmp_path)
    admin = harness.git_admin_directory
    (admin / "auto-redeploy.failures").write_text("2", encoding="utf-8")

    completed = harness.run(ATELIER2_TEST_UPDATE_INTAKE_REFUSED="1")

    assert completed.returncode == 0, completed.stderr
    assert harness.invocations("serve_live_update") == [
        ["serve_live_update", target_commit]
    ]
    assert harness.served_commit_file.read_text(encoding="utf-8") == target_commit
    assert harness.state("auto-redeploy.failures") == 0
    assert not harness.staged_serve_live_update.exists()
    assert harness.checkout_status() == ""
    assert "user.warning" in harness.priorities()
    assert any(
        f"main now served at {target_commit}; workflow intake refused"
        in " ".join(invocation)
        for invocation in harness.invocations("logger")
    )


def test_lock_contention_skips_the_tick_before_fetching_or_polling(
    tmp_path: Path,
) -> None:
    harness, previous_commit, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run(ATELIER2_TEST_LOCK_BUSY="1")

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.invocations("curl") == []
    assert harness.invocations("gh") == []
    assert harness.invocations("serve_live_update") == []
    assert (harness.git_admin_directory / "auto-redeploy.lock").exists()


def test_the_service_declares_the_timer_execution_path() -> None:
    service = AUTO_REDEPLOY_SERVICE.read_text(encoding="utf-8")

    assert "Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin" in service


def test_scripts_do_not_set_ambient_container_mode() -> None:
    script = AUTO_REDEPLOY.read_text(encoding="utf-8")
    for forbidden in (
        "ATELIER2_DEPLOYMENT=",
        "ATELIER2_PUBLISHED_PORT=",
        "ATELIER2_RESTART_POLICY=",
    ):
        assert forbidden not in script
