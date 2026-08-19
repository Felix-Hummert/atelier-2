"""The host's one live-versioned configuration channel.

The first entry is `project id → root path`. The second is recommended occupancy
per workflow lineage. CLI flags name where this channel lives; they are not a
second copy of the map.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

from atelier2.contracts.agents import (
    MAXIMUM_SIGNED_INT64,
    AgentConfigurationRevisionHash,
    AgentRole,
)
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.hashing import Sha256Hash, frame

MAXIMUM_PROJECT_ID_CHARACTERS = 1_024
# Linux PATH_MAX is 4096 including the terminating NUL; a 4096-character
# path is not openable. The published CHECK must match what open will admit.
MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS = 4_095

PROJECT_UNKNOWN = "project-unknown"
PROJECT_ROOT_MISSING = "project-root-missing"
HOST_CONFIGURATION_UNREADABLE = "host-configuration-unreadable"
OCCUPANCY_REVISION_CONFLICT = "occupancy-revision-conflict"


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


class OccupancyRevisionConflict(Exception):
    """The same project, lineage, and revision number already hold different bytes."""


class HostOccupancyRevisionHash(Sha256Hash):
    """Identity of one immutable occupancy mapping revision."""


@dataclass(frozen=True)
class OccupancyBinding:
    """One recommended role occupation on a lineage."""

    role: AgentRole
    agent_configuration_revision_hash: AgentConfigurationRevisionHash

    def __post_init__(self) -> None:
        if not isinstance(self.role, AgentRole):
            raise TypeError("occupancy binding role must use its typed contract")
        if not isinstance(
            self.agent_configuration_revision_hash, AgentConfigurationRevisionHash
        ):
            raise TypeError(
                "occupancy binding configuration hash must use its typed contract"
            )


@dataclass(frozen=True)
class OccupancyRevision:
    project_id: ProjectId
    lineage_id: CatalogLineageId
    revision_number: int
    bindings: tuple[OccupancyBinding, ...]
    revision_hash: HostOccupancyRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, ProjectId):
            raise TypeError("project id must use its typed contract")
        if not isinstance(self.lineage_id, CatalogLineageId):
            raise TypeError("lineage id must use its typed contract")
        if (
            type(self.revision_number) is not int
            or not 1 <= self.revision_number <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError(
                "host occupancy revision number must be a positive signed int64"
            )
        if not isinstance(self.bindings, tuple) or any(
            not isinstance(binding, OccupancyBinding) for binding in self.bindings
        ):
            raise TypeError("occupancy bindings must be occupancy binding records")
        ordered = tuple(
            sorted(
                self.bindings, key=lambda binding: binding.role.value.encode("utf-8")
            )
        )
        if len({binding.role for binding in ordered}) != len(ordered):
            raise ValueError("occupancy binding roles must be unique")
        object.__setattr__(self, "bindings", ordered)
        object.__setattr__(
            self,
            "revision_hash",
            HostOccupancyRevisionHash.of(
                frame(
                    "host-occupancy-revision/v1",
                    self.project_id.value.encode("utf-8"),
                    self.lineage_id.value.encode("ascii"),
                    struct.pack(">Q", self.revision_number),
                    *(
                        frame(
                            "host-occupancy-binding/v1",
                            binding.role.value.encode("utf-8"),
                            binding.agent_configuration_revision_hash.value.encode(
                                "ascii"
                            ),
                        )
                        for binding in ordered
                    ),
                )
            ),
        )
