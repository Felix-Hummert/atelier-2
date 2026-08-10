from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.hashing import Sha256Hash


class RunIdentityConflict(RuntimeError):
    """A caller reused a RunId for a different workflow revision."""


class RevisionHashCollision(RuntimeError):
    """Durable bytes disagree with the document identified by their hash."""


class WorkflowRevisionHash(Sha256Hash):
    """The immutable product identity of one workflow document."""


@dataclass(frozen=True)
class WorkflowRevision:
    document: bytes
    revision_hash: WorkflowRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "revision_hash", WorkflowRevisionHash.of(self.document)
        )


@dataclass(frozen=True)
class RunId:
    value: str

    def __post_init__(self) -> None:
        if self.value == "":
            raise ValueError("RunId must be a nonempty caller string")


class RunState(StrEnum):
    STARTED = "STARTED"
    WAITING_RECONCILIATION = "WAITING_RECONCILIATION"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class Run:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    state: RunState


@dataclass(frozen=True)
class StartRunRequest:
    run_id: RunId
    revision: WorkflowRevision
