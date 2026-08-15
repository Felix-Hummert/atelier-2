from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.run_store import (
    AgentReceiptConflict,
    RunTransitionConflict,
    _agent_receipt_v2_from_record,
    _agent_receipt_v2_values,
    _commit_event,
    _insert_event,
    load_graph,
    load_run,
)
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
    runs,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.agent_attempts import (
    AgentAttempt,
    AgentAttemptCancellation,
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptProcessPhase,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
    AgentProcessOwnerId,
    CancelAgentAttemptRequest,
    WatchdogGenerationId,
)
from atelier2.contracts.agents import (
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentReceiptHash,
    AgentReceiptV2,
)
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    NodeExecutionId,
    RunEvent,
    RunEventKind,
)
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflows import AgentNodeV2, WorkflowGraphV2
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationCommandConflict,
    AgentAttemptCancellationNotCurrent,
    AgentAttemptCancellationResult,
    AgentAttemptCancellationRunMissing,
    AgentAttemptCancellationStale,
    AgentAttemptCancellationTargetMissing,
    AgentAttemptCancellationTerminalConflict,
    AgentAttemptClaimedByThisCall,
    AgentAttemptClaimResult,
    AgentAttemptFailed,
    AgentAttemptPossiblyRan,
    AgentAttemptReplacementNotAllowed,
    AgentAttemptSucceeded,
)

CANCELLATION_WORKFLOW_NAME = "atelier2_agent_attempt_cancellation"
REPLACEMENT_WORKFLOW_NAME = "atelier2_agent_attempt_replacement"


def _attempt_from_record(record: Mapping[Any, Any]) -> AgentAttempt:
    try:
        failure = record["failure_code"]
        receipt = record["receipt_hash"]
        owner = record["process_owner_id"]
        generation = record["watchdog_generation_id"]
        command_id = record["cancellation_command_id"]
        disposition = record["cancellation_disposition"]
        cancellation = (
            None
            if command_id is None
            else AgentAttemptCancellation(
                str(command_id),
                int(record["cancellation_expected_state_version"]),
                AgentAttemptReplacement(str(record["replacement"])),
                AgentAttemptRedriveState(str(record["redrive_state"])),
                (
                    None
                    if disposition is None
                    else AgentAttemptCancellationDisposition(str(disposition))
                ),
            )
        )
        return AgentAttempt(
            AgentAttemptId(str(record["attempt_id"])),
            NodeExecutionId(str(record["node_execution_id"])),
            AgentExecutionRequestHash(str(record["request_hash"])),
            AgentExecutorOperationalIdentity(
                str(record["executor_operational_identity"])
            ),
            RunId(str(record["run_id"])),
            WorkflowRevisionHash(str(record["workflow_revision_hash"])),
            str(record["node_id"]),
            int(record["attempt_ordinal"]),
            AgentAttemptState(str(record["state"])),
            int(record["state_version"]),
            None if failure is None else AgentAttemptFailureCode(str(failure)),
            None if receipt is None else AgentReceiptHash(str(receipt)),
            AgentAttemptProcessPhase(str(record["process_phase"])),
            None if owner is None else AgentProcessOwnerId(str(owner)),
            None if generation is None else WatchdogGenerationId(str(generation)),
            cancellation,
        )
    except (TypeError, ValueError) as error:
        raise RunTransitionConflict(
            "durable agent attempt binding disagrees"
        ) from error


