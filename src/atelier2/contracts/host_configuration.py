"""The host's one live-versioned configuration channel.

The first entry is `project id → root path`. CLI flags name where this channel
lives; they are not a second copy of the map. The mapping cannot live in a
project store: it is what tells the service which store to open.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from atelier2.contracts.agents import MAXIMUM_SIGNED_INT64
from atelier2.contracts.hashing import Sha256Hash

MAXIMUM_PROJECT_ID_CHARACTERS = 1_024
MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS = 4_096
_REVISION_NUMBER_DIGITS = len(str(MAXIMUM_SIGNED_INT64))
_JSON_WRAPPER_BYTES = len('{"project_id":"","revision_number":,"root_path":""}')
MAXIMUM_HOST_PROJECT_ROOT_DOCUMENT_BYTES = (
    MAXIMUM_PROJECT_ID_CHARACTERS
    + MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS
    + _REVISION_NUMBER_DIGITS
    + _JSON_WRAPPER_BYTES
)

PROJECT_ID_FIELD = "project_id"
REVISION_NUMBER_FIELD = "revision_number"
ROOT_PATH_FIELD = "root_path"
_KNOWN_FIELDS = frozenset({PROJECT_ID_FIELD, REVISION_NUMBER_FIELD, ROOT_PATH_FIELD})

PROJECT_UNKNOWN = "project-unknown"
HOST_CONFIGURATION_UNREADABLE = "host-configuration-unreadable"
HOST_CONFIGURATION_CONFLICT = "host-configuration-conflict"
HOST_CONFIGURATION_COLLISION = "host-configuration-collision"

_BYTE_ORDER_MARK = "\ufeff"


class HostConfigurationRefusal(StrEnum):
    """Why published bytes are not a project-root mapping, as a stable token."""

    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_NOT_UTF8 = "document_not_utf8"
    NOT_A_PROJECT_ROOT_OBJECT = "not_a_project_root_object"
    UNKNOWN_FIELD = "unknown_field"
    RELATIVE_ROOT_PATH = "relative_root_path"


class HostConfigurationUnreadable(ValueError):
    """The configuration channel could not be read."""

    def __init__(self, detail: str = "") -> None:
        message = (
            HOST_CONFIGURATION_UNREADABLE
            if not detail
            else f"{HOST_CONFIGURATION_UNREADABLE}: {detail}"
        )
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class ProjectId:
    """One minted project identity, as the host configuration names it."""

    value: str

    def __post_init__(self) -> None:
        if (
            type(self.value) is not str
            or not 1 <= len(self.value) <= MAXIMUM_PROJECT_ID_CHARACTERS
            or "\x00" in self.value
        ):
            raise ValueError(
                "a project id must contain "
                f"1..{MAXIMUM_PROJECT_ID_CHARACTERS} exact characters"
            )


class HostConfigurationRevisionHash(Sha256Hash):
    """Identity of one immutable host-configuration document."""


@dataclass(frozen=True, slots=True)
class HostProjectRootAccepted:
    """These bytes map this project id to this root path at this revision."""

    project_id: ProjectId
    revision_number: int
    root_path: Path


@dataclass(frozen=True, slots=True)
class HostConfigurationInvalid:
    """These bytes are not a project-root mapping, and why."""

    reason: HostConfigurationRefusal
    detail: str = ""

    def __str__(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"{self.reason.value}{suffix}"


type HostProjectRootVerdict = HostProjectRootAccepted | HostConfigurationInvalid


def read_host_project_root_document(document: bytes) -> HostProjectRootVerdict:
    """Whether these exact bytes are one project-id → root-path revision."""

    if len(document) > MAXIMUM_HOST_PROJECT_ROOT_DOCUMENT_BYTES:
        return HostConfigurationInvalid(
            HostConfigurationRefusal.DOCUMENT_TOO_LARGE,
            f"{len(document)} bytes exceeds {MAXIMUM_HOST_PROJECT_ROOT_DOCUMENT_BYTES}",
        )
    try:
        text = document.decode("utf-8")
    except UnicodeDecodeError as broken:
        return HostConfigurationInvalid(
            HostConfigurationRefusal.DOCUMENT_NOT_UTF8, broken.reason
        )
    if text.startswith(_BYTE_ORDER_MARK):
        return HostConfigurationInvalid(
            HostConfigurationRefusal.DOCUMENT_NOT_UTF8, "byte order mark"
        )
    try:
        decoded = json.loads(text)
    except ValueError as broken:
        return HostConfigurationInvalid(
            HostConfigurationRefusal.NOT_A_PROJECT_ROOT_OBJECT, str(broken)
        )
    if type(decoded) is not dict:
        return HostConfigurationInvalid(
            HostConfigurationRefusal.NOT_A_PROJECT_ROOT_OBJECT,
            f"a project-root mapping is an object, not {type(decoded).__name__}",
        )
    unknown = sorted(name for name in decoded if name not in _KNOWN_FIELDS)
    if unknown:
        return HostConfigurationInvalid(
            HostConfigurationRefusal.UNKNOWN_FIELD,
            "a project-root mapping names project_id, revision_number and "
            f"root_path, not {', '.join(unknown)}",
        )
    project_id = decoded.get(PROJECT_ID_FIELD)
    revision_number = decoded.get(REVISION_NUMBER_FIELD)
    root_path = decoded.get(ROOT_PATH_FIELD)
    if (
        type(project_id) is not str
        or not 1 <= len(project_id) <= MAXIMUM_PROJECT_ID_CHARACTERS
        or "\x00" in project_id
    ):
        return HostConfigurationInvalid(
            HostConfigurationRefusal.NOT_A_PROJECT_ROOT_OBJECT,
            f"project_id must be 1..{MAXIMUM_PROJECT_ID_CHARACTERS} exact characters",
        )
    if (
        type(revision_number) is not int
        or not 1 <= revision_number <= MAXIMUM_SIGNED_INT64
    ):
        return HostConfigurationInvalid(
            HostConfigurationRefusal.NOT_A_PROJECT_ROOT_OBJECT,
            "revision_number must be a positive signed int64",
        )
    if (
        type(root_path) is not str
        or not 1 <= len(root_path) <= MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS
        or "\x00" in root_path
    ):
        return HostConfigurationInvalid(
            HostConfigurationRefusal.NOT_A_PROJECT_ROOT_OBJECT,
            "root_path must be 1.."
            f"{MAXIMUM_PROJECT_ROOT_PATH_CHARACTERS} exact characters",
        )
    path = Path(root_path)
    if not path.is_absolute():
        return HostConfigurationInvalid(
            HostConfigurationRefusal.RELATIVE_ROOT_PATH,
            f"root_path must be absolute, not {root_path!r}",
        )
    return HostProjectRootAccepted(ProjectId(project_id), revision_number, path)


@dataclass(frozen=True)
class HostProjectRootRevision:
    """One immutable project-root mapping: exact bytes in, hash out."""

    document: bytes
    mapping: HostProjectRootAccepted
    revision_hash: HostConfigurationRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "revision_hash",
            HostConfigurationRevisionHash.of(self.document),
        )

    @property
    def project_id(self) -> ProjectId:
        return self.mapping.project_id

    @property
    def revision_number(self) -> int:
        return self.mapping.revision_number

    @property
    def root_path(self) -> Path:
        return self.mapping.root_path


@dataclass(frozen=True)
class ProjectRootFound:
    root_path: Path
    revision: HostProjectRootRevision


@dataclass(frozen=True)
class ProjectUnknown:
    """An operation named a project with no configured root."""

    project_id: ProjectId

    def __str__(self) -> str:
        return (
            f"{PROJECT_UNKNOWN}: project {self.project_id.value!r} "
            "has no configured root"
        )


@dataclass(frozen=True)
class HostProjectRootRevisionCreated:
    revision: HostProjectRootRevision


@dataclass(frozen=True)
class HostProjectRootRevisionExisting:
    revision: HostProjectRootRevision


@dataclass(frozen=True)
class HostProjectRootRevisionConflict:
    def __str__(self) -> str:
        return HOST_CONFIGURATION_CONFLICT


@dataclass(frozen=True)
class HostProjectRootRevisionCollision:
    def __str__(self) -> str:
        return HOST_CONFIGURATION_COLLISION
