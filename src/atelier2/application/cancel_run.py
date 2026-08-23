"""Ask for one run to stop under one operator command, in this layer's words.

Mirrors `application/cancel_agent_attempt.py`: the store answers with its own
port vocabulary, and this is the one place that reads it and says what it
means for a caller above. #439 P2 wires the store path this module calls; #439
P4 gives it an HTTP route. Until then this module's only caller is its own
tests -- inert the same way `contracts/run_cancellations.py` was before this
phase gave it one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.refusals import DurableStateCorrupt, WriteUnavailable
from atelier2.contracts.agent_attempts import AgentAttempt
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.run_bindings import AnyRun
from atelier2.contracts.run_cancellations import CancelRunRequest
from atelier2.contracts.runs import RunId
from atelier2.ports.agent_attempts import (
    RunCancellationAccepted as DurableAccepted,
)
from atelier2.ports.agent_attempts import (
    RunCancellationCommandConflict as DurableCommandConflict,
)
from atelier2.ports.agent_attempts import (
    RunCancellationNotCancellable as DurableNotCancellable,
)
from atelier2.ports.agent_attempts import (
    RunCancellationOvertakenBySuccess as DurableOvertakenBySuccess,
)
from atelier2.ports.agent_attempts import (
    RunCancellationRefusal,
    TransactionalAgentAttemptCanceller,
)
from atelier2.ports.agent_attempts import (
    RunCancellationRunMissing as DurableRunMissing,
)
from atelier2.ports.agent_attempts import (
    RunCancellationTerminalRetry as DurableTerminalRetry,
)
from atelier2.ports.durable_runs import DurableStateCorrupt as PortDurableStateCorrupt
from atelier2.ports.durable_runs import DurableWriteUnavailable as PortWriteUnavailable


@dataclass(frozen=True)
class CancelAccepted:
    """A genuinely new command moved the run's live attempt to `CANCEL_REQUESTED`."""

    attempt: AgentAttempt


@dataclass(frozen=True)
class CancelTerminalRetry:
    """The exact command was already accepted, and cleanup already ended it."""

    run: AnyRun


@dataclass(frozen=True)
class CancelOvertakenBySuccess:
    """The exact command was accepted, but the attempt succeeded first.

    The run kept going; this command did not end it.
    """

    run: AnyRun


@dataclass(frozen=True)
class CancelNotCancellable:
    reason: RunCancellationRefusal


@dataclass(frozen=True)
class CancelCommandConflict:
    """This run's live attempt already carries a different command's cancel."""


@dataclass(frozen=True)
class CancelRunMissing:
    pass


@dataclass(frozen=True)
class MalformedIdempotencyKey:
    """The authored idempotency key is no key a run-cancel command can mint from."""


type CancelRunResult = (
    CancelAccepted
    | CancelTerminalRetry
    | CancelOvertakenBySuccess
    | CancelNotCancellable
    | CancelCommandConflict
    | CancelRunMissing
    | MalformedIdempotencyKey
    | WriteUnavailable
    | DurableStateCorrupt
)


def cancel_run_result(
    run_id: RunId,
    idempotency_key: str,
    expected_node_execution_id: NodeExecutionId,
    canceller: TransactionalAgentAttemptCanceller,
) -> CancelRunResult:
    """One operator run-cancel confirmation, answered in this layer's words.

    Building the typed request is part of the decision: the boundary shape
    (`CancelRunRequest`) is where the idempotency key's length is enforced, so
    an authored key the store is never asked about still answers in the same
    vocabulary as every other refusal here, exactly as `answer_wait_result`
    refuses an unbuildable submission before its store is asked.
    """
    try:
        request = CancelRunRequest(run_id, idempotency_key, expected_node_execution_id)
    except (TypeError, ValueError):
        return MalformedIdempotencyKey()
    match canceller.request_run_cancellation(request):
        case DurableAccepted(attempt):
            return CancelAccepted(attempt)
        case DurableTerminalRetry(run):
            return CancelTerminalRetry(run)
        case DurableOvertakenBySuccess(run):
            return CancelOvertakenBySuccess(run)
        case DurableNotCancellable(reason):
            return CancelNotCancellable(reason)
        case DurableCommandConflict():
            return CancelCommandConflict()
        case DurableRunMissing():
            return CancelRunMissing()
        case PortWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
