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


class BareRepository:
    """A bare repository whose trees are written straight through git plumbing."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self._git("init", "--bare", "--quiet", "--initial-branch=main", ".")

    @property
    def location(self) -> str:
        return str(self.path)

    def commit(self, entries: Mapping[str, TreeEntry], ref: str = MAIN) -> str:
        index = self.path / "scenario.index"
        index.unlink(missing_ok=True)
        described = "".join(
            f"{entry.mode} {self._object(path, entry)}\t{path}\n"
            for path, entry in entries.items()
        )
        self._git("update-index", "--index-info", stdin=described)
        tree = self._git("write-tree")
        commit = self._git("commit-tree", tree, "-m", "scenario")
        self._git("update-ref", ref, commit)
        return commit

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
                "GIT_DIR": str(self.path),
                "GIT_INDEX_FILE": str(self.path / "scenario.index"),
            },
            input=stdin.encode("utf-8"),
            capture_output=True,
            check=True,
        )
        return completed.stdout.decode("utf-8").strip()


def registration(
    repository: BareRepository | Path,
    *patterns: str,
    ref: str = MAIN,
) -> DefinitionSourceConfiguration:
    location = (
        repository.location
        if isinstance(repository, BareRepository)
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
    repository = BareRepository(tmp_path / "definitions.git")
    commit = repository.commit({"workflows/build.yaml": regular("name: build")})

    assert GitDefinitionSource().scan(registration(repository)).commit.value == commit


def test_every_selected_file_arrives_with_its_exact_bytes_in_path_order(
    tmp_path: Path,
) -> None:
    repository = BareRepository(tmp_path / "definitions.git")
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
    repository = BareRepository(tmp_path / "definitions.git")
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
    repository = BareRepository(tmp_path / "definitions.git")
    first = repository.commit({"workflows/build.yaml": regular("name: build")})
    second = repository.commit({"workflows/build.yaml": regular("name: rebuilt")})

    scanned = GitDefinitionSource().scan(registration(repository))

    assert first != second
    assert scanned.commit.value == second
    assert scanned.files[0].document == b"name: rebuilt"


def test_a_ref_the_source_does_not_carry_refuses_by_name(tmp_path: Path) -> None:
    repository = BareRepository(tmp_path / "definitions.git")
    repository.commit({"workflows/build.yaml": regular("name: build")})

    assert (
        refusal_of(registration(repository, ref="refs/heads/absent"))
        == DefinitionSourceRefusal.REF_UNRESOLVED
    )


def test_a_location_that_is_no_repository_refuses_by_name(tmp_path: Path) -> None:
    (tmp_path / "not-a-repository").mkdir()

    assert (
        refusal_of(registration(tmp_path / "not-a-repository"))
        == DefinitionSourceRefusal.UNREACHABLE
    )


def test_a_plain_directory_inside_a_repository_is_not_that_repository(
    tmp_path: Path,
) -> None:
    """Git walks upwards; a source configured here never named those files."""

    enclosing = tmp_path / "checkout"
    enclosing.mkdir()
    BareRepository(enclosing / ".git").commit(
        {"workflows/build.yaml": regular("name: build")}
    )
    nested = enclosing / "somewhere" / "inside"
    nested.mkdir(parents=True)

    assert refusal_of(registration(nested)) == DefinitionSourceRefusal.UNREACHABLE


def test_a_working_tree_is_read_through_the_repository_it_owns(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    commit = BareRepository(checkout / ".git").commit(
        {"workflows/build.yaml": regular("name: build")}
    )

    assert GitDefinitionSource().scan(registration(checkout)).commit.value == commit


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
    repository = BareRepository(tmp_path / "definitions.git")
    repository.commit({"README.md": regular("no workflow here")})

    assert (
        refusal_of(registration(repository))
        == DefinitionSourceRefusal.NO_SELECTED_FILES
    )


def test_two_selections_claiming_one_file_refuse_the_whole_scan(
    tmp_path: Path,
) -> None:
    repository = BareRepository(tmp_path / "definitions.git")
    repository.commit({"workflows/build.yaml": regular("name: build")})

    assert (
        refusal_of(registration(repository, "workflows/*.yaml", "workflows/build*"))
        == DefinitionSourceRefusal.SELECTION_AMBIGUOUS
    )


def test_a_selected_symlink_refuses_rather_than_publishing_its_target(
    tmp_path: Path,
) -> None:
    repository = BareRepository(tmp_path / "definitions.git")
    repository.commit({"workflows/build.yaml": TreeEntry(SYMLINK, "../../etc/passwd")})

    assert (
        refusal_of(registration(repository)) == DefinitionSourceRefusal.SYMLINK_SELECTED
    )


def test_a_selected_gitlink_refuses_rather_than_publishing_a_foreign_commit(
    tmp_path: Path,
) -> None:
    repository = BareRepository(tmp_path / "definitions.git")
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
    repository = BareRepository(tmp_path / "definitions.git")
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
