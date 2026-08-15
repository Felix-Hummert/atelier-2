from dataclasses import dataclass
from typing import assert_never

from atelier2.application.publish_workflow_revision import (
    DurableStateCorrupt,
    WriteUnavailable,
)
from atelier2.contracts.executions import (
    SubmitWaitAnswerRequest,
    WaitAnswerSnapshot,
    WaitAnswerState,
)
from atelier2.ports.durable_runs import (
    DurableAnswerBytesConflict,
    DurableAnswerCreated,
    DurableAnswerExisting,
    DurableAnswerNodeMissing,
    DurableAnswerRevisionConflict,
    DurableAnswerRunMissing,
    DurableAnswerStateConflict,
    DurableWriteUnavailable,
    TransactionalWaitAnswerer,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)


@dataclass(frozen=True)
class AnswerAcceptedPending:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class AnswerExistingPending:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class AnswerExistingApplied:
    snapshot: WaitAnswerSnapshot


@dataclass(frozen=True)
class RunMissing:
    pass


@dataclass(frozen=True)
class NodeMissing:
    pass


@dataclass(frozen=True)
class AnswerRevisionConflict:
    pass


@dataclass(frozen=True)
class AnswerStateConflict:
    pass


@dataclass(frozen=True)
class AnswerBytesConflict:
    pass


type AnswerWaitResult = (
    AnswerAcceptedPending
    | AnswerExistingPending
    | AnswerExistingApplied
    | RunMissing
    | NodeMissing
    | AnswerRevisionConflict
    | AnswerStateConflict
    | AnswerBytesConflict
    | WriteUnavailable
    | DurableStateCorrupt
)


def answer_wait_result(
    request: SubmitWaitAnswerRequest, answerer: TransactionalWaitAnswerer
) -> AnswerWaitResult:
    result = answerer.submit_result(request)
    match result:
        case DurableAnswerCreated(snapshot):
            return AnswerAcceptedPending(snapshot)
        case DurableAnswerExisting(snapshot):
            if snapshot.state is WaitAnswerState.PENDING:
                return AnswerExistingPending(snapshot)
            return AnswerExistingApplied(snapshot)
        case DurableAnswerRunMissing():
            return RunMissing()
        case DurableAnswerNodeMissing():
            return NodeMissing()
        case DurableAnswerRevisionConflict():
            return AnswerRevisionConflict()
        case DurableAnswerStateConflict():
            return AnswerStateConflict()
        case DurableAnswerBytesConflict():
            return AnswerBytesConflict()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
