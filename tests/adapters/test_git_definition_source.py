"""What a git definition source answers, asked of real repositories.

Every repository here is built with git's own plumbing under the test's
temporary directory, because what this reader is about is what git says: a tree
entry's mode, the commit a ref resolves to, and the exact bytes of a blob. A
double would only ever repeat this reader's own assumptions back to it.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from atelier2.adapters.git_definition_source import GitDefinitionSource
from atelier2.contracts.definition_sources import (
    DefinitionSourceAccess,
    DefinitionSourceActor,
    DefinitionSourceConfiguration,
    DefinitionSourceKind,
    DefinitionSourceRefusal,
    DefinitionSourceSelection,
    RepositoryLocation,
    RepositoryRef,
    SelectionPattern,
)
from atelier2.contracts.revisions_v3 import RevisionKind
from atelier2.ports.definition_sources import DefinitionSourceUnreadable

MAIN = "refs/heads/main"
REGULAR_FILE = "100644"
SYMLINK = "120000"
GITLINK = "160000"

_AUTHORED_BY_THE_SCENARIO = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "scenario",
    "GIT_AUTHOR_EMAIL": "scenario@invalid",
    "GIT_COMMITTER_NAME": "scenario",
    "GIT_COMMITTER_EMAIL": "scenario@invalid",
}
"""Who commits, so a scenario never inherits the machine's own git identity."""


@dataclass(frozen=True)
class TreeEntry:
    """One entry of a scenario's tree, as git's index describes it."""

    mode: str
    content: str


def regular(content: str) -> TreeEntry:
    return TreeEntry(REGULAR_FILE, content)


class GitRepository:
    """A repository whose trees are written straight through git plumbing.

    Bare or with a working tree, because the two answer differently for what a
    location *is*: a bare repository answers for itself, a checkout answers
    with its top level, and a worktree linked to that checkout keeps its
    administrative files somewhere else entirely.
    """

    def __init__(self, path: Path, *, bare: bool = True) -> None:
        self.path = path
        self._git_directory = path if bare else path / ".git"
        path.mkdir(parents=True, exist_ok=True)
        self._git(
            "init",
            *(("--bare",) if bare else ()),
            "--quiet",
            "--initial-branch=main",
            ".",
        )

    @property
    def location(self) -> str:
        return str(self.path)

    def commit(self, entries: Mapping[str, TreeEntry], ref: str = MAIN) -> str:
        self._index.unlink(missing_ok=True)
        described = "".join(
            f"{entry.mode} {self._object(path, entry)}\t{path}\n"
            for path, entry in entries.items()
        )
        self._git("update-index", "--index-info", stdin=described)
        tree = self._git("write-tree")
        commit = self._git("commit-tree", tree, "-m", "scenario")
        self._git("update-ref", ref, commit)
        return commit

    def linked_worktree(self, path: Path, ref: str = MAIN) -> Path:
        """A second working tree of this repository, checked out elsewhere."""

        self._git("worktree", "add", "--quiet", "--detach", str(path), ref)
        return path

    @property
    def _index(self) -> Path:
        return self._git_directory / "scenario.index"

    def _object(self, path: str, entry: TreeEntry) -> str:
        if entry.mode == GITLINK:
            return entry.content
        return self._git("hash-object", "-w", "--stdin", stdin=entry.content)

    def _git(self, *arguments: str, stdin: str = "") -> str:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=self.path,
            env={
                **os.environ,
                **_AUTHORED_BY_THE_SCENARIO,
                "GIT_DIR": str(self._git_directory),
                "GIT_INDEX_FILE": str(self._index),
            },
            input=stdin.encode("utf-8"),
            capture_output=True,
            check=True,
        )
        return completed.stdout.decode("utf-8").strip()


def registration(
    repository: GitRepository | Path,
    *patterns: str,
    ref: str = MAIN,
) -> DefinitionSourceConfiguration:
    location = (
        repository.location
        if isinstance(repository, GitRepository)
        else str(repository)
    )
    return DefinitionSourceConfiguration(
        DefinitionSourceKind.GIT,
        RepositoryLocation(location),
        RepositoryRef(ref),
        DefinitionSourceAccess.ANONYMOUS,
        DefinitionSourceActor("felix"),
        tuple(
            DefinitionSourceSelection(SelectionPattern(pattern), RevisionKind.WORKFLOW)
            for pattern in (patterns or ("workflows/*.yaml",))
        ),
    )


