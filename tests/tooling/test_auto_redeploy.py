from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUTO_REDEPLOY = PROJECT_ROOT / "scripts" / "auto_redeploy.sh"
AUTO_REDEPLOY_SERVICE = PROJECT_ROOT / "scripts" / "atelier2-auto-redeploy.service"

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


def install_serve_live_update_stub(scripts_directory: Path) -> None:
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

repository = Path(__file__).resolve().parent.parent
commit = subprocess.run(
    ["git", "-C", str(repository), "rev-parse", "HEAD"],
    check=True,
    capture_output=True,
    text=True,
).stdout.strip()
Path(os.environ["ATELIER2_TEST_SERVED_COMMIT_FILE"]).write_text(
    commit, encoding="utf-8"
)
print(f"serve live update: now serves {commit}")
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
if query.get("limit") != ["1"]:
    raise SystemExit(2)

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
is_second_check = request_count >= 3
is_busy = state == busy_state and (is_second_check or not busy_after_first_check)
print(json.dumps({"items": [{"id": "run-1"}] if is_busy else []}))
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
checks = scenarios[os.environ.get("ATELIER2_TEST_CHECKS", "green")]
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
            "ATELIER2_TEST_SERVED_COMMIT_FILE": str(self.served_commit_file),
            "ATELIER2_TEST_NOW_SECONDS": "2000000000",
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


def prepare_pending_deploy(tmp_path: Path) -> tuple[AutoRedeployHarness, str, str]:
    harness = AutoRedeployHarness.create(tmp_path)
    previous_commit = harness.seed_served_commit()
    target_commit = harness.commit()
    return harness, previous_commit, target_commit


def test_a_green_commit_is_fast_forwarded_and_given_to_the_loopback_update(
    tmp_path: Path,
) -> None:
    harness, _, target_commit = prepare_pending_deploy(tmp_path)

    completed = harness.run()

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == target_commit
    assert harness.served_commit_file.read_text(encoding="utf-8") == target_commit
    assert harness.invocations("serve_live_update") == [["serve_live_update"]]
    assert harness.invocations("gh")[0][1:4] == [
        "api",
        "--paginate",
        f"repos/FlexOr2/atelier-2/commits/{target_commit}/check-runs",
    ]
    assert harness.state("auto-redeploy.failures") == 0
    assert harness.state("auto-redeploy.busy") == 0
    assert not list(harness.git_admin_directory.glob("*deployed*"))


def test_busy_checks_cover_every_run_state_before_and_after_the_fast_forward(
    tmp_path: Path,
) -> None:
    harness, _, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run()

    assert completed.returncode == 0, completed.stderr
    run_urls = [invocation[-1] for invocation in harness.invocations("curl")][1:]
    assert (
        run_urls
        == [
            "http://127.0.0.1:8422/atelier/api/v1/runs?state=STARTED&limit=1",
            "http://127.0.0.1:8422/atelier/api/v1/runs?state=WAITING_INPUT&limit=1",
            "http://127.0.0.1:8422/atelier/api/v1/runs?state=WAITING_RECONCILIATION&limit=1",
        ]
        * 2
    )


@pytest.mark.parametrize(
    "state", ("STARTED", "WAITING_INPUT", "WAITING_RECONCILIATION")
)
def test_each_busy_run_state_defers_without_failing_or_touching_the_checkout(
    tmp_path: Path, state: str
) -> None:
    harness, previous_commit, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run(ATELIER2_TEST_BUSY_STATE=state)

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.busy") == 1
    assert harness.state("auto-redeploy.failures") == 0


def test_a_run_starting_after_fast_forward_still_defers_the_update(
    tmp_path: Path,
) -> None:
    harness, _, target_commit = prepare_pending_deploy(tmp_path)

    completed = harness.run(
        ATELIER2_TEST_BUSY_STATE="STARTED",
        ATELIER2_TEST_BUSY_AFTER_FAST_FORWARD="1",
    )

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == target_commit
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.busy") == 1


@pytest.mark.parametrize("failure", ("unreadable", "malformed"))
def test_an_unreadable_run_list_fails_closed_and_counts_the_tick(
    tmp_path: Path, failure: str
) -> None:
    harness, previous_commit, _ = prepare_pending_deploy(tmp_path)
    setting = f"ATELIER2_TEST_RUNS_{failure.upper()}_STATE"

    completed = harness.run(**{setting: "WAITING_INPUT"})

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == previous_commit
    assert harness.invocations("serve_live_update") == []
    assert harness.state("auto-redeploy.failures") == 1


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


def test_neutral_and_skipped_check_runs_are_not_red(tmp_path: Path) -> None:
    harness, _, target_commit = prepare_pending_deploy(tmp_path)

    completed = harness.run(ATELIER2_TEST_CHECKS="neutral_and_skipped")

    assert completed.returncode == 0, completed.stderr
    assert run_git(harness.deploy, "rev-parse", "HEAD") == target_commit
    assert harness.invocations("serve_live_update") == [["serve_live_update"]]


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


def test_the_thirtieth_busy_tick_warns_without_failing_the_unit(
    tmp_path: Path,
) -> None:
    harness, _, _ = prepare_pending_deploy(tmp_path)

    completed = [harness.run(ATELIER2_TEST_BUSY_STATE="STARTED") for _ in range(30)]

    assert all(run.returncode == 0 for run in completed)
    assert harness.state("auto-redeploy.busy") == 30
    assert harness.priorities().count("user.warning") == 1
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
    (admin / "auto-redeploy.failures").write_text("2", encoding="utf-8")
    (admin / "auto-redeploy.busy").write_text("29", encoding="utf-8")

    deployed = harness.run()
    (admin / "auto-redeploy.failures").write_text("2", encoding="utf-8")
    (admin / "auto-redeploy.busy").write_text("29", encoding="utf-8")
    current = harness.run()

    assert deployed.returncode == 0, deployed.stderr
    assert current.returncode == 0, current.stderr
    assert harness.served_commit_file.read_text(encoding="utf-8") == target_commit
    assert harness.state("auto-redeploy.failures") == 0
    assert harness.state("auto-redeploy.busy") == 0
    assert any(
        "already current" in " ".join(call) for call in harness.invocations("logger")
    )


def test_a_failed_loopback_update_counts_without_calling_a_container_command(
    tmp_path: Path,
) -> None:
    harness, _, _ = prepare_pending_deploy(tmp_path)

    completed = harness.run(ATELIER2_TEST_UPDATE_FAILS="1")

    assert completed.returncode == 0
    assert harness.invocations("serve_live_update") == [["serve_live_update"]]
    assert harness.state("auto-redeploy.failures") == 1
    assert any(
        "(exit 1)" in " ".join(invocation)
        for invocation in harness.invocations("logger")
    )
    assert "container_live" not in AUTO_REDEPLOY.read_text(encoding="utf-8")
    assert "deployed.sha" not in AUTO_REDEPLOY.read_text(encoding="utf-8")


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
