from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.executions import (
    SubmitWaitAnswerRequest,
    WaitAnswerSnapshot,
    WaitAnswerState,
    is_canonical_integer_bytes,
)
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
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
    UnanswerableWait
    | AnswerAcceptedPending
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


@dataclass(frozen=True)
class UnanswerableWait:
    """The authored answer does not make one submission for this wait."""


def answer_wait_result(
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
    answer_bytes: bytes,
    answerer: TransactionalWaitAnswerer,
) -> AnswerWaitResult:
    """Answer one waiting node, from the values an author supplied.

    Building the submission is part of the decision rather than a step before it:
    a node nobody named, or answer bytes that are not the canonical form a wait
    accepts, refuse the answer in the same vocabulary as everything else that can
    go wrong here. The store is not asked in that case.
    """
    if not is_canonical_integer_bytes(answer_bytes):
        return UnanswerableWait()
    try:
        request = SubmitWaitAnswerRequest(run_id, revision_hash, node_id, answer_bytes)
    except (TypeError, ValueError):
        return UnanswerableWait()
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