def _attempt_values(attempt: AgentAttempt) -> dict[str, object]:
    return {
        "attempt_id": attempt.attempt_id.value,
        "node_execution_id": attempt.node_execution_id.value,
        "request_hash": attempt.request_hash.value,
        "executor_operational_identity": attempt.executor_operational_identity.value,
        "run_id": attempt.run_id.value,
        "workflow_revision_hash": attempt.workflow_revision_hash.value,
        "node_id": attempt.node_id,
        "attempt_ordinal": attempt.attempt_ordinal,
        "state": attempt.state.value,
        "state_version": attempt.state_version,
        "process_phase": attempt.process_phase.value,
        "process_owner_id": (
            None if attempt.process_owner_id is None else attempt.process_owner_id.value
        ),
        "watchdog_generation_id": (
            None
            if attempt.watchdog_generation_id is None
            else attempt.watchdog_generation_id.value
        ),
        "cancellation_command_id": (
            None if attempt.cancellation is None else attempt.cancellation.command_id
        ),
        "cancellation_expected_state_version": (
            None
            if attempt.cancellation is None
            else attempt.cancellation.expected_attempt_state_version
        ),
        "replacement": (
            None
            if attempt.cancellation is None
            else attempt.cancellation.replacement.value
        ),
        "redrive_state": (
            None
            if attempt.cancellation is None
            else attempt.cancellation.redrive_state.value
        ),
        "cancellation_disposition": (
            None
            if attempt.cancellation is None or attempt.cancellation.disposition is None
            else attempt.cancellation.disposition.value
        ),
        "cancellation_workflow_id": None,
        "failure_code": (
            None if attempt.failure_code is None else attempt.failure_code.value
        ),
        "receipt_hash": (
            None if attempt.receipt_hash is None else attempt.receipt_hash.value
        ),
    }


def _prepared_attempt(execution: AgentAttemptExecution) -> AgentAttempt:
    request = execution.request
    return AgentAttempt(
        execution.attempt_id,
        request.node_execution_id,
        request.request_hash,
        request.executor_operational_identity,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        execution.ordinal,
        AgentAttemptState.PREPARED,
        0,
    )


def _load_attempt(session: Any, attempt_id: AgentAttemptId) -> AgentAttempt:
    record = (
        session.execute(
            sa.select(agent_attempts).where(
                agent_attempts.c.attempt_id == attempt_id.value
            )
        )
        .mappings()
        .one_or_none()
    )
    if record is None:
        raise RunTransitionConflict("agent attempt is missing")
    return _attempt_from_record(record)


def _validate_request(
    session: Any, request: AgentExecutionRequestV2
) -> tuple[RunV2, WorkflowGraphV2]:
    run = load_run(session, request.run_id)
    graph = load_graph(session, request.workflow_revision_hash)
    if not isinstance(run, RunV2) or not isinstance(graph, WorkflowGraphV2):
        raise RunTransitionConflict("agent attempt requires a V2 run")
    if run.revision_hash != request.workflow_revision_hash:
        raise RunTransitionConflict("agent attempt request names another revision")
    node = graph.node(request.node_id)
    if (
        not isinstance(node, AgentNodeV2)
        or node.role != request.resolved_binding.role.value
        or node.job.encode("utf-8") != request.job_bytes
    ):
        raise RunTransitionConflict("agent attempt request differs from durable graph")
    durable_binding = next(
        (
            binding
            for binding in run.agent_bindings
            if binding.role == request.resolved_binding.role
        ),
        None,
    )
    if durable_binding != request.resolved_binding:
        raise RunTransitionConflict(
            "agent attempt request differs from durable binding"
        )
    return run, graph


def _require_attempt_binding(
    attempt: AgentAttempt, execution: AgentAttemptExecution
) -> None:
    request = execution.request
    if (
        attempt.attempt_id != execution.attempt_id
        or attempt.node_execution_id != request.node_execution_id
        or attempt.request_hash != request.request_hash
        or attempt.executor_operational_identity
        != request.executor_operational_identity
        or attempt.run_id != request.run_id
        or attempt.workflow_revision_hash != request.workflow_revision_hash
        or attempt.node_id != request.node_id
        or attempt.attempt_ordinal != execution.ordinal
    ):
        raise RunTransitionConflict("durable agent attempt differs from exact retry")


