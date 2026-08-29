from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.catalog_intakes import CatalogIntake, CatalogIntakeId
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable


@dataclass(frozen=True)
class CatalogIntakeStored:
    intake: CatalogIntake


@dataclass(frozen=True)
class CatalogIntakeExisting:
    intake: CatalogIntake


@dataclass(frozen=True)
class CatalogIntakeFound:
    intake: CatalogIntake


@dataclass(frozen=True)
class CatalogIntakeMissing:
    intake_id: CatalogIntakeId


type StoreCatalogIntakeResult = (
    CatalogIntakeStored
    | CatalogIntakeExisting
    | DurableWriteUnavailable
    | DurableStateCorrupt
)
type ReadCatalogIntakeResult = (
    CatalogIntakeFound
    | CatalogIntakeMissing
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class CatalogIntakes(Protocol):
    def store_intake(self, intake: CatalogIntake) -> StoreCatalogIntakeResult: ...
    def read_intake(self, intake_id: CatalogIntakeId) -> ReadCatalogIntakeResult: ...
