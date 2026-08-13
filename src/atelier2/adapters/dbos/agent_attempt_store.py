from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from atelier2.adapters.dbos.run_store import (
    AgentReceiptConflict,
    RunTransitionConflict,
    _agent_receipt_v2_from_record,
    _agent_receipt_v2_values,
    _commit_event,
    load_graph,
    load_run,
)
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    agent_receipts_v2,
)
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.agent_attempts import (
    AGENT_ATTEMPT_ORDINAL,
    AgentAttempt,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptState,
)
from atelier2.contracts.agents import (
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentReceiptHash,
    AgentReceiptV2,
)
from atelier2.contracts.executions import NodeExecutionId, RunEventKind
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import RunId, RunState, WorkflowRevisionHash
from atelier2.contracts.workflows import AgentNodeV2, WorkflowGraphV2
from atelier2.ports.agent_attempts import (
    AgentAttemptClaimedByThisCall,
    AgentAttemptClaimResult,
    AgentAttemptFailed,
    AgentAttemptPossiblyRan,
    AgentAttemptSucceeded,
)


def _attempt_from_record(record: Mapping[Any, Any]) -> AgentAttempt:
    try:
        failure = record["failure_code"]
        receipt = record["receipt_hash"]
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
        "failure_code": (
            None if attempt.failure_code is None else attempt.failure_code.value
        ),
        "receipt_hash": (
            None if attempt.receipt_hash is None else attempt.receipt_hash.value
        ),
    }


def _prepared_attempt(request: AgentExecutionRequestV2) -> AgentAttempt:
    return AgentAttempt(
        AgentAttemptId.for_execution(request.node_execution_id, request.request_hash),
        request.node_execution_id,
        request.request_hash,
        request.executor_operational_identity,
        request.run_id,
        request.workflow_revision_hash,
        request.node_id,
        AGENT_ATTEMPT_ORDINAL,
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
    attempt: AgentAttempt, request: AgentExecutionRequestV2
) -> None:
    if (
        attempt.attempt_id
        != AgentAttemptId.for_execution(request.node_execution_id, request.request_hash)
        or attempt.node_execution_id != request.node_execution_id
        or attempt.request_hash != request.request_hash
        or attempt.executor_operational_identity
        != request.executor_operational_identity
        or attempt.run_id != request.run_id
        or attempt.workflow_revision_hash != request.workflow_revision_hash
        or attempt.node_id != request.node_id
        or attempt.attempt_ordinal != AGENT_ATTEMPT_ORDINAL
    ):
        raise RunTransitionConflict("durable agent attempt differs from exact retry")


class DbosAgentAttemptStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def prepare(self, request: AgentExecutionRequestV2) -> AgentAttempt:
        prepared = _prepared_attempt(request)
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(connection, request)
            if (
                run.state is not RunState.STARTED
                or run.current_node_id != request.node_id
            ):
                existing = _load_attempt(connection, prepared.attempt_id)
                _require_attempt_binding(existing, request)
                return existing
            connection.execute(
                agent_attempts.insert()
                .prefix_with("OR IGNORE")
                .values(_attempt_values(prepared))
            )
            durable = _load_attempt(connection, prepared.attempt_id)
            _require_attempt_binding(durable, request)
            return durable

    def claim(self, request: AgentExecutionRequestV2) -> AgentAttemptClaimResult:
        attempt_id = AgentAttemptId.for_execution(
            request.node_execution_id, request.request_hash
        )
        with canonical_write_transaction(self._engine) as connection:
            run, graph = _validate_request(connection, request)
            durable = _load_attempt(connection, attempt_id)
            _require_attempt_binding(durable, request)
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
                        agent_attempts.c.state_version == 0,
                    )
                    .values(
                        state=AgentAttemptState.LAUNCH_ARMED.value,
                        state_version=1,
                    )
                )
                durable = _load_attempt(connection, attempt_id)
                if updated.rowcount == 1:
                    return AgentAttemptClaimedByThisCall(durable)
            if durable.state is AgentAttemptState.LAUNCH_ARMED:
                return AgentAttemptPossiblyRan(durable)
            if durable.state is AgentAttemptState.FAILED:
                return AgentAttemptFailed(durable)
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

    def complete_success(
        self, request: AgentExecutionRequestV2, result: AgentExecutionResult
    ) -> AgentAttemptSucceeded:
        attempt_id = AgentAttemptId.for_execution(
            request.node_execution_id, request.request_hash
        )
        with canonical_write_transaction(self._engine) as connection:
            run, graph = _validate_request(connection, request)
            durable = _load_attempt(connection, attempt_id)
            _require_attempt_binding(durable, request)
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
                    agent_attempts.c.state_version == 1,
                )
                .values(
                    state=AgentAttemptState.SUCCEEDED.value,
                    state_version=2,
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
            )
            return AgentAttemptSucceeded(durable_success, successor)

    def complete_known_failure(
        self, request: AgentExecutionRequestV2
    ) -> AgentAttemptFailed:
        attempt_id = AgentAttemptId.for_execution(
            request.node_execution_id, request.request_hash
        )
        failure = AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
        with canonical_write_transaction(self._engine) as connection:
            run, _graph = _validate_request(connection, request)
            durable = _load_attempt(connection, attempt_id)
            _require_attempt_binding(durable, request)
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
                    agent_attempts.c.state_version == 1,
                )
                .values(
                    state=AgentAttemptState.FAILED.value,
                    state_version=2,
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
            )
            return AgentAttemptFailed(durable_failure)
