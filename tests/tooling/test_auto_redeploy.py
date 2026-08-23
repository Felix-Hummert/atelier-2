from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUTO_REDEPLOY = PROJECT_ROOT / "scripts" / "auto_redeploy.sh"

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


def commit_file(repository: Path, name: str, content: str) -> str:
    (repository / name).write_text(content, encoding="utf-8")
    run_git(repository, "add", "--all")
    run_git(repository, "commit", "--quiet", "--message", f"update {name}")
    return run_git(repository, "rev-parse", "HEAD")


def install_container_live_stub(scripts_directory: Path) -> None:
    """A `container_live.sh` double: its own `update` behaviour, not real
    Docker or the store migration ladder, is scripts/container_live.sh's own
    contract (tests/tooling/test_container_live.py already proves that
    contract) -- this double only proves what auto_redeploy.sh does with the
    exit code and the commit `update` claims to have landed.
    """
    write_stub(
        scripts_directory / "container_live.sh",
        """\
import os
import subprocess
import sys
from pathlib import Path

served_path = Path(os.environ["ATELIER2_TEST_SERVED_COMMIT_FILE"])
log_path = Path(os.environ["ATELIER2_TEST_CONTAINER_LIVE_LOG"])
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")

command = sys.argv[1] if len(sys.argv) > 1 else ""
if command != "update":
    print(f"container live stub: unsupported command {command!r}", file=sys.stderr)
    raise SystemExit(2)

repository = Path(__file__).resolve().parent.parent
new_commit = subprocess.run(
    ["git", "-C", str(repository), "rev-parse", "HEAD"],
    check=True, capture_output=True, text=True,
).stdout.strip()

if os.environ.get("ATELIER2_TEST_UPDATE_UNHEALTHY") == "1":
    print("container live: updated container is not healthy", file=sys.stderr)
    raise SystemExit(1)

served_path.write_text(new_commit, encoding="utf-8")
print("container live: cockpit -> http://127.0.0.1:8422/atelier/")
""",
    )


def install_curl_stub(bin_directory: Path) -> None:
    """A `curl` double for the health endpoint compose.yaml's own healthcheck
    also targets: it reports whatever commit `ATELIER2_TEST_SERVED_COMMIT_FILE`
    currently names, exactly as the real endpoint reports whatever commit the
    running container was built from.
    """
    write_stub(
        bin_directory / "curl",
        """\
import json
import os
import sys
from pathlib import Path

served_path = Path(os.environ["ATELIER2_TEST_SERVED_COMMIT_FILE"])
if os.environ.get("ATELIER2_TEST_HEALTH_UNREACHABLE") == "1" or not served_path.exists():
    raise SystemExit(7)
if os.environ.get("ATELIER2_TEST_HEALTH_MALFORMED") == "1":
    print("not the expected health contract at all")
    raise SystemExit(0)
commit = served_path.read_text(encoding="utf-8").strip()
status = os.environ.get("ATELIER2_TEST_HEALTH_STATUS", "serving")
print(json.dumps({"status": status, "source_commit": commit, "source_tree": "0" * 40}))
""",
    )


