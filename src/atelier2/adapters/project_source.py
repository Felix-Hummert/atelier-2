"""The project's own git repository, and the one boundary every git call crosses.

The pin is what makes a run repeatable: a commit is resolved once, and every later
question -- does this pin still resolve, what does its manifest declare, what does
the attempt work in -- is asked of that commit rather than of whatever the
operator's checkout happens to hold at the time.

What is unpacked into a lease is the tree alone. No `.git` travels with it, so the
directory is material rather than a repository, and a provider that wants history
has none. Nothing here is isolation: the unpacking runs as this process's own user,
into the blank directory the attempt leased, and that directory's own sentence
about not being a sandbox is left standing.

The tree is entered through the identity the lease attested rather than through the
path it was named by, because unpacking happens after the lease is taken and before
the provider starts -- exactly the window in which a peer of this user could move
its own directory into that path.
"""

from __future__ import annotations

import os
import subprocess
import tarfile
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath

from atelier2.adapters.leased_directory import entered_leased_directory
from atelier2.contracts.project_sources import ProjectSourcePin
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease
from atelier2.ports.project_source import ProjectSourceUnavailable

_GIT_EXECUTABLE_NAME = "git"
"""Resolved from the serving process's own path, like every command a project names."""

_HEAD_REVISION = "HEAD"
"""What git calls the commit a checkout currently stands on."""

_STAGED_ARCHIVE_NAME = "project-source.tar"

_CONFIGURATION_FREE_GIT = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}
"""What keeps the host out of every answer this boundary gives.

The machine's own git configuration is not part of any project: it can declare a
`clean` filter, a git-lfs smudge, an author identity or a credential helper, and
each of those would change what this boundary reads or writes for reasons no run
ever declared. With no system and no global configuration left to read, only the
repository being spoken to has a say, and a call that would have prompted a human
for a credential fails instead of hanging on a terminal nobody is watching.
"""


class GitRefused(Exception):
    """A git call did not answer, so its caller says what that means for it.

    Deliberately not a port failure: the same call is a source being unavailable
    to one owner and a candidate not being captured to another, and only the
    caller knows which sentence its own users were promised.
    """


def isolated_git_environment(**declared: str) -> dict[str, str]:
    """The environment a git call gets: this process's, minus the host's opinions."""

    return {**os.environ, **_CONFIGURATION_FREE_GIT, **declared}


def answered_git(
    arguments: Sequence[str],
    *,
    working_directory: str,
    environment: Mapping[str, str],
    passed_descriptors: tuple[int, ...] = (),
) -> bytes:
    """Run one git command where it was told to, and answer with what it wrote.

    The working directory is entered by the child rather than named to git with
    `-C`, so a caller holding an open descriptor can pass `/proc/self/fd/<n>`
    together with that descriptor and have the child land in the very directory
    its owner checked.
    """

    try:
        completed = subprocess.run(
            (_GIT_EXECUTABLE_NAME, *arguments),
            cwd=working_directory,
            env=dict(environment),
            pass_fds=passed_descriptors,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise GitRefused(
            f"git {' '.join(arguments)} could not be started in "
            f"{working_directory}: {error}"
        ) from error
    if completed.returncode != 0:
        # The whole argument list is named, not just the subcommand: what a
        # refusal is about is the revision or path that was asked for.
        raise GitRefused(
            f"git {' '.join(arguments)} answered {completed.returncode} in "
            f"{working_directory}: "
            f"{completed.stderr.decode('utf-8', 'replace').strip()}"
        )
    return completed.stdout


def _tree_of(revision: str) -> str:
    return f"{revision}^{{tree}}"


class LocalGitProjectSource:
    """One local git repository as the source of every tree an attempt works in."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = project_root.resolve()

    def head(self) -> ProjectSourcePin:
        """Pin what this repository stands on, refusing a root that is not one.

        A root below its repository's top level is refused rather than pinned:
        git answers for the whole repository, so a tree pinned there would carry
        everything beside the project too, and the manifest read at that commit
        would be the enclosing repository's rather than this project's.
        """

        top_level = Path(self._line(("rev-parse", "--show-toplevel")))
        if top_level != self._project_root:
            raise ProjectSourceUnavailable(
                f"the project source {self._project_root} is not the top level of "
                f"the repository it lies in ({top_level}), so a tree pinned here "
                "would carry that whole repository rather than this project"
            )
        commit = self._object_name(_HEAD_REVISION)
        tree = self._object_name(_tree_of(commit))
        try:
            return ProjectSourcePin(commit, tree)
        except ValueError as error:
            raise ProjectSourceUnavailable(
                f"the source at {self._project_root} answered {commit!r} and "
                f"{tree!r} for what it stands on, which name no commit and tree"
            ) from error

    def attest(self, pin: ProjectSourcePin) -> None:
        standing = self._object_name(_tree_of(pin.commit))
        if standing != pin.tree:
            raise ProjectSourceUnavailable(
                f"commit {pin.commit} in {self._project_root} names the tree "
                f"{standing}, not the tree {pin.tree} this attempt was pinned to"
            )

    def read(self, pin: ProjectSourcePin, path: PurePosixPath) -> bytes:
        return self._answered(("show", f"{pin.commit}:{path}"))

    def materialize(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> None:
        with tempfile.TemporaryDirectory() as staging:
            archive = Path(staging) / _STAGED_ARCHIVE_NAME
            self._answered(
                ("archive", "--format=tar", f"--output={archive}", pin.commit)
            )
            with entered_leased_directory(
                lease.working_directory, lease.device, lease.inode
            ) as (entered, _descriptor):
                self._unpack(archive, entered, pin, lease)

    def _unpack(
        self,
        archive: Path,
        entered: str,
        pin: ProjectSourcePin,
        lease: AgentAttemptWorkspaceLease,
    ) -> None:
        try:
            with tarfile.open(archive) as unpacked:
                unpacked.extractall(entered, filter="data")
        except (OSError, tarfile.TarError) as error:
            raise ProjectSourceUnavailable(
                f"the tree of commit {pin.commit} from {self._project_root} could "
                f"not be unpacked into the workspace of attempt "
                f"{lease.attempt_id.value}: {error}"
            ) from error

    def _object_name(self, revision: str) -> str:
        return self._line(("rev-parse", "--verify", revision))

    def _line(self, arguments: tuple[str, ...]) -> str:
        return self._answered(arguments).decode("utf-8", "replace").strip()

    def _answered(self, arguments: tuple[str, ...]) -> bytes:
        try:
            return answered_git(
                arguments,
                working_directory=str(self._project_root),
                environment=isolated_git_environment(),
            )
        except GitRefused as error:
            raise ProjectSourceUnavailable(
                f"the project source at {self._project_root} could not be read: {error}"
            ) from error
