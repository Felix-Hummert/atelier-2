from __future__ import annotations

import os
import stat
from pathlib import Path

from atelier2.contracts.agent_attempts import (
    TERMINAL_AGENT_ATTEMPT_STATES,
    AgentAttemptId,
)
from atelier2.ports.agent_attempts import AgentAttemptReader
from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease

SCRATCH_ROOT_MODE = 0o700
"""The only mode a scratch root may carry: no group, no world, no sharing."""

_GIT_ENTRY = ".git"
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class AgentScratchRootRefused(ValueError):
    """The declared scratch root cannot hold attempt workspaces."""


class AgentAttemptWorkspaceRefused(RuntimeError):
    """This attempt cannot be given the blank workspace it requires."""


def _refuse_unusable_path(scratch_root: Path) -> None:
    """Read the named path before it is opened, and refuse what it stands in.

    A symbolic link anywhere above the root would let whoever controls it move
    every attempt's directory somewhere else between two calls, and a root
    inside a git worktree would put provider scratch files into a checkout the
    operator is working in. Both are refused by name, because whoever first
    points `--agent-scratch-root` at a repository path deserves to read why.
    """

    if not scratch_root.is_absolute():
        raise AgentScratchRootRefused(
            f"the agent scratch root must be an absolute path, not {scratch_root}"
        )
    for component in (scratch_root, *scratch_root.parents):
        if component.is_symlink():
            raise AgentScratchRootRefused(
                f"the agent scratch root must contain no symbolic link, but "
                f"{component} is one: a link above the root can move every "
                "attempt's directory between two calls"
            )
        git_entry = component / _GIT_ENTRY
        if git_entry.exists() or git_entry.is_symlink():
            raise AgentScratchRootRefused(
                f"the agent scratch root must lie outside every git worktree, but "
                f"{git_entry} exists: a provider writes into its workspace, so a "
                "root inside a checkout would write into that checkout"
            )


def _open_scratch_root(scratch_root: Path) -> int:
    _refuse_unusable_path(scratch_root)
    try:
        return os.open(scratch_root, _DIRECTORY_FLAGS)
    except OSError as error:
        raise AgentScratchRootRefused(
            f"the agent scratch root must be an existing directory the serving "
            f"user owns with mode {SCRATCH_ROOT_MODE:o}, but {scratch_root} "
            f"could not be opened: {error}"
        ) from error


def _attempt_name(entry_name: str) -> AgentAttemptId | None:
    try:
        return AgentAttemptId(entry_name)
    except ValueError:
        return None


def _remove_workspace_tree(parent_fd: int, name: str) -> None:
    """Remove one directory and its contents through descriptors alone.

    Every step is relative to a descriptor the caller already holds and no
    symbolic link is ever followed, so nothing outside this directory can be
    reached by replacing an entry inside it. A link the provider left behind is
    unlinked; whatever it points at is not touched.
    """

    try:
        directory_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        return
    try:
        for entry in os.scandir(directory_fd):
            if entry.is_dir(follow_symlinks=False):
                _remove_workspace_tree(directory_fd, entry.name)
            else:
                os.unlink(entry.name, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    os.rmdir(name, dir_fd=parent_fd)


class LocalAgentAttemptWorkspaceOwner:
    """The one owner of every attempt's scratch directory on this host.

    It binds the declared scratch root to a held directory descriptor, so every
    creation and removal happens inside the exact directory that was attested,
    not inside whatever the same path names later. The root itself is never
    removed and never removed recursively: only one attempt's own directory is.

    It proves no operating-system isolation. A provider still runs as the
    serving user and can name any path it likes; what this owner guarantees is
    that the directory it is *started in* is blank, its own, and gone again.
    """

    def __init__(self, scratch_root: Path) -> None:
        # Absolute but deliberately unresolved: resolving would erase the very
        # symbolic links the attestation below refuses, and this exact path is
        # both what is attested and what is opened.
        self._scratch_root = Path(os.path.abspath(scratch_root))
        self._root_fd = _open_scratch_root(self._scratch_root)
        self.preflight()

    @property
    def scratch_root(self) -> Path:
        return self._scratch_root

    def preflight(self) -> None:
        status = os.fstat(self._root_fd)
        if status.st_uid != os.getuid():
            raise AgentScratchRootRefused(
                f"the agent scratch root {self._scratch_root} belongs to another "
                "user, so this server cannot own the workspaces inside it"
            )
        if stat.S_IMODE(status.st_mode) != SCRATCH_ROOT_MODE:
            raise AgentScratchRootRefused(
                f"the agent scratch root {self._scratch_root} must carry mode "
                f"{SCRATCH_ROOT_MODE:o}, not {stat.S_IMODE(status.st_mode):o}: "
                "group or world access would share every provider's scratch files"
            )
        for entry in os.scandir(self._root_fd):
            if _attempt_name(entry.name) is None or not entry.is_dir(
                follow_symlinks=False
            ):
                raise AgentScratchRootRefused(
                    f"the agent scratch root {self._scratch_root} holds {entry.name}, "
                    "which is no attempt workspace: this root is owned entirely by "
                    "attempt directories, so nothing else may be mistaken for one"
                )

    def acquire(self, attempt_id: AgentAttemptId) -> AgentAttemptWorkspaceLease:
        self.preflight()
        name = attempt_id.value
        try:
            os.mkdir(name, SCRATCH_ROOT_MODE, dir_fd=self._root_fd)
        except FileExistsError as error:
            raise AgentAttemptWorkspaceRefused(
                f"the workspace of attempt {name} already exists under "
                f"{self._scratch_root}: an attempt is started in a directory it "
                "created itself or in none at all"
            ) from error
        working_directory = self._scratch_root / name
        created = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        try:
            named = os.stat(working_directory, follow_symlinks=False)
            same_directory = (named.st_dev, named.st_ino) == (
                created.st_dev,
                created.st_ino,
            )
        except OSError:
            same_directory = False
        if not same_directory:
            _remove_workspace_tree(self._root_fd, name)
            raise AgentScratchRootRefused(
                f"the agent scratch root {self._scratch_root} no longer names the "
                "directory this server bound, so the path handed to a provider "
                "would not be the workspace that was created"
            )
        return AgentAttemptWorkspaceLease(attempt_id, working_directory)

    def release(self, attempt_id: AgentAttemptId) -> None:
        _remove_workspace_tree(self._root_fd, attempt_id.value)

    def reconcile(self, attempts: AgentAttemptReader) -> None:
        """Remove what terminal attempts left behind, and nothing else.

        Every directory is read back against the durable attempt it is named
        after. A terminal attempt's workspace is removed, a nonterminal one's
        is left exactly as it stands for the recovery that owns it, and a name
        with no durable attempt behind it refuses the whole reconciliation
        rather than being deleted on the assumption that it is rubbish.
        """

        self.preflight()
        for name in sorted(os.listdir(self._root_fd)):
            attempt_id = AgentAttemptId(name)
            if attempts.load(attempt_id).state in TERMINAL_AGENT_ATTEMPT_STATES:
                _remove_workspace_tree(self._root_fd, name)

    def close(self) -> None:
        os.close(self._root_fd)
