"""The host's one live-versioned configuration channel.

The first entry is `project id → root path`. The second is recommended occupancy
per workflow lineage. The third is the project's source connection: which
external platform holds the project's tracked source, by an address only the
connected platform adapter interprets, and where the host resolves its
credential from — always a directory reference, never a credential value
(ADR 0010 decision 2, ADR 0009 §6). CLI flags name where this channel lives;
they are not a second copy of the map.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from atelier2.contracts.agents import (
    MAXIMUM_SIGNED_INT64,
    AgentConfigurationRevisionHash,
    AgentRole,
)
from atelier2.contracts.catalog_v3 import CatalogLineageId
from atelier2.contracts.hashing import Sha256Hash, frame

MAXIMUM_PROJECT_ID_CHARACTERS = 1_024
# This serve slice opens no more than the single project id bound at composition.
# The collection, application decision, and OpenAPI all answer to this owner.
MAXIMUM_SERVED_PROJECTS = 1
# Linux PATH_MAX is 4096 including the terminating NUL; a 4096-character
# path is not openable. The published CHECK must match what open will admit.
MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS = 4_095
# Occupancy is one recommended role per binding. This family owns the count;
# the run-start wire bound is a different question and must not cap a stored
# occupancy after the fact.
MAXIMUM_OCCUPANCY_BINDINGS = 100
# A source kind names one platform adapter family; it is a short word, not an
# address.
MAXIMUM_SOURCE_KIND_CHARACTERS = 64
# The source address is opaque here, so its bound mirrors the tracker item
# reference's: room for any platform's own identifier, no room for a document.
MAXIMUM_SOURCE_ADDRESS_CHARACTERS = 1_024
MAXIMUM_CONNECTION_ACTOR_CHARACTERS = 1_024
# The credential reference is a filesystem directory, so the project-root
# bound's PATH_MAX rationale is this bound too.
MAXIMUM_CREDENTIAL_DIRECTORY_CHARACTERS = MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS

PROJECT_UNKNOWN = "project-unknown"
PROJECT_ROOT_MISSING = "project-root-missing"
HOST_CONFIGURATION_UNREADABLE = "host-configuration-unreadable"
OCCUPANCY_REVISION_CONFLICT = "occupancy-revision-conflict"
# ADR 0010's refusal for an operation or observation naming a project with no
# connection record.
PLATFORM_CONNECTION_UNKNOWN = "platform-connection-unknown"


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


class ProjectRootBytesDisagree(Exception):
    """Stored project-root fields do not hash to the revision hash they carry."""


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
        try:
            encoded = self.value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ProjectUnknown(
                f"{PROJECT_UNKNOWN}: a project id must be exact UTF-8 Unicode scalar text"
            ) from error
        if encoded.decode("utf-8") != self.value:
            raise ProjectUnknown(
                f"{PROJECT_UNKNOWN}: a project id must round-trip exact UTF-8"
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


class OccupancyBytesDisagree(Exception):
    """Stored occupancy fields do not hash to the revision hash they carry."""


class OccupancyRevisionHashCollision(Exception):
    """The same occupancy revision hash already names different fields."""


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
        if len(ordered) > MAXIMUM_OCCUPANCY_BINDINGS:
            raise ValueError(
                "occupancy bindings must contain at most "
                f"{MAXIMUM_OCCUPANCY_BINDINGS} roles"
            )
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


class ProjectSourceConnectionConflict(Exception):
    """The same project, source kind, and revision number already hold
    different bytes."""


class ProjectSourceConnectionBytesDisagree(Exception):
    """Stored connection fields do not hash to the revision hash they carry."""


class ProjectSourceConnectionHashCollision(Exception):
    """The same connection revision hash already names different fields."""


class HostProjectSourceConnectionRevisionHash(Sha256Hash):
    """Identity of one immutable project-source connection revision."""


def _bounded_text(value: object, maximum: int, what: str) -> str:
    if type(value) is not str or not 1 <= len(value) <= maximum:
        raise ValueError(f"{what} must contain 1..{maximum} exact characters")
    return value


@dataclass(frozen=True)
class SourceKind:
    """Which platform adapter family interprets a connection's source address.

    The value — like the address it qualifies — is the adapter's contract
    (ADR 0010 decision 1): no platform name is fixed here.
    """

    value: str

    def __post_init__(self) -> None:
        _bounded_text(self.value, MAXIMUM_SOURCE_KIND_CHARACTERS, "a source kind")


@dataclass(frozen=True)
class SourceAddress:
    """The opaque address of one project's source inside its platform.

    What the string means — a repository, a group path — is the connected
    platform adapter's contract, never reinterpreted here (the
    `TrackerItemReference` rule, applied to the source itself).
    """

    value: str

    def __post_init__(self) -> None:
        _bounded_text(self.value, MAXIMUM_SOURCE_ADDRESS_CHARACTERS, "a source address")


@dataclass(frozen=True)
class ConnectionActor:
    """The operator accountable for one connect act (ADR 0010 decision 2)."""

    value: str

    def __post_init__(self) -> None:
        _bounded_text(
            self.value, MAXIMUM_CONNECTION_ACTOR_CHARACTERS, "a connection actor"
        )


class SourceConnectionAuthMethod(StrEnum):
    """The credential method the operator chose at connect time.

    ADR 0010 decision 2 specifies two methods; the App method joins here when
    its composition is built — deferred by naming, not by a placeholder field.
    """

    PERSONAL_ACCESS_TOKEN = "personal-access-token"


@dataclass(frozen=True)
class ProjectSourceConnectionRevision:
    """One immutable connect act: identities and a credential reference only.

    The credential directory is where the host resolves the secret at
    composition (ADR 0009 §6); the secret's value never enters this record,
    the channel, or any projection of either.
    """

    project_id: ProjectId
    revision_number: int
    source_kind: SourceKind
    source_address: SourceAddress
    credential_directory: Path
    auth_method: SourceConnectionAuthMethod
    connected_by: ConnectionActor
    revision_hash: HostProjectSourceConnectionRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, ProjectId):
            raise TypeError("project id must use its typed contract")
        if (
            type(self.revision_number) is not int
            or not 1 <= self.revision_number <= MAXIMUM_SIGNED_INT64
        ):
            raise ValueError(
                "project-source connection revision number must be "
                "a positive signed int64"
            )
        if not isinstance(self.source_kind, SourceKind):
            raise TypeError("source kind must use its typed contract")
        if not isinstance(self.source_address, SourceAddress):
            raise TypeError("source address must use its typed contract")
        if not isinstance(self.credential_directory, Path):
            raise TypeError("credential directory must be a path")
        if not isinstance(self.auth_method, SourceConnectionAuthMethod):
            raise TypeError("auth method must use its typed contract")
        if not isinstance(self.connected_by, ConnectionActor):
            raise TypeError("connection actor must use its typed contract")
        stored = str(self.credential_directory.expanduser().resolve())
        if not 1 <= len(stored) <= MAXIMUM_CREDENTIAL_DIRECTORY_CHARACTERS:
            raise ValueError(
                "credential directory must contain "
                f"1..{MAXIMUM_CREDENTIAL_DIRECTORY_CHARACTERS} exact characters"
            )
        object.__setattr__(self, "credential_directory", Path(stored))
        object.__setattr__(
            self,
            "revision_hash",
            HostProjectSourceConnectionRevisionHash.of(
                frame(
                    "host-project-source-connection-revision/v1",
                    self.project_id.value.encode("utf-8"),
                    struct.pack(">Q", self.revision_number),
                    self.source_kind.value.encode("utf-8"),
                    self.source_address.value.encode("utf-8"),
                    stored.encode("utf-8"),
                    self.auth_method.value.encode("ascii"),
                    self.connected_by.value.encode("utf-8"),
                )
            ),
        )
