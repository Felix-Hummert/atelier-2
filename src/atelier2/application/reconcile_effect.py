from dataclasses import dataclass
from typing import assert_never

from atelier2.application.answer_wait import RunMissing
from atelier2.application.publish_workflow_revision import (
    DurableStateCorrupt,
    WriteUnavailable,
)
from atelier2.contracts.effects import (
    ReconcileCommand,
    ReconcileCommandSnapshot,
    ReconcileCommandState,
)
from atelier2.ports.durable_runs import (
    DurableStateCorrupt as PortDurableStateCorrupt,
)
from atelier2.ports.durable_runs import (
    DurableWriteUnavailable,
)
from atelier2.ports.effects import (
    DurableReconciliationCommandConflict,
    DurableReconciliationCreated,
    DurableReconciliationDeterminationConflict,
    DurableReconciliationExisting,
    DurableReconciliationTargetMissing,
    TransactionalEffectReconcileCommander,
)


@dataclass(frozen=True)
class ReconciliationAcceptedPending:
    snapshot: ReconcileCommandSnapshot


@dataclass(frozen=True)
class ReconciliationExistingPending:
    snapshot: ReconcileCommandSnapshot


@dataclass(frozen=True)
class ReconciliationExistingApplied:
    snapshot: ReconcileCommandSnapshot


@dataclass(frozen=True)
class ReconciliationExistingRejected:
    snapshot: ReconcileCommandSnapshot


@dataclass(frozen=True)
class ReconciliationTargetMissing:
    pass


@dataclass(frozen=True)
class ReconciliationStale:
    pass


@dataclass(frozen=True)
class ReconciliationCommandConflict:
    pass


@dataclass(frozen=True)
class ReconciliationDeterminationConflict:
    pass


type ReconcileRunResult = (
    ReconciliationAcceptedPending
    | ReconciliationExistingPending
    | ReconciliationExistingApplied
    | ReconciliationExistingRejected
    | RunMissing
    | ReconciliationTargetMissing
    | ReconciliationStale
    | ReconciliationCommandConflict
    | ReconciliationDeterminationConflict
    | WriteUnavailable
    | DurableStateCorrupt
)


def reconcile_effect_result(
    command: ReconcileCommand, commander: TransactionalEffectReconcileCommander
) -> ReconcileRunResult:
    result = commander.submit_result(command)
    match result:
        case DurableReconciliationCreated(snapshot):
            if snapshot.state is ReconcileCommandState.PENDING:
                return ReconciliationAcceptedPending(snapshot)
            return ReconciliationStale()
        case DurableReconciliationExisting(snapshot):
            if snapshot.state is ReconcileCommandState.PENDING:
                return ReconciliationExistingPending(snapshot)
            if snapshot.state is ReconcileCommandState.APPLIED:
                return ReconciliationExistingApplied(snapshot)
            return ReconciliationExistingRejected(snapshot)
        case DurableReconciliationTargetMissing():
            return ReconciliationTargetMissing()
        case DurableReconciliationCommandConflict():
            return ReconciliationCommandConflict()
        case DurableReconciliationDeterminationConflict():
            return ReconciliationDeterminationConflict()
        case DurableWriteUnavailable():
            return WriteUnavailable()
        case PortDurableStateCorrupt():
            return DurableStateCorrupt()
        case _ as unreachable:
            assert_never(unreachable)
