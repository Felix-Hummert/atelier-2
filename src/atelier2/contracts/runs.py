from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum


class RunIdentityConflict(RuntimeError):
    """A caller reused a RunId for a different workflow revision."""


class RevisionHashCollision(RuntimeError):
    """Durable bytes disagree with the document identified by their hash."""


@dataclass(frozen=True)
class WorkflowRevisionHash:
    value: str

    @classmethod
    def from_document(cls, document: bytes) -> WorkflowRevisionHash:
        return cls(hashlib.sha256(document).hexdigest())


@dataclass(frozen=True)
class WorkflowRevision:
    document: bytes
    revision_hash: WorkflowRevisionHash = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "revision_hash", WorkflowRevisionHash.from_document(self.document)
        )


@dataclass(frozen=True)
class RunId:
    value: str

    def __post_init__(self) -> None:
        if self.value == "":
            raise ValueError("RunId must be a nonempty caller string")


class RunState(StrEnum):
    STARTED = "STARTED"
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
