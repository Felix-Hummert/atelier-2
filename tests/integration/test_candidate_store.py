"""What an attempt made outlives the directory it made it in, or nothing claims it.

Every test here runs against real git repositories under the test's own temporary
directory and touches no network: what the store promises -- that a candidate is
still readable when the workspace and even the operator's checkout are gone -- is
only worth something if git itself says so.

Two properties carry the rest. The candidate is what the pinned checkout would
have staged, so content a project ignores but tracks is kept rather than silently
dropped. And one attempt names one piece of work: capturing the same work twice
is the same fact stated twice, capturing different work under the same attempt is
a contradiction and is refused.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from atelier2.adapters.candidate_store import (
    CANDIDATE_STORE_DIRECTORY_NAME,
    GitCandidateTreeStore,
)
from atelier2.adapters.leased_directory import LeasedDirectoryChanged
from atelier2.adapters.project_source import LocalGitProjectSource
from atelier2.contracts.agent_attempts import AgentAttemptId
from atelier2.contracts.project_sources import CandidateTree
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.candidate_store import (
    CandidateCaptureConflict,
    CandidateTreeUnrepresentable,
)
from tests.scenarios.agents import leased_directory_identity
from tests.scenarios.projects import commit_to_project, git_project

COMMITTED = "print('committed')\n"
WORKED_ON = "print('what the agent made')\n"
KEPT_OUT_OF_GIT = "*.secret\n"
TRACKED_BUT_IGNORED = "the value a run must not lose\n"

AN_ATTEMPT = AgentAttemptId("a1" * 32)
ANOTHER_ATTEMPT = AgentAttemptId("b2" * 32)

HOSTILE_GLOBAL_CONFIGURATION = (
    '[filter "canary"]\n\tclean = sed s/./X/g\n\tsmudge = cat\n'
)
"""A host that rewrites content on the way into git, as git-lfs and hooks do."""


class Project:
    """One checkout, its candidate store, and the workspaces an attempt leases."""

    def __init__(self, tmp_path: Path, files: dict[str, str]) -> None:
        self.checkout = tmp_path / "checkout"
        self.root = tmp_path / "project-root"
        self.root.mkdir()
        self.pin = git_project(self.checkout, files)
        self.store_path = self.root / CANDIDATE_STORE_DIRECTORY_NAME
        self.store = GitCandidateTreeStore(self.checkout, self.root / "atelier.sqlite")
        self._leases = 0

    def commit(self, files: dict[str, str]) -> None:
        """Take one more commit into the checkout and pin the run to that one."""

        self.pin = commit_to_project(self.checkout, files)

    def workspace(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        """A lease holding exactly the pinned tree, as a real attempt is given."""

        self._leases += 1
        lease = leased_directory_identity(
            attempt_id, self.checkout.parent / f"lease-{self._leases}"
        )
        LocalGitProjectSource(self.checkout).materialize(self.pin, lease)
        return lease


def carried(store_path: Path, tree: str) -> dict[str, str]:
    """Every path under this tree in the store, and the text each one holds."""

    listed = asked_of_git(store_path, ("ls-tree", "-r", "--name-only", tree))
    return {
        path: asked_of_git(store_path, ("cat-file", "-p", f"{tree}:{path}"))
        for path in listed.splitlines()
    }


def asked_of_git(repository: Path, arguments: tuple[str, ...]) -> str:
    """What git itself says about a repository, asked outside the code under test."""

    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
    )
    return completed.stdout.decode("utf-8")


def test_the_store_lives_in_the_project_root_beside_its_database(
    tmp_path: Path,
) -> None:
    """A project keeps nothing of its own outside its own root (ADR 0011)."""

    project = Project(tmp_path, {"tool.py": COMMITTED})

    project.store.capture(project.pin, project.workspace(AN_ATTEMPT))

    assert project.store_path.is_dir()


def test_capturing_writes_nothing_into_the_operator_checkout(tmp_path: Path) -> None:
    """The checkout is read for what a run was pinned to, and never written."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    standing = asked_of_git(project.checkout, ("status", "--porcelain"))

    project.store.capture(project.pin, project.workspace(AN_ATTEMPT))

    assert asked_of_git(project.checkout, ("rev-parse", "HEAD")).strip() == (
        project.pin.commit
    )
    assert asked_of_git(project.checkout, ("status", "--porcelain")) == standing
    assert asked_of_git(project.checkout, ("for-each-ref", "refs/atelier")) == ""


