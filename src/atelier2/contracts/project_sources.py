"""The pin a node's binding took, owned where a durable value belongs.

A pin is written into that binding and read back on every recovery, so what a
pin *is* has to be decidable with no repository reachable and no adapter loaded.
`ports/project_source.py` speaks it and resolves it; nothing there decides it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GIT_OBJECT_NAME_LENGTHS = (40, 64)
"""How long a git object name is: a SHA-1 repository's, and a SHA-256 one's."""

_OBJECT_NAME_CHARACTERS = re.compile(r"[0-9a-f]+")


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
