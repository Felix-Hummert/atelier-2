"""Store an opaque library intake under the kind the caller declared."""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import (
    DurableStateCorrupt,
    ReadUnavailable,
    WriteUnavailable,
)
from atelier2.contracts.catalog_intakes import (
    CatalogIntake,
    CatalogIntakeId,
    CatalogIntakeKind,
)
from atelier2.contracts.catalog_v3 import CatalogActivatedAt, CatalogActor
from atelier2.ports.catalog_intakes import (
    CatalogIntakeExisting as PortCatalogIntakeExisting,
)
from atelier2.ports.catalog_intakes import (
    CatalogIntakeFound as PortCatalogIntakeFound,
)
from atelier2.ports.catalog_intakes import (
    CatalogIntakeMissing as PortCatalogIntakeMissing,
)
from atelier2.ports.catalog_intakes import CatalogIntakes
from atelier2.ports.catalog_intakes import (
    CatalogIntakeStored as PortCatalogIntakeStored,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import (
    DurableWriteUnavailable as PortDurableWriteUnavailable,
)


class LibraryAdditionOutcome:
    """The application-owned answer to one catalog intake command."""


class LibraryAdditionReadOutcome:
    """The application-owned answer to one catalog intake read."""


@dataclass(frozen=True)
class LibraryAdditionStored(LibraryAdditionOutcome):
    intake: CatalogIntake


@dataclass(frozen=True)
class LibraryAdditionExisting(LibraryAdditionOutcome):
    intake: CatalogIntake


@dataclass(frozen=True)
class LibraryAdditionFound(LibraryAdditionReadOutcome):
    intake: CatalogIntake


@dataclass(frozen=True)
class LibraryAdditionMissing(LibraryAdditionReadOutcome):
    intake_id: CatalogIntakeId


type AdmitLibraryAdditionResult = (
    LibraryAdditionStored
    | LibraryAdditionExisting
    | WriteUnavailable
    | DurableStateCorrupt
)
type ReadLibraryAdditionResult = (
    LibraryAdditionFound
    | LibraryAdditionMissing
    | ReadUnavailable
    | DurableStateCorrupt
)


def admit_library_addition(
    document: bytes,
    kind: CatalogIntakeKind,
    actor: CatalogActor,
    activated_at: CatalogActivatedAt,
    intakes: CatalogIntakes,
) -> AdmitLibraryAdditionResult:
    match intakes.store_intake(CatalogIntake(kind, document, actor, activated_at)):
        case PortCatalogIntakeStored(intake):
            return LibraryAdditionStored(intake)
        case PortCatalogIntakeExisting(intake):
            return LibraryAdditionExisting(intake)
        case PortDurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)


def read_library_addition(
    intake_id: CatalogIntakeId, intakes: CatalogIntakes
) -> ReadLibraryAdditionResult:
    match intakes.read_intake(intake_id):
        case PortCatalogIntakeFound(intake):
            return LibraryAdditionFound(intake)
        case PortCatalogIntakeMissing(missing_id):
            return LibraryAdditionMissing(missing_id)
        case PortDurableWriteUnavailable():
            return ReadUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
