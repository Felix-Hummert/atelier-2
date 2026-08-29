"""The declared, opaque documents the catalog has accepted."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.catalog_v3 import CatalogActivatedAt, CatalogActor
from atelier2.contracts.hashing import Sha256Hash, frame


class CatalogIntakeKind(StrEnum):
    AGENT = "agent"
    SKILL = "skill"
    WORKFLOW = "workflow"


class CatalogIntakeId(Sha256Hash):
    """The identity of exact bytes under the kind the caller declared."""


@dataclass(frozen=True)
class CatalogIntake:
    kind: CatalogIntakeKind
    document: bytes
    actor: CatalogActor
    activated_at: CatalogActivatedAt
    intake_id: CatalogIntakeId = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, CatalogIntakeKind):
            raise TypeError("a catalog intake requires a declared kind")
        if not isinstance(self.document, bytes):
            raise TypeError("a catalog intake document must be bytes")
        object.__setattr__(
            self,
            "intake_id",
            CatalogIntakeId.of(
                frame(
                    "catalog-intake/v1", self.kind.value.encode("ascii"), self.document
                )
            ),
        )
