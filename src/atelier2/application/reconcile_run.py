from __future__ import annotations

from dataclasses import dataclass
from typing import assert_never

from atelier2.application.answer_wait import RunMissing
from atelier2.application.publish_workflow_revision import (
    DurableStateCorrupt,
    WriteUnavailable,
)
from atelier2.application.reconcile_effect import (
    ReconcileRunResult,
    ReconciliationCommandConflict,
    ReconciliationTargetMissing,
    reconcile_effect_result,
)
from atelier2.contracts.effects import (
    EffectIntentStateVersion,
    ReconcileActor,
    ReconcileCommand,
    ReconcileCommandId,
    ReconcileDetermination,
)
from atelier2.contracts.runs import RunId
from atelier2.ports.effects import TransactionalEffectReconcileCommander
from atelier2.ports.run_queries import (
    ReconciliationRetryCommandConflict,
    ReconciliationRetryTargetFound,
    ReconciliationRetryTargetMissing,
    RunFound,
    RunQueries,
    RunQueryMissing,
)
from atelier2.ports.workflow_revisions import (
    QueryDurableStateCorrupt,
    ReadUnavailable,
    WorkflowProjectionLimit,
)


@dataclass(frozen=True)
class ReconcileRunRequest:
    run_id: RunId
    command_id: ReconcileCommandId
    expected_intent_state_version: EffectIntentStateVersion
    actor: ReconcileActor
    evidence: str
    determination: ReconcileDetermination


def reconcile_run(
    request: ReconcileRunRequest,
    queries: RunQueries,
    commander: TransactionalEffectReconcileCommander,
    projection_limit: WorkflowProjectionLimit | None = None,
) -> ReconcileRunResult:
    found = queries.get_run(request.run_id, projection_limit)
    match found:
        case RunQueryMissing():
            return RunMissing()
        case ReadUnavailable(detail):
            return WriteUnavailable(detail)
        case QueryDurableStateCorrupt():
            return DurableStateCorrupt()
        case RunFound(projection):
            if projection.reconciliation is not None:
                intent = projection.reconciliation.intent
            else:
                retry_target = queries.get_reconciliation_retry_target(
                    request.run_id, request.command_id
                )
                match retry_target:
                    case ReconciliationRetryTargetFound(intent):
                        pass
                    case ReconciliationRetryTargetMissing():
                        return ReconciliationTargetMissing()
                    case ReconciliationRetryCommandConflict():
                        return ReconciliationCommandConflict()
                    case RunQueryMissing():
                        return RunMissing()
                    case ReadUnavailable():
                        return WriteUnavailable()
                    case QueryDurableStateCorrupt():
                        return DurableStateCorrupt()
                    case _ as unreachable:
                        assert_never(unreachable)
            command = ReconcileCommand(
                request.command_id,
                intent.intent.reference,
                request.expected_intent_state_version,
                request.actor,
                request.evidence,
                request.determination,
            )
            return reconcile_effect_result(command, commander)
        case _ as unreachable:
            assert_never(unreachable)
