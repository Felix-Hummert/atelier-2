from __future__ import annotations

from collections.abc import Mapping
from typing import Any, assert_never

import sqlalchemy as sa
from dbos import DBOSClient, EnqueueOptions
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.node_records import keep_node_receipt
from atelier2.adapters.dbos.run_store import (
    AgentReceiptConflict,
    RunTransitionConflict,
    ToolRedemptionConflict,
    _agent_receipt_v2_from_record,
    _agent_receipt_v2_values,
    _commit_event,
    _insert_event,
    _tool_redemption_from_record,
    _tool_redemption_values,
    load_graph,
    load_node_outputs,
    load_run,
    load_run_inputs,
    why_a_value_its_declared_schema_refuses,
)
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
    runs,
    tool_redemptions,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.application.compose_node_job import node_job
from atelier2.contracts.agent_attempts import (
    TERMINAL_AGENT_ATTEMPT_STATES,
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
    driving_workflow_id,
    replacement_workflow_id_for,
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
from atelier2.contracts.node_records_v3 import (
    NodeArtifact,
    NodeReceiptReason,
    PersistedReceiptDisposition,
    node_receipt_reason,
)
from atelier2.contracts.revisions_v3 import PublishedRevisionHash
from atelier2.contracts.run_bindings import RunV2, RunV3
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.tool_grants_v3 import ToolRedemptionReceipt
from atelier2.contracts.workflows import (
    AgentNodeV2,
    NodeCompletion,
    RunCompletes,
    RunContinues,
    WorkflowGraphV2,
    completion_after_node,
)
from atelier2.contracts.workflows_v3 import AgentNodeV3, WorkflowGraphV3
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

# DBOS owns this table and these tokens; the store only reads them, and only to
# answer whether a workflow it enqueued itself is still going to run. Reaching
# for the whole DBOS model to ask one question would bind far more of the
# runtime's internals than the answer is worth.
_dbos_workflow_status = sa.table(
    "workflow_status", sa.column("workflow_uuid"), sa.column("status")
)
_DRIVING_WORKFLOW_STATUSES = ("PENDING", "ENQUEUED", "DELAYED")
"""The DBOS statuses under which a workflow is still owed its next step."""


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
) -> tuple[RunV2 | RunV3, WorkflowGraphV2 | WorkflowGraphV3]:
    """The run and graph one attempt request must exactly describe.

    A V3 agent node runs here too. Its role binding, its attempt identity and its
    provider contract are the ones V2 already carries, so admitting it is a wider
    door rather than a second path -- what a V3 run does differently begins after
    the provider answers, where the receipt chain is written.

    Only bound runs pass: a V1 run has no role matrix to attempt against. The
    graph is what decides how a node is read, and the run head is what decides
    where the run stands, so neither format is asked to answer for the other.
    """
    run = load_run(session, request.run_id)
    graph = load_graph(session, request.workflow_revision_hash)
    if not isinstance(run, (RunV2, RunV3)) or not isinstance(
        graph, (WorkflowGraphV2, WorkflowGraphV3)
    ):
        raise RunTransitionConflict("agent attempt requires a bound run")
    if run.revision_hash != request.workflow_revision_hash:
        raise RunTransitionConflict("agent attempt request names another revision")
    node = graph.node(request.node_id)
    if not isinstance(node, (AgentNodeV2, AgentNodeV3)):
        raise RunTransitionConflict("agent attempt request differs from durable graph")
    # Recomputed from durable truth rather than trusted: what the node's author
    # wrote, plus the orders this run was started with, through the one owner
    # that decides what an agent is handed. A second spelling here would let a
    # request claim a job the document and the run never agreed on.
    authored_job = (
        node.job
        if isinstance(node, AgentNodeV2)
        else node_job(
            node.instruction,
            load_run_inputs(session, request.run_id, node),
            load_node_outputs(
                session, request.run_id, request.workflow_revision_hash, graph, node
            ),
        )
    ).encode("utf-8")
    if (
        node.role != request.resolved_binding.role.value
        or authored_job != request.job_bytes
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


def _require_completed_attempt_head(
    run: RunV2 | RunV3,
    request: AgentExecutionRequestV2,
    completion: NodeCompletion,
) -> None:
    match completion:
        case RunContinues(node_id):
            if run.state is not RunState.STARTED or run.current_node_id != node_id:
                raise RunTransitionConflict(
                    "successful attempt has no exact successor transition"
                )
        case RunCompletes():
            if (
                run.state is not RunState.COMPLETED
                or run.current_node_id != request.node_id
            ):
                raise RunTransitionConflict(
                    "successful terminal attempt has no exact completed transition"
                )
        case _ as unreachable:
            assert_never(unreachable)


def _fail_current_attempt(
    connection: Any,
    execution: AgentAttemptExecution,
    durable: AgentAttempt,
    failure: AgentAttemptFailureCode,
    receipt_reason: str | None = None,
) -> AgentAttemptFailed:
    """One durable failure seam for every way an armed attempt ends badly.

    The attempt turns `FAILED` under its named code, the `AGENT_FAILED` event
    carries that code, and the run stays `STARTED` on the node -- the exact
    shape a process failure has always left behind, so a reader needs one
    vocabulary for both. `receipt_reason` is the schema owner's verdict where
    one judged; a process that died produced nothing anybody judged, so it
    writes no node receipt here -- that absence is a named gap on #295.
    """
    request = execution.request
    attempt_id = execution.attempt_id
    if receipt_reason is not None:
        keep_node_receipt(
            connection,
            request.node_execution_id,
            PersistedReceiptDisposition.FAILED,
            receipt_reason,
        )
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


def _keep_tool_redemption(
    connection: Any,
    execution: AgentAttemptExecution,
    redemption: ToolRedemptionReceipt | None,
) -> None:
    """Keep what this attempt's grant redeemed, inside the write that succeeds it.

    A retry of the same durable attempt runs the verification again, and the row
    that is already there decides: identical evidence is the same redemption
    written twice, and different evidence is two answers about one node
    execution, which is a contradiction rather than a second receipt.
    """
    if redemption is None:
        return
    if (
        redemption.node_execution_id != execution.request.node_execution_id
        or redemption.attempt_id != execution.attempt_id
    ):
        raise ToolRedemptionConflict("tool redemption differs from its exact attempt")
    connection.execute(
        tool_redemptions.insert()
        .prefix_with("OR IGNORE")
        .values(_tool_redemption_values(redemption))
    )
    stored = (
        connection.execute(
            sa.select(tool_redemptions).where(
                tool_redemptions.c.node_execution_id
                == redemption.node_execution_id.value
            )
        )
        .mappings()
        .one()
    )
    if _tool_redemption_from_record(stored) != redemption:
        raise ToolRedemptionConflict(
            "durable tool redemption differs from exact redemption"
        )


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
                completion = completion_after_node(graph, request.node_id)
                _require_completed_attempt_head(run, request, completion)
                return AgentAttemptSucceeded(durable, completion)
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

    def driverless_attempts(self) -> tuple[AgentAttempt, ...]:
        """Ask only after the durable runtime is launched.

        Before the launch, the workflow table this reads is either absent or
        still holds the statuses of the process that died, so every answer it
        could give would be about a machine that is not running yet.
        """

        with self._engine.connect() as connection:
            nonterminal = tuple(
                _attempt_from_record(record)
                for record in connection.execute(
                    sa.select(agent_attempts)
                    .where(
                        agent_attempts.c.state.not_in(
                            tuple(
                                state.value for state in TERMINAL_AGENT_ATTEMPT_STATES
                            )
                        )
                    )
                    .order_by(agent_attempts.c.attempt_id)
                ).mappings()
            )
            if not nonterminal:
                return ()
            drivers = {attempt: driving_workflow_id(attempt) for attempt in nonterminal}
            driving = set(
                connection.scalars(
                    sa.select(_dbos_workflow_status.c.workflow_uuid).where(
                        _dbos_workflow_status.c.workflow_uuid.in_(
                            tuple(drivers.values())
                        ),
                        _dbos_workflow_status.c.status.in_(_DRIVING_WORKFLOW_STATUSES),
                    )
                )
            )
        return tuple(
            attempt for attempt, driver in drivers.items() if driver not in driving
        )

    def complete_success(
        self,
        execution: AgentAttemptExecution,
        result: AgentExecutionResult,
        redemption: ToolRedemptionReceipt | None = None,
    ) -> AgentAttemptSucceeded | AgentAttemptFailed:
        """Write the one success this attempt is allowed, or its named refusal.

        A V3 node's declared output is what its author promised, so the exact
        decoded bytes are read against the schema it pins before anything here is
        written. Bytes their own schema refuses leave no agent receipt, no
        `AGENT_COMPLETED` event and no advanced run -- a success nobody may take
        back must not be written for an answer this product cannot honour. What
        the refusal leaves instead is its durable name: a `failed`
        `node-receipt/v3` carrying the schema owner's words, and the attempt on
        the same failure seam `PROCESS_EXITED_UNSUCCESSFULLY` runs on today, so
        the driver ends named instead of dying on an exception nobody stored.

        A V3 success additionally keeps what the run now knows durably: the
        produced value as `node-artifact/v3` and the `succeeded`
        `node-receipt/v3` naming it, in this same transaction.
        """
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
            node = graph.node(request.node_id)
            if isinstance(node, AgentNodeV3):
                refusal = why_a_value_its_declared_schema_refuses(
                    connection, node.id, node.outputs[0], result.output_bytes
                )
                if refusal is not None:
                    return _fail_current_attempt(
                        connection,
                        execution,
                        durable,
                        AgentAttemptFailureCode.OUTPUT_SCHEMA_REFUSED,
                        node_receipt_reason(
                            NodeReceiptReason.OUTPUT_SCHEMA_REFUSED, refusal
                        ),
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
            _keep_tool_redemption(connection, execution, redemption)
            if isinstance(node, AgentNodeV3):
                declared = node.outputs[0]
                keep_node_receipt(
                    connection,
                    request.node_execution_id,
                    PersistedReceiptDisposition.SUCCEEDED,
                    node_receipt_reason(NodeReceiptReason.OUTPUT_ACCEPTED),
                    NodeArtifact(
                        request.run_id,
                        node.id,
                        request.node_execution_id,
                        declared.name,
                        PublishedRevisionHash(declared.schema_reference.revision),
                        result.output_bytes,
                    ),
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
            completion = completion_after_node(graph, request.node_id)
            match completion:
                case RunContinues(node_id):
                    target_state = RunState.STARTED
                    target_node_id = node_id
                    terminal = False
                case RunCompletes():
                    target_state = RunState.COMPLETED
                    target_node_id = request.node_id
                    terminal = True
                case _ as unreachable:
                    assert_never(unreachable)
            _commit_event(
                connection,
                request.run_id,
                request.workflow_revision_hash,
                request.node_id,
                RunEventKind.AGENT_COMPLETED,
                result.output_bytes,
                RunState.STARTED,
                target_state,
                target_node_id,
                terminal=terminal,
                agent_attempt_id=attempt_id,
                attempt_ordinal=execution.ordinal,
                agent_receipt_hash=receipt.receipt_hash,
            )
            return AgentAttemptSucceeded(durable_success, completion)

    def complete_known_failure(
        self, execution: AgentAttemptExecution
    ) -> AgentAttemptFailed:
        request = execution.request
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(connection, request)
            durable = _load_attempt(connection, execution.attempt_id)
            _require_attempt_binding(durable, execution)
            if (
                durable.state is not AgentAttemptState.LAUNCH_ARMED
                or run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
            ):
                raise RunTransitionConflict("only the armed current attempt can fail")
            return _fail_current_attempt(
                connection,
                execution,
                durable,
                AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
            )

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
                        "workflow_id": replacement_workflow_id_for(
                            replacement_attempt_id
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
