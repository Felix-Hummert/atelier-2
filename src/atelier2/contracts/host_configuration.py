"""The host's one live-versioned configuration channel.

The first entry is `project id → root path`. CLI flags name where this channel
lives; they are not a second copy of the map.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.contracts.hashing import Sha256Hash, frame

MAXIMUM_PROJECT_ID_CHARACTERS = 1_024
# Linux PATH_MAX is 4096 including the terminating NUL; a 4096-character
# path is not openable. The published CHECK must match what open will admit.
MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS = 4_095

PROJECT_UNKNOWN = "project-unknown"
PROJECT_ROOT_MISSING = "project-root-missing"
HOST_CONFIGURATION_UNREADABLE = "host-configuration-unreadable"


class ProjectUnknown(Exception):
    """The id is malformed, or names a project with no configured root.

    The second case is ADR 0011's service refusal. The channel's missing row
    remains `ProjectRootMissing`.
    """


class ProjectRootMissing(Exception):
    """The channel has no root-path row for this project id."""


class HostConfigurationUnreadable(Exception):
    """The configuration channel could not be read."""


class ProjectRootRevisionConflict(Exception):
    """The same project id and revision number already hold different bytes."""


@dataclass(frozen=True)
class ProjectId:
    value: str

    def __post_init__(self) -> None:
        if (
            type(self.value) is not str
            or not 1 <= len(self.value) <= MAXIMUM_PROJECT_ID_CHARACTERS
        ):
            raise ProjectUnknown(
                f"{PROJECT_UNKNOWN}: a project id must contain "
                f"1..{MAXIMUM_PROJECT_ID_CHARACTERS} exact characters"
            )


class HostProjectRootRevisionHash(Sha256Hash):
    """Identity of one immutable project-root mapping revision."""


@dataclass(frozen=True)
class ProjectRootRevision:
    project_id: ProjectId
    revision_number: int
    root_path: Path
    revision_hash: HostProjectRootRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, ProjectId):
            raise TypeError("project id must use its typed contract")
        if (
            type(self.revision_number) is not int
            or not 1 <= self.revision_number <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError(
                "host project-root revision number must be a positive signed int64"
            )
        if not isinstance(self.root_path, Path):
            raise TypeError("project root path must be a path")
        stored = str(self.root_path.expanduser().resolve())
        if not 1 <= len(stored) <= MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS:
            raise ValueError(
                "project root path must contain "
                f"1..{MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS} exact characters"
            )
        object.__setattr__(self, "root_path", Path(stored))
        object.__setattr__(
            self,
            "revision_hash",
            HostProjectRootRevisionHash.of(
                frame(
                    "host-project-root-revision/v1",
                    self.project_id.value.encode("utf-8"),
                    struct.pack(">Q", self.revision_number),
                    stored.encode("utf-8"),
                )
            ),
        )