def _insert_attempt_event(
    connection: Any,
    attempt: AgentAttempt,
    kind: RunEventKind,
    *,
    command: AgentAttemptCancellation | None = None,
    replacement_attempt_id: AgentAttemptId | None = None,
) -> None:
    run = load_run(connection, attempt.run_id)
    sequence = run.last_event_sequence + 1
    payload = b"" if command is None else command.command_id.encode("utf-8")
    event = RunEvent(
        attempt.run_id,
        attempt.workflow_revision_hash,
        sequence,
        attempt.node_id,
        attempt.node_execution_id,
        kind,
        payload,
        agent_attempt_id=attempt.attempt_id.value,
        attempt_ordinal=attempt.attempt_ordinal,
        cancellation_command_id=(None if command is None else command.command_id),
        replacement=None if command is None else command.replacement.value,
        cancellation_disposition=(
            None
            if command is None or command.disposition is None
            else command.disposition.value
        ),
        replacement_attempt_id=(
            None if replacement_attempt_id is None else replacement_attempt_id.value
        ),
    )
    updated = connection.execute(
        runs.update()
        .where(
            runs.c.run_id == attempt.run_id.value,
            runs.c.revision_hash == attempt.workflow_revision_hash.value,
            runs.c.current_node_id == attempt.node_id,
            runs.c.state == RunState.STARTED.value,
            runs.c.state_version == run.state_version,
            runs.c.last_event_sequence == run.last_event_sequence,
        )
        .values(
            state_version=run.state_version + 1,
            last_event_sequence=sequence,
        )
    )
    if updated.rowcount != 1:
        raise RunTransitionConflict("agent attempt event lost the run-head CAS")
    _insert_event(connection, event)


