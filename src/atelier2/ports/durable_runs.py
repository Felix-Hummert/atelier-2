from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from atelier2.contracts.executions import SubmitWaitAnswerRequest, WaitAnswerSnapshot
from atelier2.contracts.runs import Run, StartRunRequest


@dataclass(frozen=True)
class DurableWriteUnavailable:
    pass


@dataclass(frozen=True)
class DurableStateCorrupt:
    pass


@dataclass(frozen=True)
class DurableRunCreated:
    run: Run


@dataclass(frozen=True)
class DurableRunExisting:
    run: Run


@dataclass(frozen=True)
class DurableRunRevisionMissing:
    pass


@dataclass(frozen=True)
class DurableRunIdentityConflict:
    pass


type DurablePublishedRunResult = (
    DurableRunCreated
    | DurableRunExisting
    | DurableRunRevisionMissing
    | DurableRunIdentityConflict
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


@dataclass(frozen=True)
class DurableAnswerCreated:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class DurableAnswerExisting:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class DurableAnswerRunMissing:
    pass


@dataclass(frozen=True)
class DurableAnswerNodeMissing:
    pass


@dataclass(frozen=True)
class DurableAnswerRevisionConflict:
    pass


@dataclass(frozen=True)
class DurableAnswerStateConflict:
    pass


@dataclass(frozen=True)
class DurableAnswerBytesConflict:
    pass


type DurableAnswerResult = (
    DurableAnswerCreated
    | DurableAnswerExisting
    | DurableAnswerRunMissing
    | DurableAnswerNodeMissing
    | DurableAnswerRevisionConflict
    | DurableAnswerStateConflict
    | DurableAnswerBytesConflict
    | DurableWriteUnavailable
    | DurableStateCorrupt
)


class DurableRunStarter(Protocol):
    def start(self, request: StartRunRequest) -> Run: ...


class WaitAnswerer(Protocol):
    def submit(self, request: SubmitWaitAnswerRequest) -> WaitAnswerSnapshot: ...


class TransactionalWaitAnswerer(Protocol):
    def submit_result(
        self, request: SubmitWaitAnswerRequest
    ) -> DurableAnswerResult: ...
