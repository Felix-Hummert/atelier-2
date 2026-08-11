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
    WAITING_INPUT = "WAITING_INPUT"
    COMPLETED = "COMPLETED"


@dataclass(frozen=True)
class Run:
    run_id: RunId
    revision_hash: WorkflowRevisionHash
    state: RunState
    current_node_id: str
    state_version: int
    last_event_sequence: int
    terminal_hash: Sha256Hash | None = None

    def __post_init__(self) -> None:
        if self.current_node_id == "":
            raise ValueError("current_node_id must be nonempty")
        if self.state_version < 0 or self.last_event_sequence < 0:
            raise ValueError("run versions and cursors must be nonnegative")
        if (self.state is RunState.COMPLETED) != (self.terminal_hash is not None):
            raise ValueError("only a completed run has a terminal hash")


@dataclass(frozen=True)
class StartRunRequest:
    run_id: RunId
    revision: WorkflowRevision