class DbosAgentAttemptStore:
    def __init__(self, engine: Engine, application_version: str | None = None) -> None:
        self._engine = engine
        self._application_version = application_version

    def prepare(self, execution: AgentAttemptExecution) -> AgentAttempt:
        request = execution.request
        prepared = _prepared_attempt(execution)
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(connection, request)
            if (
                run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
                or execution.ordinal != 1
            ):
                existing = _load_attempt(connection, prepared.attempt_id)
                _require_attempt_binding(existing, execution)
                return existing
            connection.execute(
                agent_attempts.insert()
                .prefix_with("OR IGNORE")
                .values(_attempt_values(prepared))
            )
            durable = _load_attempt(connection, prepared.attempt_id)
            _require_attempt_binding(durable, execution)
            return durable

    def bind_watchdog(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt:
        with canonical_write_transaction(self._engine) as connection:
            _validate_request(connection, execution.request)
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if durable.process_phase is AgentAttemptProcessPhase.WATCHDOG_READY:
                if (
                    durable.process_owner_id != process_owner_id
                    or durable.watchdog_generation_id != watchdog_generation_id
                ):
                    raise RunTransitionConflict(
                        "watchdog retry differs from durable generation"
                    )
                return durable
            if (
                durable.state is not AgentAttemptState.PREPARED
                or durable.process_phase is not AgentAttemptProcessPhase.NONE
            ):
                raise RunTransitionConflict(
                    "only an unbound prepared attempt can bind a watchdog"
                )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == durable.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.PREPARED.value,
                    agent_attempts.c.state_version == durable.state_version,
                    agent_attempts.c.process_phase
                    == AgentAttemptProcessPhase.NONE.value,
                )
                .values(
                    state_version=durable.state_version + 1,
                    process_phase=AgentAttemptProcessPhase.WATCHDOG_READY.value,
                    process_owner_id=process_owner_id.value,
                    watchdog_generation_id=watchdog_generation_id.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("watchdog binding lost its attempt CAS")
            return _load_attempt(connection, durable.attempt_id)

    def claim(self, execution: AgentAttemptExecution) -> AgentAttemptClaimResult:
        request = execution.request
        attempt_id = execution.attempt_id
        with canonical_write_transaction(self._engine) as connection:
            run, graph = _validate_request(connection, request)
            durable = _load_attempt(connection, attempt_id)
            _require_attempt_binding(durable, execution)
            if durable.state is AgentAttemptState.PREPARED:
                if (
                    run.state is not RunState.STARTED
                    or run.current_node_id != request.node_id
                ):
                    raise RunTransitionConflict(
                        "prepared attempt no longer owns current node"
                    )
                updated = connection.execute(
                    agent_attempts.update()
                    .where(
                        agent_attempts.c.attempt_id == attempt_id.value,
                        agent_attempts.c.state == AgentAttemptState.PREPARED.value,
                        agent_attempts.c.state_version == durable.state_version,
                    )
                    .values(
                        state=AgentAttemptState.LAUNCH_ARMED.value,
                        state_version=durable.state_version + 1,
                        process_phase=(
                            AgentAttemptProcessPhase.LAUNCH_AUTHORIZED.value
                            if durable.process_phase
                            is AgentAttemptProcessPhase.WATCHDOG_READY
                            else AgentAttemptProcessPhase.NONE.value
                        ),
                    )
                )
                durable = _load_attempt(connection, attempt_id)
                if updated.rowcount == 1:
                    return AgentAttemptClaimedByThisCall(durable)
            if durable.state is AgentAttemptState.LAUNCH_ARMED:
                return AgentAttemptPossiblyRan(durable)
            if durable.state is AgentAttemptState.FAILED:
                return AgentAttemptFailed(durable)
            if durable.state in {
                AgentAttemptState.CANCEL_REQUESTED,
                AgentAttemptState.CANCELLED,
                AgentAttemptState.INTERRUPTED,
            }:
                return AgentAttemptPossiblyRan(durable)
            if durable.state is AgentAttemptState.SUCCEEDED:
                successor = graph.successor(request.node_id).id
                if (
                    run.state is not RunState.STARTED
                    or run.current_node_id != successor
                ):
                    raise RunTransitionConflict(
                        "successful attempt has no exact successor transition"
                    )
                return AgentAttemptSucceeded(durable, successor)
            raise AssertionError("closed agent attempt state was not exhaustive")

    def observe_process(
        self,
        execution: AgentAttemptExecution,
        process_owner_id: AgentProcessOwnerId,
        watchdog_generation_id: WatchdogGenerationId,
    ) -> AgentAttempt:
        with canonical_write_transaction(self._engine) as connection:
            _validate_request(connection, execution.request)
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if durable.process_phase is AgentAttemptProcessPhase.PROCESS_OBSERVED:
                if (
                    durable.process_owner_id != process_owner_id
                    or durable.watchdog_generation_id != watchdog_generation_id
                ):
                    raise RunTransitionConflict(
                        "observed process retry differs from durable generation"
                    )
                return durable
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == durable.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.LAUNCH_ARMED.value,
                    agent_attempts.c.state_version == durable.state_version,
                    agent_attempts.c.process_phase
                    == AgentAttemptProcessPhase.LAUNCH_AUTHORIZED.value,
                    agent_attempts.c.process_owner_id == process_owner_id.value,
                    agent_attempts.c.watchdog_generation_id
                    == watchdog_generation_id.value,
                )
                .values(
                    state_version=durable.state_version + 1,
                    process_phase=AgentAttemptProcessPhase.PROCESS_OBSERVED.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("process observation lost its attempt CAS")
            return _load_attempt(connection, durable.attempt_id)

    def load(self, attempt_id: AgentAttemptId) -> AgentAttempt:
        with self._engine.connect() as connection:
            return _load_attempt(connection, attempt_id)

    def complete_success(
        self, execution: AgentAttemptExecution, result: AgentExecutionResult
    ) -> AgentAttemptSucceeded:
        request = execution.request
        attempt_id = execution.attempt_id
        with canonical_write_transaction(self._engine) as connection:
            run, graph = _validate_request(connection, request)
            durable = _load_attempt(connection, attempt_id)
            _require_attempt_binding(durable, execution)
            if (
                durable.state is not AgentAttemptState.LAUNCH_ARMED
                or run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
            ):
                raise RunTransitionConflict(
                    "only the armed current attempt can succeed"
                )
            receipt = AgentReceiptV2.for_execution(
                request, run.binding_set_hash, result
            )
            connection.execute(
                agent_receipts_v2.insert()
                .prefix_with("OR IGNORE")
                .values(_agent_receipt_v2_values(receipt))
            )
            receipt_record = (
                connection.execute(
                    sa.select(agent_receipts_v2).where(
                        agent_receipts_v2.c.node_execution_id
                        == request.node_execution_id.value
                    )
                )
                .mappings()
                .one()
            )
            if _agent_receipt_v2_from_record(receipt_record) != receipt:
                raise AgentReceiptConflict(
                    "durable V2 agent receipt differs from exact result"
                )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.LAUNCH_ARMED.value,
                    agent_attempts.c.state_version == durable.state_version,
                )
                .values(
                    state=AgentAttemptState.SUCCEEDED.value,
                    state_version=durable.state_version + 1,
                    receipt_hash=receipt.receipt_hash.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("agent success lost its attempt CAS")
            durable_success = _load_attempt(connection, attempt_id)
            successor = graph.successor(request.node_id).id
            _commit_event(
                connection,
                request.run_id,
                request.workflow_revision_hash,
                request.node_id,
                RunEventKind.AGENT_COMPLETED,
                result.output_bytes,
                RunState.STARTED,
                RunState.STARTED,
                successor,
                agent_attempt_id=attempt_id,
                attempt_ordinal=execution.ordinal,
            )
            return AgentAttemptSucceeded(durable_success, successor)

    def complete_known_failure(
        self, execution: AgentAttemptExecution
    ) -> AgentAttemptFailed:
        request = execution.request
        attempt_id = execution.attempt_id
        failure = AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(connection, request)
            durable = _load_attempt(connection, attempt_id)
            _require_attempt_binding(durable, execution)
            if (
                durable.state is not AgentAttemptState.LAUNCH_ARMED
                or run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
            ):
                raise RunTransitionConflict("only the armed current attempt can fail")
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.LAUNCH_ARMED.value,
                    agent_attempts.c.state_version == durable.state_version,
                )
                .values(
                    state=AgentAttemptState.FAILED.value,
                    state_version=durable.state_version + 1,
                    failure_code=failure.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("agent failure lost its attempt CAS")
            durable_failure = _load_attempt(connection, attempt_id)
            _commit_event(
                connection,
                request.run_id,
                request.workflow_revision_hash,
                request.node_id,
                RunEventKind.AGENT_FAILED,
                failure.value.encode("ascii"),
                RunState.STARTED,
                RunState.STARTED,
                request.node_id,
                agent_attempt_id=attempt_id,
                attempt_ordinal=execution.ordinal,
            )
            return AgentAttemptFailed(durable_failure)

    def request_cancellation(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttemptCancellationResult:
        from atelier2.adapters.dbos.workflow import QUEUE_NAME

        client: DBOSClient | None = None
        try:
            with canonical_write_transaction(self._engine) as connection:
                run_record = connection.scalar(
                    sa.select(runs.c.run_id).where(
                        runs.c.run_id == request.run_id.value
                    )
                )
                if run_record is None:
                    return AgentAttemptCancellationRunMissing()
                record = (
                    connection.execute(
                        sa.select(agent_attempts).where(
                            agent_attempts.c.attempt_id == request.attempt_id.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return AgentAttemptCancellationTargetMissing()
                attempt = _attempt_from_record(record)
                if attempt.run_id != request.run_id:
                    return AgentAttemptCancellationTargetMissing()
                existing = attempt.cancellation
                if existing is not None:
                    if not existing.matches(request):
                        return AgentAttemptCancellationCommandConflict()
                    return AgentAttemptCancellationAccepted(
                        attempt,
                        attempt.state
                        in {
                            AgentAttemptState.CANCELLED,
                            AgentAttemptState.INTERRUPTED,
                        },
                        self._replacement_attempt_id(connection, attempt),
                    )
                if attempt.state in {
                    AgentAttemptState.SUCCEEDED,
                    AgentAttemptState.FAILED,
                    AgentAttemptState.CANCELLED,
                    AgentAttemptState.INTERRUPTED,
                }:
                    return AgentAttemptCancellationTerminalConflict()
                if attempt.state_version != request.expected_attempt_state_version:
                    return AgentAttemptCancellationStale()
                if request.replacement is AgentAttemptReplacement.ONE and (
                    attempt.attempt_ordinal != 1
                ):
                    return AgentAttemptReplacementNotAllowed()
                current_ordinal = connection.scalar(
                    sa.select(sa.func.max(agent_attempts.c.attempt_ordinal)).where(
                        agent_attempts.c.node_execution_id
                        == attempt.node_execution_id.value
                    )
                )
                run = load_run(connection, request.run_id)
                if (
                    run.state is not RunState.STARTED
                    or run.current_node_id != attempt.node_id
                    or int(current_ordinal or 0) != attempt.attempt_ordinal
                ):
                    return AgentAttemptCancellationNotCurrent()
                workflow_id = request.workflow_id
                updated = connection.execute(
                    agent_attempts.update()
                    .where(
                        agent_attempts.c.attempt_id == attempt.attempt_id.value,
                        agent_attempts.c.state == attempt.state.value,
                        agent_attempts.c.state_version == attempt.state_version,
                        agent_attempts.c.cancellation_command_id.is_(None),
                    )
                    .values(
                        state=AgentAttemptState.CANCEL_REQUESTED.value,
                        state_version=attempt.state_version + 1,
                        cancellation_command_id=request.command_id,
                        cancellation_expected_state_version=(
                            request.expected_attempt_state_version
                        ),
                        replacement=request.replacement.value,
                        redrive_state=AgentAttemptRedriveState.PENDING.value,
                        cancellation_workflow_id=workflow_id,
                    )
                )
                if updated.rowcount != 1:
                    return AgentAttemptCancellationStale()
                accepted = _load_attempt(connection, attempt.attempt_id)
                _insert_attempt_event(
                    connection,
                    accepted,
                    RunEventKind.AGENT_CANCEL_REQUESTED,
                    command=accepted.cancellation,
                )
                if self._application_version is None:
                    raise RunTransitionConflict(
                        "cancellation submission requires the runtime application version"
                    )
                client = DBOSClient(
                    system_database_engine=self._engine, use_listen_notify=False
                )
                options: EnqueueOptions = {
                    "workflow_name": CANCELLATION_WORKFLOW_NAME,
                    "queue_name": QUEUE_NAME,
                    "workflow_id": workflow_id,
                    "app_version": self._application_version,
                }
                client.enqueue_in_transaction(
                    connection,
                    options,
                    attempt.run_id.value,
                    attempt.attempt_id.value,
                    request.command_id,
                )
                return AgentAttemptCancellationAccepted(accepted, False)
        finally:
            if client is not None:
                client.destroy()

    def attest_cancellation_cleanup(
        self,
        request: CancelAgentAttemptRequest,
        disposition: AgentAttemptCancellationDisposition,
        process_owner_id: AgentProcessOwnerId | None,
        watchdog_generation_id: WatchdogGenerationId | None,
    ) -> AgentAttemptCancellationAccepted:
        from atelier2.adapters.dbos.workflow import QUEUE_NAME

        with canonical_write_transaction(self._engine) as connection:
            attempt = _load_attempt(connection, request.attempt_id)
            cancellation = attempt.cancellation
            if cancellation is None or not cancellation.matches(request):
                raise RunTransitionConflict(
                    "cleanup attestation differs from its cancellation command"
                )
            if attempt.state in {
                AgentAttemptState.CANCELLED,
                AgentAttemptState.INTERRUPTED,
            }:
                if cancellation.disposition is not disposition:
                    raise RunTransitionConflict(
                        "cleanup retry differs from durable disposition"
                    )
                return AgentAttemptCancellationAccepted(
                    attempt,
                    True,
                    self._replacement_attempt_id(connection, attempt),
                )
            if attempt.state is not AgentAttemptState.CANCEL_REQUESTED:
                raise RunTransitionConflict("only a requested cancellation can attest")
            if (
                attempt.process_owner_id != process_owner_id
                or attempt.watchdog_generation_id != watchdog_generation_id
            ):
                raise RunTransitionConflict(
                    "cleanup attestation differs from durable owner generation"
                )
            terminal_state = (
                AgentAttemptState.INTERRUPTED
                if disposition
                is AgentAttemptCancellationDisposition.OWNER_LOST_AFTER_PARENT_DEATH
                else AgentAttemptState.CANCELLED
            )
            terminal_cancellation = AgentAttemptCancellation(
                cancellation.command_id,
                cancellation.expected_attempt_state_version,
                cancellation.replacement,
                AgentAttemptRedriveState.CLEANUP_ATTESTED,
                disposition,
            )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == attempt.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.CANCEL_REQUESTED.value,
                    agent_attempts.c.state_version == attempt.state_version,
                    agent_attempts.c.cancellation_command_id == request.command_id,
                )
                .values(
                    state=terminal_state.value,
                    state_version=attempt.state_version + 1,
                    process_phase=AgentAttemptProcessPhase.CLEANUP_ATTESTED.value,
                    process_owner_id=(
                        None if process_owner_id is None else process_owner_id.value
                    ),
                    watchdog_generation_id=(
                        None
                        if watchdog_generation_id is None
                        else watchdog_generation_id.value
                    ),
                    redrive_state=AgentAttemptRedriveState.CLEANUP_ATTESTED.value,
                    cancellation_disposition=disposition.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("cleanup attestation lost its attempt CAS")
            terminal = _load_attempt(connection, attempt.attempt_id)
            replacement_attempt_id = None
            if cancellation.replacement is AgentAttemptReplacement.ONE:
                replacement_attempt_id = AgentAttemptId.for_execution(
                    attempt.node_execution_id, attempt.request_hash, 2
                )
                replacement = AgentAttempt(
                    replacement_attempt_id,
                    attempt.node_execution_id,
                    attempt.request_hash,
                    attempt.executor_operational_identity,
                    attempt.run_id,
                    attempt.workflow_revision_hash,
                    attempt.node_id,
                    2,
                    AgentAttemptState.PREPARED,
                    0,
                )
                connection.execute(
                    agent_attempts.insert().values(_attempt_values(replacement))
                )
                if self._application_version is None:
                    raise RunTransitionConflict(
                        "replacement submission requires the runtime application version"
                    )
                client = DBOSClient(
                    system_database_engine=self._engine, use_listen_notify=False
                )
                try:
                    options: EnqueueOptions = {
                        "workflow_name": REPLACEMENT_WORKFLOW_NAME,
                        "queue_name": QUEUE_NAME,
                        "workflow_id": (
                            "atelier2-agent-replacement-" + replacement_attempt_id.value
                        ),
                        "app_version": self._application_version,
                    }
                    client.enqueue_in_transaction(
                        connection, options, replacement_attempt_id.value
                    )
                finally:
                    client.destroy()
            _insert_attempt_event(
                connection,
                terminal,
                (
                    RunEventKind.AGENT_INTERRUPTED
                    if terminal_state is AgentAttemptState.INTERRUPTED
                    else RunEventKind.AGENT_CANCELLED
                ),
                command=terminal_cancellation,
                replacement_attempt_id=replacement_attempt_id,
            )
            return AgentAttemptCancellationAccepted(
                terminal, True, replacement_attempt_id
            )

    def mark_cancellation_owner_not_local(
        self, request: CancelAgentAttemptRequest
    ) -> AgentAttempt:
        with canonical_write_transaction(self._engine) as connection:
            attempt = _load_attempt(connection, request.attempt_id)
            cancellation = attempt.cancellation
            if cancellation is None or not cancellation.matches(request):
                raise RunTransitionConflict(
                    "owner redrive differs from its cancellation command"
                )
            if cancellation.redrive_state is AgentAttemptRedriveState.OWNER_NOT_LOCAL:
                return attempt
            if (
                attempt.state is not AgentAttemptState.CANCEL_REQUESTED
                or cancellation.redrive_state is not AgentAttemptRedriveState.PENDING
            ):
                raise RunTransitionConflict(
                    "only a pending cancellation can lose its local owner"
                )
            updated = connection.execute(
                agent_attempts.update()
                .where(
                    agent_attempts.c.attempt_id == attempt.attempt_id.value,
                    agent_attempts.c.state == AgentAttemptState.CANCEL_REQUESTED.value,
                    agent_attempts.c.state_version == attempt.state_version,
                    agent_attempts.c.redrive_state
                    == AgentAttemptRedriveState.PENDING.value,
                )
                .values(
                    state_version=attempt.state_version + 1,
                    redrive_state=AgentAttemptRedriveState.OWNER_NOT_LOCAL.value,
                )
            )
            if updated.rowcount != 1:
                raise RunTransitionConflict("owner redrive lost its attempt CAS")
            return _load_attempt(connection, attempt.attempt_id)

    @staticmethod
    def _replacement_attempt_id(
        connection: Any, attempt: AgentAttempt
    ) -> AgentAttemptId | None:
        if (
            attempt.cancellation is None
            or attempt.cancellation.replacement is AgentAttemptReplacement.NONE
        ):
            return None
        value = connection.scalar(
            sa.select(agent_attempts.c.attempt_id).where(
                agent_attempts.c.node_execution_id == attempt.node_execution_id.value,
                agent_attempts.c.attempt_ordinal == 2,
            )
        )
        return None if value is None else AgentAttemptId(str(value))
