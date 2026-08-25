"""The pin a node's binding took and the candidate its attempt made.

A pin is written into that binding and read back on every recovery, so what a
pin *is* has to be decidable with no repository reachable and no adapter loaded.
`ports/project_source.py` speaks it and resolves it; nothing there decides it.
The candidate is the same kind of value on the way back: what an attempt made,
named by the object it now is rather than by the directory it was made in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from atelier2.contracts.agent_attempts import AgentAttemptId

_OBJECT_NAME_CHARACTERS = re.compile(r"[0-9a-f]+")


class GitObjectFormat(StrEnum):
    """How a repository names its objects, in git's own words.

    Two repositories of different formats cannot hold each other's objects: the
    name of a tree *is* its hash. So this is not decoration -- it decides whether
    "keep this project's work over there" is possible at all.
    """

    SHA1 = "sha1"
    SHA256 = "sha256"


_OBJECT_NAME_LENGTH = {GitObjectFormat.SHA1: 40, GitObjectFormat.SHA256: 64}
"""How long a name is in each format, in hex characters.

Beside the formats rather than apart from them, because they are one fact: a
format this product cannot name is a name length it must not accept either.
"""


def _refuse_unless_object_name(what: str, value: str) -> None:
    if (
        type(value) is not str
        or len(value) not in _OBJECT_NAME_LENGTH.values()
        or not _OBJECT_NAME_CHARACTERS.fullmatch(value)
    ):
        raise ValueError(f"a {what} must be an object name, not {value!r}")


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
            _refuse_unless_object_name(f"project source {field_name}", value)


@dataclass(frozen=True)
class CandidateTree:
    """One attempt's finished work, as the immutable object it was kept as.

    It names the attempt and the tree, and nothing more, because those are the
    two facts the store itself can answer for after any restart. The commit the
    work stands on is already held by the node's own binding; carrying a second
    copy here would be a copy that can disagree with the first.
    """

    attempt_id: AgentAttemptId
    tree: str

    def __post_init__(self) -> None:
        _refuse_unless_object_name("candidate tree", self.tree)