def test_the_captured_tree_is_the_work_the_attempt_left_in_its_workspace(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path, {"tool.py": COMMITTED, "kept.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    (lease.working_directory / "tool.py").write_text(WORKED_ON, encoding="utf-8")
    (lease.working_directory / "added.py").write_text(WORKED_ON, encoding="utf-8")

    candidate = project.store.capture(project.pin, lease)

    assert candidate.attempt_id == AN_ATTEMPT
    assert carried(project.store_path, candidate.tree) == {
        "tool.py": WORKED_ON,
        "added.py": WORKED_ON,
        "kept.py": COMMITTED,
    }


def test_the_candidate_is_readable_when_workspace_and_checkout_are_gone(
    tmp_path: Path,
) -> None:
    """The store stands on itself: it is where the work of a run now lives."""

    project = Project(tmp_path, {"deep/nested/tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)

    candidate = project.store.capture(project.pin, lease)

    shutil.rmtree(project.checkout)
    shutil.rmtree(lease.working_directory)
    assert carried(project.store_path, candidate.tree) == {
        "deep/nested/tool.py": COMMITTED
    }


def test_a_file_the_project_tracks_but_ignores_survives_the_capture(
    tmp_path: Path,
) -> None:
    """The index is seeded from the pin, so ignored history is not lost silently."""

    project = Project(
        tmp_path, {"keys.secret": TRACKED_BUT_IGNORED, "tool.py": COMMITTED}
    )
    project.commit({".gitignore": KEPT_OUT_OF_GIT})

    candidate = project.store.capture(project.pin, project.workspace(AN_ATTEMPT))

    assert carried(project.store_path, candidate.tree)["keys.secret"] == (
        TRACKED_BUT_IGNORED
    )


def test_a_file_the_agent_newly_ignored_is_no_change_as_it_would_be_in_git(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path, {".gitignore": KEPT_OUT_OF_GIT})
    lease = project.workspace(AN_ATTEMPT)
    (lease.working_directory / "scratch.secret").write_text(WORKED_ON, encoding="utf-8")

    candidate = project.store.capture(project.pin, lease)

    assert carried(project.store_path, candidate.tree) == {
        ".gitignore": KEPT_OUT_OF_GIT
    }


def test_capturing_the_same_work_twice_states_the_same_candidate_twice(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)

    first = project.store.capture(project.pin, lease)
    again = project.store.capture(project.pin, lease)

    assert again == first
    assert project.store.read(AN_ATTEMPT) == first


def test_one_attempt_claiming_other_work_is_refused_rather_than_overwritten(
    tmp_path: Path,
) -> None:
    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    anchored = project.store.capture(project.pin, lease)
    (lease.working_directory / "tool.py").write_text(WORKED_ON, encoding="utf-8")

    with pytest.raises(CandidateCaptureConflict, match=anchored.tree):
        project.store.capture(project.pin, lease)

    assert project.store.read(AN_ATTEMPT) == anchored


def test_a_nested_repository_is_refused_before_any_candidate_is_written(
    tmp_path: Path,
) -> None:
    """A gitlink names a commit this store has never seen, so it names nothing."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    git_project(lease.working_directory / "vendored", {"other.py": COMMITTED})

    with pytest.raises(CandidateTreeUnrepresentable, match="vendored"):
        project.store.capture(project.pin, lease)

    assert project.store.read(AN_ATTEMPT) is None


def test_a_directory_swapped_under_its_lease_is_never_captured_from(
    tmp_path: Path,
) -> None:
    """Capturing happens in the same window the lease identity exists to close."""

    project = Project(tmp_path, {"tool.py": COMMITTED})
    lease = project.workspace(AN_ATTEMPT)
    impostor = tmp_path / "impostor"
    impostor.mkdir()
    shutil.rmtree(lease.working_directory)
    impostor.rename(lease.working_directory)

    with pytest.raises(LeasedDirectoryChanged):
        project.store.capture(project.pin, lease)

    assert project.store.read(AN_ATTEMPT) is None


def test_no_candidate_is_claimed_for_an_attempt_that_captured_none(
    tmp_path: Path,
) -> None:
    """Before the first capture there is no store yet, and that is an answer too."""

    project = Project(tmp_path, {"tool.py": COMMITTED})

    assert project.store.read(ANOTHER_ATTEMPT) is None

    project.store.capture(project.pin, project.workspace(AN_ATTEMPT))

    assert project.store.read(ANOTHER_ATTEMPT) is None


def test_the_host_git_configuration_cannot_rewrite_what_a_candidate_carries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `clean` filter of the machine is no part of any project's work."""

    project = Project(
        tmp_path, {".gitattributes": "* filter=canary\n", "tool.py": COMMITTED}
    )
    hostile = tmp_path / "hostile-gitconfig"
    hostile.write_text(HOSTILE_GLOBAL_CONFIGURATION, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    assert _would_be_rewritten(project.checkout), "the canary filter never fired"

    candidate = project.store.capture(project.pin, project.workspace(AN_ATTEMPT))

    assert carried(project.store_path, candidate.tree)["tool.py"] == COMMITTED


def _would_be_rewritten(checkout: Path) -> bool:
    """Whether the ambient configuration changes content on its way into git."""

    hashed = tuple(
        subprocess.run(
            (
                "git",
                "-C",
                str(checkout),
                "hash-object",
                "--stdin",
                *path_arguments,
            ),
            input=COMMITTED.encode("utf-8"),
            env={**os.environ, "GIT_CONFIG_SYSTEM": os.devnull},
            capture_output=True,
            check=True,
        ).stdout
        for path_arguments in ((), ("--path", "tool.py"))
    )
    return hashed[0] != hashed[1]


def test_a_candidate_tree_names_a_real_object_or_is_no_candidate_at_all() -> None:
    """What git answered is read at a boundary, so a non-answer never becomes one."""

    with pytest.raises(ValueError, match="candidate tree"):
        CandidateTree(AN_ATTEMPT, "the work the agent did")