def deploy_repository_pair(tmp_path: Path) -> tuple[Path, Path]:
    """An `origin` repository carrying auto_redeploy.sh and a container_live.sh
    double, cloned into a `deploy` checkout the way a real host clones this
    repository once and then only ever fast-forwards it.
    """
    origin = tmp_path / "origin"
    (origin / "scripts").mkdir(parents=True)
    scripts = origin / "scripts"
    (scripts / "auto_redeploy.sh").write_text(
        AUTO_REDEPLOY.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (scripts / "auto_redeploy.sh").chmod(0o755)
    install_container_live_stub(scripts)
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
    return origin, deploy


def run_auto_redeploy(
    deploy: Path, tmp_path: Path, **settings: str
) -> subprocess.CompletedProcess[str]:
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(exist_ok=True)
    install_curl_stub(bin_directory)
    environment = {
        **os.environ,
        **GIT_IDENTITY,
        "PATH": f"{bin_directory}{os.pathsep}{os.environ['PATH']}",
        "ATELIER2_TEST_SERVED_COMMIT_FILE": str(tmp_path / "served-commit.txt"),
        "ATELIER2_TEST_CONTAINER_LIVE_LOG": str(tmp_path / "container-live.log"),
    }
    environment.update(settings)
    return subprocess.run(
        ["bash", str(deploy / "scripts/auto_redeploy.sh")],
        cwd=deploy,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def seed_served_commit(tmp_path: Path, commit: str) -> None:
    (tmp_path / "served-commit.txt").write_text(commit, encoding="utf-8")


def container_live_invocations(tmp_path: Path) -> list[str]:
    log_path = tmp_path / "container-live.log"
    if not log_path.exists():
        return []
    return log_path.read_text(encoding="utf-8").splitlines()


def test_up_to_date_serve_triggers_no_redeploy(tmp_path: Path) -> None:
    origin, deploy = deploy_repository_pair(tmp_path)
    current_commit = run_git(origin, "rev-parse", "HEAD")
    seed_served_commit(tmp_path, current_commit)

    completed = run_auto_redeploy(deploy, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert "already current" in completed.stdout
    assert container_live_invocations(tmp_path) == []
    assert run_git(deploy, "rev-parse", "HEAD") == current_commit
    assert (tmp_path / "served-commit.txt").read_text(
        encoding="utf-8"
    ).strip() == current_commit


def test_a_new_commit_on_main_is_pulled_and_served(tmp_path: Path) -> None:
    origin, deploy = deploy_repository_pair(tmp_path)
    old_commit = run_git(origin, "rev-parse", "HEAD")
    seed_served_commit(tmp_path, old_commit)
    new_commit = commit_file(origin, "payload.txt", "v2\n")

    completed = run_auto_redeploy(deploy, tmp_path)

    assert completed.returncode == 0, completed.stderr
    assert f"main now served at {new_commit}" in completed.stdout
    assert run_git(deploy, "rev-parse", "HEAD") == new_commit
    assert (tmp_path / "served-commit.txt").read_text(
        encoding="utf-8"
    ).strip() == new_commit
    assert container_live_invocations(tmp_path) == ["update"]


def test_a_red_health_check_after_update_leaves_the_previous_commit_served(
    tmp_path: Path,
) -> None:
    origin, deploy = deploy_repository_pair(tmp_path)
    old_commit = run_git(origin, "rev-parse", "HEAD")
    seed_served_commit(tmp_path, old_commit)
    commit_file(origin, "payload.txt", "v2\n")

    completed = run_auto_redeploy(deploy, tmp_path, ATELIER2_TEST_UPDATE_UNHEALTHY="1")

    assert completed.returncode != 0
    assert "not healthy" in completed.stderr
    assert "refused" in completed.stderr
    assert (tmp_path / "served-commit.txt").read_text(
        encoding="utf-8"
    ).strip() == old_commit
    assert container_live_invocations(tmp_path) == ["update"]


def test_an_unreachable_health_check_refuses_without_touching_the_checkout(
    tmp_path: Path,
) -> None:
    origin, deploy = deploy_repository_pair(tmp_path)
    old_commit = run_git(origin, "rev-parse", "HEAD")
    commit_file(origin, "payload.txt", "v2\n")

    completed = run_auto_redeploy(
        deploy, tmp_path, ATELIER2_TEST_HEALTH_UNREACHABLE="1"
    )

    assert completed.returncode != 0
    assert "health check is unavailable" in completed.stderr
    assert run_git(deploy, "rev-parse", "HEAD") == old_commit
    assert container_live_invocations(tmp_path) == []


def test_a_malformed_health_response_refuses_cleanly_without_touching_the_checkout(
    tmp_path: Path,
) -> None:
    origin, deploy = deploy_repository_pair(tmp_path)
    old_commit = run_git(origin, "rev-parse", "HEAD")
    commit_file(origin, "payload.txt", "v2\n")

    completed = run_auto_redeploy(deploy, tmp_path, ATELIER2_TEST_HEALTH_MALFORMED="1")

    assert completed.returncode != 0
    assert "health check is unavailable" in completed.stderr
    assert run_git(deploy, "rev-parse", "HEAD") == old_commit
    assert container_live_invocations(tmp_path) == []


def test_scripts_do_not_set_ambient_container_mode() -> None:
    script = AUTO_REDEPLOY.read_text(encoding="utf-8")
    for forbidden in (
        "ATELIER2_DEPLOYMENT=",
        "ATELIER2_PUBLISHED_PORT=",
        "ATELIER2_RESTART_POLICY=",
    ):
        assert forbidden not in script