def refusal_of(
    configuration: DefinitionSourceConfiguration,
) -> DefinitionSourceRefusal:
    with pytest.raises(DefinitionSourceUnreadable) as refused:
        GitDefinitionSource().scan(configuration)
    return refused.value.refusal


def test_a_scan_answers_the_commit_its_configured_ref_resolves_to(
    tmp_path: Path,
) -> None:
    repository = GitRepository(tmp_path / "definitions.git")
    commit = repository.commit({"workflows/build.yaml": regular("name: build")})

    assert GitDefinitionSource().scan(registration(repository)).commit.value == commit


def test_resolving_answers_the_commit_without_asking_what_is_selected(
    tmp_path: Path,
) -> None:
    """A source claiming nothing yet still has a location and a ref."""

    repository = GitRepository(tmp_path / "definitions.git")
    commit = repository.commit({"README.md": regular("no workflow here")})

    resolved = GitDefinitionSource().resolve(registration(repository))

    assert resolved.value == commit
    assert (
        refusal_of(registration(repository))
        == DefinitionSourceRefusal.NO_SELECTED_FILES
    )


def test_every_selected_file_arrives_with_its_exact_bytes_in_path_order(
    tmp_path: Path,
) -> None:
    repository = GitRepository(tmp_path / "definitions.git")
    repository.commit(
        {
            "workflows/zebra.yaml": regular("name: zebra"),
            "workflows/alpha.yaml": regular("name: alpha"),
            "workflows/middle.yaml": regular("name: middle"),
            "README.md": regular("not selected"),
        }
    )

    scanned = GitDefinitionSource().scan(registration(repository))

    assert tuple(
        (selected.path.value, selected.document) for selected in scanned.files
    ) == (
        ("workflows/alpha.yaml", b"name: alpha"),
        ("workflows/middle.yaml", b"name: middle"),
        ("workflows/zebra.yaml", b"name: zebra"),
    )


def test_each_selected_file_carries_the_kind_its_own_selection_configured(
    tmp_path: Path,
) -> None:
    repository = GitRepository(tmp_path / "definitions.git")
    repository.commit(
        {
            "workflows/build.yaml": regular("name: build"),
            "flows/ship.yaml": regular("name: ship"),
        }
    )

    scanned = GitDefinitionSource().scan(
        registration(repository, "workflows/*.yaml", "flows/*.yaml")
    )

    assert {
        selected.path.value: selected.selection.pattern.value
        for selected in scanned.files
    } == {
        "flows/ship.yaml": "flows/*.yaml",
        "workflows/build.yaml": "workflows/*.yaml",
    }


def test_a_moved_ref_is_a_different_commit_and_different_bytes(
    tmp_path: Path,
) -> None:
    repository = GitRepository(tmp_path / "definitions.git")
    first = repository.commit({"workflows/build.yaml": regular("name: build")})
    second = repository.commit({"workflows/build.yaml": regular("name: rebuilt")})

    scanned = GitDefinitionSource().scan(registration(repository))

    assert first != second
    assert scanned.commit.value == second
    assert scanned.files[0].document == b"name: rebuilt"


def test_a_ref_the_source_does_not_carry_refuses_by_name(tmp_path: Path) -> None:
    repository = GitRepository(tmp_path / "definitions.git")
    repository.commit({"workflows/build.yaml": regular("name: build")})

    assert (
        refusal_of(registration(repository, ref="refs/heads/absent"))
        == DefinitionSourceRefusal.REF_UNRESOLVED
    )


def test_a_location_that_is_no_repository_refuses_by_name_with_gits_reason(
    tmp_path: Path,
) -> None:
    """The word says what happened; git's own sentence says why.

    A location that is merely misspelled and one this user may not enter earn
    the same refusal word, so a refusal that dropped git's words left the
    operator with nothing to act on (#1071).
    """

    unreadable = tmp_path / "not-a-repository"
    unreadable.mkdir()

    with pytest.raises(DefinitionSourceUnreadable) as refused:
        GitDefinitionSource().scan(registration(unreadable))

    assert refused.value.refusal == DefinitionSourceRefusal.UNREACHABLE
    assert what_git_refuses_at(unreadable) in refused.value.detail


