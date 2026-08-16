from __future__ import annotations

import re
from dataclasses import dataclass, field

from atelier2.contracts.hashing import SHA256_HEX_DIGEST, Sha256Hash, frame
from atelier2.contracts.revisions_v3 import PublishedRevisionHash, RevisionKind

MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS = 128
_LINEAGE_DISPLAY_NAME = re.compile(r"[a-z][a-z0-9._-]*")


class CatalogLineageId(Sha256Hash):
    """The store-stable identity derived from one kind and founding revision."""


@dataclass(frozen=True)
class CatalogLineageDisplayName:
    """One unambiguous operator-authored name for a catalog lineage."""

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("a catalog lineage display name must be text")
        if not 1 <= len(self.value) <= MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS:
            raise ValueError(
                "a catalog lineage display name must contain 1 to "
                f"{MAXIMUM_LINEAGE_DISPLAY_NAME_CHARACTERS} characters"
            )
        if _LINEAGE_DISPLAY_NAME.fullmatch(self.value) is None:
            raise ValueError(
                "a catalog lineage display name must match [a-z][a-z0-9._-]*"
            )
        if SHA256_HEX_DIGEST.fullmatch(self.value) is not None:
            raise ValueError(
                "a catalog lineage display name cannot look like a lineage id"
            )


@dataclass(frozen=True)
class CatalogLineage:
    """The immutable identity-bearing root of one revision lineage."""

    kind: RevisionKind
    founding_revision_hash: PublishedRevisionHash
    lineage_id: CatalogLineageId = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RevisionKind):
            raise TypeError("a catalog lineage names its kind through the contract")
        if not isinstance(self.founding_revision_hash, PublishedRevisionHash):
            raise TypeError(
                "a catalog lineage names its founding revision through the contract"
            )
        object.__setattr__(
            self,
            "lineage_id",
            CatalogLineageId.of(
                frame(
                    "catalog-lineage/v1",
                    self.kind.value.encode("ascii"),
                    self.founding_revision_hash.value.encode("ascii"),
                )
            ),
        )
