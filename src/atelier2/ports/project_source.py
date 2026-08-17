"""One project's own source, pinned to a commit and unpacked where the work happens.

An attempt does not work on "the project as it stands". It works on the exact tree
one commit names, resolved once when the node's durable binding is composed and
never resolved again -- so an operator may commit, rebase or check out anything
while a run is in flight without that run changing under it.

This port is how an attempt reaches that tree: pin the source, refuse a pin the
source can no longer answer for, read one declaration out of the pinned tree
without unpacking it, and unpack the tree into the directory the attempt leased.
What is unpacked is material, not a repository: no history travels with it, so
nothing in the lease can commit or fetch. That is a stated limit and not
isolation -- the lease's own sentence about that still stands.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Protocol

from atelier2.ports.agent_executions import AgentAttemptWorkspaceLease

GIT_OBJECT_NAME_LENGTHS = (40, 64)
"""How long a git object name is: a SHA-1 repository's, and a SHA-256 one's."""

_OBJECT_NAME_CHARACTERS = re.compile(r"[0-9a-f]+")


class ProjectSourceUnavailable(Exception):
    """The pinned project source cannot be answered for, so nothing is claimed."""


@dataclass(frozen=True)
class ProjectSourcePin:
    """The exact source one attempt works on: one commit, and the tree it names.

    Both travel because they answer different questions. The commit is what the
    operator recognises and what the source is asked for again; the tree is the
    identity of the material itself, so two commits carrying the same content are
    visibly the same material to whoever reads the pin later.
    """

    commit: str
    tree: str

    def __post_init__(self) -> None:
        for field_name, value in (("commit", self.commit), ("tree", self.tree)):
            if (
                type(value) is not str
                or len(value) not in GIT_OBJECT_NAME_LENGTHS
                or not _OBJECT_NAME_CHARACTERS.fullmatch(value)
            ):
                raise ValueError(
                    f"a project source {field_name} must be an object name, "
                    f"not {value!r}"
                )


class ProjectSourceRepository(Protocol):
    """The provider-neutral owner of one project's source and its pinned trees."""

    def head(self) -> ProjectSourcePin:
        """Pin the source as it stands now, so a later attempt runs on this tree."""
        ...

    def attest(self, pin: ProjectSourcePin) -> None:
        """Refuse a pin this source can no longer answer for, unpacking nothing."""
        ...

    def read(self, pin: ProjectSourcePin, path: PurePosixPath) -> bytes:
        """The bytes one file carries in the pinned tree, without unpacking it."""
        ...

    def materialize(
        self, pin: ProjectSourcePin, lease: AgentAttemptWorkspaceLease
    ) -> None:
        """Unpack the pinned tree into the directory this attempt leased."""
        ...