def what_git_refuses_at(directory: Path) -> str:
    """What git itself writes when asked for the repository in this directory."""

    completed = subprocess.run(
        ("git", "rev-parse", "--absolute-git-dir"),
        cwd=directory,
        env={**os.environ, **_AUTHORED_BY_THE_SCENARIO},
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0, "this scenario needs a location git refuses"
    return completed.stderr.decode("utf-8").strip()


def test_a_plain_directory_inside_a_repository_is_not_that_repository(
    tmp_path: Path,
) -> None:
    """Git walks upwards; a source configured here never named those files."""

    checkout = GitRepository(tmp_path / "checkout", bare=False)
    checkout.commit({"workflows/build.yaml": regular("name: build")})
    nested = tmp_path / "checkout" / "somewhere" / "inside"
    nested.mkdir(parents=True)

    assert refusal_of(registration(nested)) == DefinitionSourceRefusal.UNREACHABLE


def test_a_directory_inside_a_bare_repository_is_not_that_repository(
    tmp_path: Path,
) -> None:
    repository = GitRepository(tmp_path / "definitions.git")
    repository.commit({"workflows/build.yaml": regular("name: build")})

    assert (
        refusal_of(registration(Path(repository.location) / "objects"))
        == DefinitionSourceRefusal.UNREACHABLE
    )


def test_a_checkout_is_read_at_the_top_level_it_answers_with(tmp_path: Path) -> None:
    checkout = GitRepository(tmp_path / "checkout", bare=False)
    commit = checkout.commit({"workflows/build.yaml": regular("name: build")})

    assert GitDefinitionSource().scan(registration(checkout)).commit.value == commit


def test_a_linked_worktree_is_a_repository_this_source_may_be_read_at(
    tmp_path: Path,
) -> None:
    """Its git directory lives under the checkout that created it, never here."""

    checkout = GitRepository(tmp_path / "checkout", bare=False)
    commit = checkout.commit({"workflows/build.yaml": regular("name: build")})
    linked = checkout.linked_worktree(tmp_path / "linked")

    scanned = GitDefinitionSource().scan(registration(linked))

    assert scanned.commit.value == commit
    assert tuple(selected.path.value for selected in scanned.files) == (
        "workflows/build.yaml",
    )


def test_a_location_that_is_not_even_a_directory_refuses_by_name(
    tmp_path: Path,
) -> None:
    assert (
        refusal_of(registration(tmp_path / "nothing-here"))
        == DefinitionSourceRefusal.UNREACHABLE
    )


def test_a_commit_carrying_nothing_the_selections_claim_refuses_by_name(
    tmp_path: Path,
) -> None:
    repository = GitRepository(tmp_path / "definitions.git")
    repository.commit({"README.md": regular("no workflow here")})

    assert (
        refusal_of(registration(repository))
        == DefinitionSourceRefusal.NO_SELECTED_FILES
    )


def test_two_selections_claiming_one_file_refuse_the_whole_scan(
    tmp_path: Path,
) -> None:
    repository = GitRepository(tmp_path / "definitions.git")
    repository.commit({"workflows/build.yaml": regular("name: build")})

    assert (
        refusal_of(registration(repository, "workflows/*.yaml", "workflows/build*"))
        == DefinitionSourceRefusal.SELECTION_AMBIGUOUS
    )


def test_a_selected_symlink_refuses_rather_than_publishing_its_target(
    tmp_path: Path,
) -> None:
    repository = GitRepository(tmp_path / "definitions.git")
    repository.commit({"workflows/build.yaml": TreeEntry(SYMLINK, "../../etc/passwd")})

    assert (
        refusal_of(registration(repository)) == DefinitionSourceRefusal.SYMLINK_SELECTED
    )


def test_a_selected_gitlink_refuses_rather_than_publishing_a_foreign_commit(
    tmp_path: Path,
) -> None:
    repository = GitRepository(tmp_path / "definitions.git")
    borrowed = repository.commit({"README.md": regular("first")})
    repository.commit(
        {
            "README.md": regular("first"),
            "workflows/build.yaml": TreeEntry(GITLINK, borrowed),
        }
    )

    assert (
        refusal_of(registration(repository)) == DefinitionSourceRefusal.GITLINK_SELECTED
    )


def test_a_symlink_no_selection_claims_leaves_the_scan_alone(tmp_path: Path) -> None:
    repository = GitRepository(tmp_path / "definitions.git")
    repository.commit(
        {
            "workflows/build.yaml": regular("name: build"),
            "elsewhere/link": TreeEntry(SYMLINK, "../workflows/build.yaml"),
        }
    )

    scanned = GitDefinitionSource().scan(registration(repository))

    assert tuple(selected.path.value for selected in scanned.files) == (
        "workflows/build.yaml",
    )
