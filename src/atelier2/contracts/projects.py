"""A project: the isolation unit ADR 0011 decision 1 names, at its first slice.

Decision 1 asks for a configured bundle with a minted id and one root -- a
repository, a tracker connection, a credential reference, workflow rules, an
item filter, running state. #133 Kopf 1 builds none of that yet (source
linking is its own head, members and rights wait on #82, chat is #7): what it
gives a project today is exactly what a caller can create and read -- an id
and a name -- so a run can start under it and a list can filter by it. The
richer bundle, its rename-as-alias-event history and its pause/resume
lifecycle stay unbuilt because no feature reads them yet; recording that shape
now would be scaffolding without a caller.
"""

from __future__ import annotations

from dataclasses import dataclass

from atelier2.contracts.hashing import SHA256_HEX_DIGEST

MAXIMUM_PROJECT_NAME_CHARACTERS = 200


@dataclass(frozen=True)
class ProjectId:
    """A project's minted, opaque identity: 32 random bytes, hex-encoded.

    ADR 0011 decision 1 requires an id "deliberately not derived from the
    bundle" -- unlike this codebase's content-addressed `Sha256Hash` family,
    a project id is minted once at the adapter boundary with a CSPRNG
    (`secrets.token_hex(32)`) and never recomputed. It is not a hash of
    anything; it shares `Sha256Hash`'s exact 64-lowercase-hex shape on
    purpose, because every other persisted hex-checked column in this schema
    is that shape and no second one exists
    (`test_every_persisted_hash_column_is_bounded_at_the_length_of_a_real_digest`).
    """

    value: str

    def __post_init__(self) -> None:
        if SHA256_HEX_DIGEST.fullmatch(self.value) is None:
            raise ValueError("ProjectId must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True)
class ProjectName:
    """The name an operator gave a project, unique among live projects."""

    value: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.value) <= MAXIMUM_PROJECT_NAME_CHARACTERS:
            raise ValueError(
                "project name must contain 1.."
                f"{MAXIMUM_PROJECT_NAME_CHARACTERS} characters"
            )
        if self.value.strip() == "":
            raise ValueError("project name must not be blank")


@dataclass(frozen=True)
class Project:
    project_id: ProjectId
    name: ProjectName
