"""The order one run carries into a workflow it reuses.

A published workflow states which named, typed artifacts a run must bring; a run
start brings them as the members of one immutable context package. The package
carries its own identity, so a run can be bound to the exact order it was started
with without the workflow document ever changing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.hashing import Sha256Hash, frame

MAXIMUM_RUN_INPUT_NAME_CHARACTERS = 64
MAXIMUM_RUN_INPUT_MEMBERS = 16
# A run input artifact is a real document, diff, or brief, so it owns a payload
# bound of its own instead of borrowing the one-line bound every authored
# workflow field carries. The value is the decoded-payload scale the HTTP host
# already admits, which is what one run start can transport.
MAXIMUM_RUN_INPUT_BYTES = 49_152

_RUN_INPUT_NAME = re.compile(
    f"[a-z][a-z0-9_]{{0,{MAXIMUM_RUN_INPUT_NAME_CHARACTERS - 1}}}"
)


class RunInputMemberHash(Sha256Hash):
    """Identity of one named, typed artifact a run start carries."""


class ContextPackageHash(Sha256Hash):
    """Identity of the complete context package frozen into one run."""


@dataclass(frozen=True)
class RunInputName:
    value: str

    def __post_init__(self) -> None:
        if _RUN_INPUT_NAME.fullmatch(self.value) is None:
            raise ValueError(
                "run input name must be a lowercase ASCII slug of "
                f"1..{MAXIMUM_RUN_INPUT_NAME_CHARACTERS} characters"
            )


class RunInputMediaType(StrEnum):
    TEXT = "text/plain"
    BYTES = "application/octet-stream"


def _require_typed_identity(name: object, media_type: object) -> None:
    if not isinstance(name, RunInputName) or not isinstance(
        media_type, RunInputMediaType
    ):
        raise TypeError("run input name and media type must use their typed contracts")


@dataclass(frozen=True)
class RunInputMember:
    name: RunInputName
    media_type: RunInputMediaType
    content: bytes
    member_hash: RunInputMemberHash = field(init=False)

    def __post_init__(self) -> None:
        _require_typed_identity(self.name, self.media_type)
        if not isinstance(self.content, bytes) or not (
            1 <= len(self.content) <= MAXIMUM_RUN_INPUT_BYTES
        ):
            raise ValueError(
                f"run input member {self.name.value!r} must carry "
                f"1..{MAXIMUM_RUN_INPUT_BYTES} exact bytes"
            )
        if self.media_type is RunInputMediaType.TEXT:
            try:
                self.content.decode("utf-8")
            except UnicodeDecodeError as error:
                raise ValueError(
                    "a text run input member must carry valid UTF-8 content"
                ) from error
        object.__setattr__(
            self,
            "member_hash",
            RunInputMemberHash.of(
                frame(
                    "run-input-member/v1",
                    self.name.value.encode("ascii"),
                    self.media_type.value.encode("ascii"),
                    self.content,
                )
            ),
        )


@dataclass(frozen=True)
class RunInputContextPackage:
    members: tuple[RunInputMember, ...]
    context_package_hash: ContextPackageHash = field(init=False)

    def __post_init__(self) -> None:
        ordered = tuple(
            sorted(self.members, key=lambda member: member.name.value.encode("ascii"))
        )
        if not 1 <= len(ordered) <= MAXIMUM_RUN_INPUT_MEMBERS:
            raise ValueError(
                f"a context package carries 1..{MAXIMUM_RUN_INPUT_MEMBERS} members"
            )
        if len({member.name for member in ordered}) != len(ordered):
            raise ValueError("context package member names must be unique")
        if sum(len(member.content) for member in ordered) > MAXIMUM_RUN_INPUT_BYTES:
            raise ValueError(
                "a context package carries at most "
                f"{MAXIMUM_RUN_INPUT_BYTES} exact bytes over all its members"
            )
        object.__setattr__(self, "members", ordered)
        object.__setattr__(
            self,
            "context_package_hash",
            ContextPackageHash.of(
                frame(
                    "context-package/v1",
                    *(member.member_hash.value.encode("ascii") for member in ordered),
                )
            ),
        )

    def member(self, name: RunInputName) -> RunInputMember:
        for member in self.members:
            if member.name == name:
                return member
        raise KeyError(name.value)

    @property
    def names(self) -> frozenset[RunInputName]:
        return frozenset(member.name for member in self.members)


@dataclass(frozen=True)
class DeclaredRunInput:
    name: RunInputName
    media_type: RunInputMediaType

    def __post_init__(self) -> None:
        _require_typed_identity(self.name, self.media_type)


@dataclass(frozen=True)
class RunInputSchema:
    """What a published workflow revision requires every one of its runs to bring."""

    declared: tuple[DeclaredRunInput, ...]

    def __post_init__(self) -> None:
        ordered = tuple(
            sorted(self.declared, key=lambda entry: entry.name.value.encode("ascii"))
        )
        if len(ordered) > MAXIMUM_RUN_INPUT_MEMBERS:
            raise ValueError(
                f"a workflow declares at most {MAXIMUM_RUN_INPUT_MEMBERS} run inputs"
            )
        if len({entry.name for entry in ordered}) != len(ordered):
            raise ValueError("declared run input names must be unique")
        object.__setattr__(self, "declared", ordered)

    def media_type_of(self, name: RunInputName) -> RunInputMediaType | None:
        for entry in self.declared:
            if entry.name == name:
                return entry.media_type
        return None

    @property
    def names(self) -> frozenset[RunInputName]:
        return frozenset(entry.name for entry in self.declared)
