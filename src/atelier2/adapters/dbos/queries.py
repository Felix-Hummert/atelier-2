from __future__ import annotations

import math
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.effect_store import (
    command_snapshot_from_record,
    intent_snapshot_from_record,
    receipt_from_record,
)
from atelier2.adapters.dbos.run_store import (
    RunTransitionConflict,
    event_from_record,
    graph_from_document,
    run_from_record_with_bindings,
    validate_run_graph_binding,
)
from atelier2.adapters.dbos.schema import (
    agent_attempts,
    effect_intents,
    effect_receipts,
    reconcile_commands,
    run_events,
    runs,
    workflow_revisions,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.contracts.agent_attempts import (
    AgentAttemptCancellationDisposition,
    AgentAttemptFailureCode,
    AgentAttemptId,
    AgentAttemptRedriveState,
    AgentAttemptReplacement,
    AgentAttemptState,
)
from atelier2.contracts.agents import (
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
)
from atelier2.contracts.effects import (
    EffectIntentState,
    ReconcileCommandId,
    ReconcileCommandState,
)
from atelier2.contracts.executions import (
    NodeExecutionId,
    RunEventKind,
    logical_effect_key_for,
)
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.run_projections import public_agent_attempt_state
from atelier2.contracts.runs import (
    RevisionHashCollision,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
from atelier2.contracts.workflows import AgentNodeV2, WorkflowGraphV2
from atelier2.ports.run_events import (
    CursorAhead,
    EventHistoryCorrupt,
    PersistedRunEvent,
    PrepareRunEventStreamResult,
    ReadRunEventPageResult,
    RunEventPage,
    StreamReady,
)
from atelier2.ports.run_queries import (
    AgentAttemptCancellationProjection,
    AgentAttemptProjection,
    GetReconciliationRetryTargetResult,
    GetRunResult,
    ListRunsResult,
    ReconciliationRetryCommandConflict,
    ReconciliationRetryTargetFound,
    ReconciliationRetryTargetMissing,
    RunFound,
    RunPage,
    RunProjection,
    RunQueryMissing,
    WaitingReconciliationProjection,
)
from atelier2.ports.workflow_revisions import (
    DescribedWorkflowRevisionPage,
    DurableProjectionLimit,
    EnrichedPageBudget,
    GetWorkflowRevisionResult,
    ListDescribedWorkflowRevisionsResult,
    ListWorkflowRevisionsResult,
    ProjectionLimitExceeded,
    ProjectionTooLarge,
    QueryDurableStateCorrupt,
    ReadUnavailable,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
)

_LENGTH_LABEL_PREFIX = "_atelier_length_"
_MAXIMUM_UTF8_BYTES_PER_CHARACTER = 4
_RUN_PROJECTION_COLUMNS: tuple[sa.Column[Any], ...] = (
    runs.c.run_id,
    runs.c.revision_hash,
    runs.c.workflow_format_version,
    runs.c.agent_binding_set_hash,
    runs.c.current_node_id,
    runs.c.state,
    runs.c.state_version,
    runs.c.last_event_sequence,
    runs.c.terminal_hash,
)
_RUN_FIELD_COLUMNS = frozenset(("run_id", "current_node_id"))
_REVISION_DOCUMENT_COLUMNS = frozenset(("document",))
_INTENT_PAYLOAD_COLUMNS = frozenset(("canonical_request",))
_INTENT_FIELD_COLUMNS = frozenset(
    (
        "logical_key",
        "run_id",
        "adapter_revision",
        "destination_identity",
        "adapter_operational_identity",
        "reconciliation_owner_command_id",
    )
)
_COMMAND_PAYLOAD_COLUMNS = frozenset(("found_result",))
_COMMAND_FIELD_COLUMNS = frozenset(
    ("command_id", "logical_key", "actor", "evidence", "found_effect_id")
)
_EVENT_PAYLOAD_COLUMNS = frozenset(("payload",))
_EVENT_FIELD_COLUMNS = frozenset(("run_id", "node_id", "receipt_logical_key"))
_ATTEMPT_FIELD_COLUMNS = frozenset(
    ("executor_operational_identity", "run_id", "node_id")
)
_RECEIPT_PAYLOAD_COLUMNS = frozenset(("canonical_request", "result"))
_RECEIPT_FIELD_COLUMNS = frozenset(
    (
        "logical_key",
        "run_id",
        "adapter_revision",
        "destination_identity",
        "adapter_operational_identity",
        "effect_id",
        "reconcile_command_id",
    )
)


def _bounded_projection_select(
    table: sa.Table,
    projection_limit: DurableProjectionLimit | None,
    *,
    columns: Sequence[sa.Column[Any]] | None = None,
    document_columns: frozenset[str] = frozenset(),
    payload_columns: frozenset[str] = frozenset(),
    field_columns: frozenset[str] = frozenset(),
) -> sa.Select[Any]:
    selected_columns = tuple(table.c) if columns is None else columns
    if projection_limit is None:
        return sa.select(*selected_columns)
    projected: list[Any] = []
    for column in selected_columns:
        if column.name in document_columns:
            maximum = projection_limit.maximum_document_bytes
            length = sa.func.length(column)
        elif column.name in payload_columns:
            maximum = projection_limit.maximum_payload_bytes
            length = sa.func.length(column)
        elif column.name in field_columns:
            maximum = (
                _MAXIMUM_UTF8_BYTES_PER_CHARACTER
                * projection_limit.maximum_field_characters
            )
            length = sa.func.length(sa.cast(column, sa.LargeBinary()))
        else:
            projected.append(column)
            continue
        projected.append(
            sa.case((length <= maximum, column), else_=None).label(column.name)
        )
        projected.append(length.label(_LENGTH_LABEL_PREFIX + column.name))
    return sa.select(*projected)


def _validate_bounded_record(
    record: Mapping[Any, Any],
    projection_limit: DurableProjectionLimit | None,
    *,
    document_columns: frozenset[str] = frozenset(),
    payload_columns: frozenset[str] = frozenset(),
    field_columns: frozenset[str] = frozenset(),
) -> None:
    if projection_limit is None:
        return
    for column_name in document_columns:
        length = record[_LENGTH_LABEL_PREFIX + column_name]
        if length is not None:
            projection_limit.validate_document_length(int(length))
    for column_name in payload_columns:
        length = record[_LENGTH_LABEL_PREFIX + column_name]
        if length is not None:
            projection_limit.validate_payload_length(int(length))
    for column_name in field_columns:
        length = record[_LENGTH_LABEL_PREFIX + column_name]
        if length is None:
            continue
        value = record[column_name]
        if value is None:
            raise ProjectionLimitExceeded(
                "durable text exceeds its response allocation limit"
            )
        projection_limit.validate_field_length(len(str(value)))


def _durable_attempt_state(persisted_value: Any) -> AgentAttemptState:
    try:
        return AgentAttemptState(str(persisted_value))
    except ValueError as outside_vocabulary:
        raise RunTransitionConflict(
            "persisted agent attempt state is outside the durable vocabulary"
        ) from outside_vocabulary


def _current_attempt_projection(
    record: Mapping[Any, Any],
    *,
    run: RunV2,
    graph: WorkflowGraphV2,
) -> AgentAttemptProjection:
    node = graph.node(run.current_node_id)
    if not isinstance(node, AgentNodeV2):
        raise RunTransitionConflict("current attempt does not belong to a V2 agent")
    binding = next(
        (binding for binding in run.agent_bindings if binding.role.value == node.role),
        None,
    )
    if binding is None:
        raise RunTransitionConflict("current agent has no exact durable binding")
    operational_identity = AgentExecutorOperationalIdentity(
        str(record["executor_operational_identity"])
    )
    execution_id = NodeExecutionId.for_node(
        run.run_id, run.revision_hash, run.current_node_id
    )
    exact_request = AgentExecutionRequestV2(
        execution_id,
        run.run_id,
        run.revision_hash,
        run.current_node_id,
        binding,
        operational_identity,
        node.job.encode("utf-8"),
    )
    request_hash = AgentExecutionRequestHash(str(record["request_hash"]))
    attempt_id = AgentAttemptId(str(record["attempt_id"]))
    ordinal = int(record["attempt_ordinal"])
    if (
        ordinal not in (1, 2)
        or NodeExecutionId(str(record["node_execution_id"])) != execution_id
        or RunId(str(record["run_id"])) != run.run_id
        or WorkflowRevisionHash(str(record["workflow_revision_hash"]))
        != run.revision_hash
        or str(record["node_id"]) != run.current_node_id
        or request_hash != exact_request.request_hash
        or attempt_id
        != AgentAttemptId.for_execution(
            execution_id, exact_request.request_hash, ordinal
        )
    ):
        raise RunTransitionConflict("current agent attempt binding disagrees")
    durable_state = _durable_attempt_state(record["state"])
    public_state = public_agent_attempt_state(durable_state)
    if public_state is None:
        raise RunTransitionConflict(
            "successful current attempt has no atomic successor transition"
        )
    failure_value = record["failure_code"]
    receipt_value = record["receipt_hash"]
    state_version = int(record["state_version"])
    failure: AgentAttemptFailureCode | None = None
    if durable_state is AgentAttemptState.PREPARED:
        if state_version not in (0, 1) or receipt_value is not None:
            raise RunTransitionConflict("prepared agent attempt shape disagrees")
    elif durable_state is AgentAttemptState.LAUNCH_ARMED:
        if state_version < 1 or receipt_value is not None:
            raise RunTransitionConflict("armed agent attempt shape disagrees")
    elif durable_state is AgentAttemptState.FAILED:
        if state_version < 2 or receipt_value is not None:
            raise RunTransitionConflict("failed agent attempt shape disagrees")
        failure = AgentAttemptFailureCode(str(failure_value))
    elif durable_state in {
        AgentAttemptState.CANCEL_REQUESTED,
        AgentAttemptState.CANCELLED,
        AgentAttemptState.INTERRUPTED,
    }:
        if receipt_value is not None or record["cancellation_command_id"] is None:
            raise RunTransitionConflict("cancelled agent attempt shape disagrees")
    else:
        raise RunTransitionConflict("agent attempt state has no projected shape")
    if (failure_value is None) != (failure is None):
        raise RunTransitionConflict("current agent attempt failure shape disagrees")
    command_id = record["cancellation_command_id"]
    disposition = record["cancellation_disposition"]
    cancellation = (
        None
        if command_id is None
        else AgentAttemptCancellationProjection(
            str(command_id),
            AgentAttemptReplacement(str(record["replacement"])),
            AgentAttemptRedriveState(str(record["redrive_state"])),
            (
                None
                if disposition is None
                else AgentAttemptCancellationDisposition(str(disposition))
            ),
        )
    )
    return AgentAttemptProjection(
        attempt_id,
        execution_id,
        request_hash,
        ordinal,
        public_state,
        failure,
        cancellation,
    )


class DbosQueries:
    """Bounded SQLite projections; each call owns and closes its read connection."""

    def __init__(
        self,
        engine: Engine,
        *,
        busy_timeout_seconds: float = 5.0,
        query_deadline_seconds: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            not math.isfinite(busy_timeout_seconds)
            or not math.isfinite(query_deadline_seconds)
            or busy_timeout_seconds < 0.001
            or query_deadline_seconds <= 0
        ):
            raise ValueError(
                "query deadline must be finite and positive and SQLite busy timeout "
                "must be finite and at least one millisecond"
            )
        self._engine = engine
        self._busy_timeout_milliseconds = int(busy_timeout_seconds * 1000)
        self._query_deadline_seconds = query_deadline_seconds
        self._monotonic = monotonic

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self._engine.connect() as connection:
            raw = connection.connection.driver_connection
            if not isinstance(raw, sqlite3.Connection):
                raise TypeError("durable query adapter requires SQLite")
            original_busy_timeout = int(
                connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            )
            try:
                connection.exec_driver_sql(
                    f"PRAGMA busy_timeout={self._busy_timeout_milliseconds}"
                )
                deadline = self._monotonic() + self._query_deadline_seconds
                raw.set_progress_handler(
                    lambda: int(self._monotonic() >= deadline), 1000
                )
                connection.exec_driver_sql("BEGIN DEFERRED")
                yield connection
            finally:
                try:
                    raw.set_progress_handler(None, 0)
                finally:
                    try:
                        connection.rollback()
                    finally:
                        connection.exec_driver_sql(
                            f"PRAGMA busy_timeout={original_busy_timeout}"
                        )

    def get_workflow_revision(
        self,
        revision_hash: WorkflowRevisionHash,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetWorkflowRevisionResult:
        try:
            with self._connection() as connection:
                record = (
                    connection.execute(
                        _bounded_projection_select(
                            workflow_revisions,
                            projection_limit,
                            document_columns=_REVISION_DOCUMENT_COLUMNS,
                        ).where(
                            workflow_revisions.c.revision_hash == revision_hash.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return WorkflowRevisionMissing()
                _validate_bounded_record(
                    record,
                    projection_limit,
                    document_columns=_REVISION_DOCUMENT_COLUMNS,
                )
                document_bytes = bytes(record["document"])
                if projection_limit is not None:
                    projection_limit.validate_document(document_bytes)
                revision = WorkflowRevision(document_bytes)
                if revision.revision_hash != revision_hash:
                    return QueryDurableStateCorrupt()
                graph = parse_workflow_document(revision.document)
                if projection_limit is not None:
                    projection_limit.validate_graph(graph)
                return WorkflowRevisionFound(
                    WorkflowRevisionProjection(revision, graph)
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def list_workflow_revisions(
        self, after: WorkflowRevisionHash | None, limit: int
    ) -> ListWorkflowRevisionsResult:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("revision page limit must be an integer from 1 to 100")
        try:
            with self._connection() as connection:
                statement = sa.select(workflow_revisions.c.revision_hash)
                if after is not None:
                    statement = statement.where(
                        workflow_revisions.c.revision_hash > after.value
                    )
                values = tuple(
                    WorkflowRevisionHash(str(value))
                    for value in connection.execute(
                        statement.order_by(workflow_revisions.c.revision_hash).limit(
                            limit + 1
                        )
                    ).scalars()
                )
                has_more = len(values) > limit
                items = values[:limit]
                return WorkflowRevisionPage(
                    items, items[-1] if has_more and items else None
                )
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def list_described_workflow_revisions(
        self,
        after: WorkflowRevisionHash | None,
        limit: int,
        budget: EnrichedPageBudget,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ListDescribedWorkflowRevisionsResult:
        """One page of revisions with their documents, in one bounded query.

        The rows stream one at a time on purpose: a page that fetched its whole
        limit before spending its budget would move every document it then
        refused to use, which is the byte cost the budget exists to bound.
        """

        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("revision page limit must be an integer from 1 to 100")
        try:
            with self._connection() as connection:
                statement = sa.select(
                    workflow_revisions.c.revision_hash, workflow_revisions.c.document
                )
                if after is not None:
                    statement = statement.where(
                        workflow_revisions.c.revision_hash > after.value
                    )
                streamed = connection.execution_options(yield_per=1).execute(
                    statement.order_by(workflow_revisions.c.revision_hash).limit(
                        limit + 1
                    )
                )
                items: list[WorkflowRevisionProjection] = []
                spent_nodes = 0
                spent_bytes = 0
                exhausted = False
                for record in streamed.mappings():
                    if len(items) == limit:
                        exhausted = True
                        break
                    document = bytes(record["document"])
                    if items and spent_bytes + len(document) > (
                        budget.maximum_document_bytes
                    ):
                        exhausted = True
                        break
                    if projection_limit is not None:
                        projection_limit.validate_document(document)
                    revision = WorkflowRevision(document)
                    if revision.revision_hash.value != str(record["revision_hash"]):
                        return QueryDurableStateCorrupt()
                    graph = parse_workflow_document(document)
                    if projection_limit is not None:
                        projection_limit.validate_graph(graph)
                    if items and spent_nodes + len(graph.nodes) > budget.maximum_nodes:
                        exhausted = True
                        break
                    items.append(WorkflowRevisionProjection(revision, graph))
                    spent_bytes += len(document)
                    spent_nodes += len(graph.nodes)
                streamed.close()
                return DescribedWorkflowRevisionPage(
                    tuple(items),
                    items[-1].revision.revision_hash if exhausted and items else None,
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def get_run(
        self,
        run_id: RunId,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetRunResult:
        try:
            with self._connection() as connection:
                record = (
                    connection.execute(
                        _bounded_projection_select(
                            runs,
                            projection_limit,
                            columns=_RUN_PROJECTION_COLUMNS,
                            field_columns=_RUN_FIELD_COLUMNS,
                        ).where(runs.c.run_id == run_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return RunQueryMissing()
                _validate_bounded_record(
                    record,
                    projection_limit,
                    field_columns=_RUN_FIELD_COLUMNS,
                )
                return RunFound(
                    self._run_projections(connection, (record,), projection_limit)[0]
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def list_runs(
        self,
        after: RunId | None,
        limit: int,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ListRunsResult:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("run page limit must be an integer from 1 to 100")
        try:
            with self._connection() as connection:
                statement = _bounded_projection_select(
                    runs,
                    projection_limit,
                    columns=_RUN_PROJECTION_COLUMNS,
                    field_columns=_RUN_FIELD_COLUMNS,
                )
                if after is not None:
                    statement = statement.where(runs.c.run_id > after.value)
                records = tuple(
                    connection.execute(
                        statement.order_by(runs.c.run_id).limit(limit + 1)
                    ).mappings()
                )
                has_more = len(records) > limit
                item_records = records[:limit]
                for record in item_records:
                    _validate_bounded_record(
                        record,
                        projection_limit,
                        field_columns=_RUN_FIELD_COLUMNS,
                    )
                projections = self._run_projections(
                    connection, item_records, projection_limit
                )
                ordered_bytes = tuple(
                    projection.run.run_id.value.encode("utf-8")
                    for projection in projections
                )
                if ordered_bytes != tuple(sorted(ordered_bytes)) or (
                    after is not None
                    and ordered_bytes
                    and ordered_bytes[0] <= after.value.encode("utf-8")
                ):
                    raise RunTransitionConflict(
                        "SQLite run order disagrees with exact UTF-8 byte order"
                    )
                return RunPage(
                    projections,
                    (projections[-1].run.run_id if has_more and projections else None),
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (UnicodeEncodeError, ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def get_reconciliation_retry_target(
        self,
        run_id: RunId,
        command_id: ReconcileCommandId,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> GetReconciliationRetryTargetResult:
        try:
            with self._connection() as connection:
                run_exists = connection.scalar(
                    sa.select(sa.literal(True)).where(runs.c.run_id == run_id.value)
                )
                if run_exists is None:
                    return RunQueryMissing()
                command_record = (
                    connection.execute(
                        _bounded_projection_select(
                            reconcile_commands,
                            projection_limit,
                            payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                            field_columns=_COMMAND_FIELD_COLUMNS,
                        ).where(reconcile_commands.c.command_id == command_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if command_record is None:
                    return ReconciliationRetryTargetMissing()
                _validate_bounded_record(
                    command_record,
                    projection_limit,
                    payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                    field_columns=_COMMAND_FIELD_COLUMNS,
                )
                intent_record = (
                    connection.execute(
                        _bounded_projection_select(
                            effect_intents,
                            projection_limit,
                            payload_columns=_INTENT_PAYLOAD_COLUMNS,
                            field_columns=_INTENT_FIELD_COLUMNS,
                        ).where(
                            effect_intents.c.logical_key
                            == command_record["logical_key"]
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if intent_record is None:
                    return QueryDurableStateCorrupt()
                _validate_bounded_record(
                    intent_record,
                    projection_limit,
                    payload_columns=_INTENT_PAYLOAD_COLUMNS,
                    field_columns=_INTENT_FIELD_COLUMNS,
                )
                intent = intent_snapshot_from_record(intent_record)
                if intent.intent.binding.run_id != run_id:
                    return ReconciliationRetryCommandConflict()
                return ReconciliationRetryTargetFound(intent)
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def _run_projections(
        self,
        connection: Connection,
        records: Sequence[Mapping[Any, Any]],
        projection_limit: DurableProjectionLimit | None = None,
    ) -> tuple[RunProjection, ...]:
        if not records:
            return ()
        loaded_runs = tuple(
            run_from_record_with_bindings(connection, record) for record in records
        )
        revision_hashes = {run.revision_hash for run in loaded_runs}
        revision_rows = tuple(
            connection.execute(
                _bounded_projection_select(
                    workflow_revisions,
                    projection_limit,
                    document_columns=_REVISION_DOCUMENT_COLUMNS,
                ).where(
                    workflow_revisions.c.revision_hash.in_(
                        tuple(value.value for value in revision_hashes)
                    )
                )
            ).mappings()
        )
        for record in revision_rows:
            _validate_bounded_record(
                record,
                projection_limit,
                document_columns=_REVISION_DOCUMENT_COLUMNS,
            )
        revision_records = {
            WorkflowRevisionHash(str(record["revision_hash"])): bytes(
                record["document"]
            )
            for record in revision_rows
        }
        if set(revision_records) != revision_hashes:
            raise RunTransitionConflict(
                "run page references a missing workflow revision"
            )
        graphs = {}
        for revision_hash, document in revision_records.items():
            if projection_limit is not None:
                projection_limit.validate_document(document)
            graph = graph_from_document(revision_hash, document)
            if projection_limit is not None:
                projection_limit.validate_graph(graph)
            graphs[revision_hash] = graph
        for run in loaded_runs:
            validate_run_graph_binding(run, graphs[run.revision_hash])

        current_agent_executions = {
            run.run_id: NodeExecutionId.for_node(
                run.run_id, run.revision_hash, run.current_node_id
            )
            for run in loaded_runs
            if isinstance(
                graphs[run.revision_hash].node(run.current_node_id), AgentNodeV2
            )
        }
        attempt_records: dict[str, list[Mapping[Any, Any]]] = {}
        if current_agent_executions:
            for record in connection.execute(
                _bounded_projection_select(
                    agent_attempts,
                    projection_limit,
                    field_columns=_ATTEMPT_FIELD_COLUMNS,
                ).where(
                    agent_attempts.c.node_execution_id.in_(
                        tuple(
                            execution.value
                            for execution in current_agent_executions.values()
                        )
                    )
                )
            ).mappings():
                _validate_bounded_record(
                    record,
                    projection_limit,
                    field_columns=_ATTEMPT_FIELD_COLUMNS,
                )
                execution_value = str(record["node_execution_id"])
                attempt_records.setdefault(execution_value, []).append(record)
            for records_for_execution in attempt_records.values():
                records_for_execution.sort(
                    key=lambda item: int(item["attempt_ordinal"])
                )
                ordinals = tuple(
                    int(item["attempt_ordinal"]) for item in records_for_execution
                )
                if ordinals not in {(1,), (1, 2)}:
                    raise RunTransitionConflict(
                        "current node has a noncanonical agent-attempt sequence"
                    )

        waiting_runs = tuple(
            run for run in loaded_runs if run.state is RunState.WAITING_RECONCILIATION
        )
        logical_keys_by_run = {
            run.run_id: logical_effect_key_for(
                NodeExecutionId.for_node(
                    run.run_id, run.revision_hash, run.current_node_id
                )
            )
            for run in waiting_runs
        }
        intent_records: dict[str, Mapping[Any, Any]] = {}
        if waiting_runs:
            for record in connection.execute(
                _bounded_projection_select(
                    effect_intents,
                    projection_limit,
                    payload_columns=_INTENT_PAYLOAD_COLUMNS,
                    field_columns=_INTENT_FIELD_COLUMNS,
                ).where(
                    effect_intents.c.logical_key.in_(
                        tuple(key.value for key in logical_keys_by_run.values())
                    )
                )
            ).mappings():
                _validate_bounded_record(
                    record,
                    projection_limit,
                    payload_columns=_INTENT_PAYLOAD_COLUMNS,
                    field_columns=_INTENT_FIELD_COLUMNS,
                )
                key = str(record["logical_key"])
                if key in intent_records:
                    raise RunTransitionConflict("durable intent primary key repeated")
                intent_records[key] = record
        if set(intent_records) != {key.value for key in logical_keys_by_run.values()}:
            raise RunTransitionConflict(
                "WAITING_RECONCILIATION run has no exact durable intent"
            )

        owner_ids = tuple(
            str(record["reconciliation_owner_command_id"])
            for record in intent_records.values()
            if record["reconciliation_owner_command_id"] is not None
        )
        command_rows = (
            tuple(
                connection.execute(
                    _bounded_projection_select(
                        reconcile_commands,
                        projection_limit,
                        payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                        field_columns=_COMMAND_FIELD_COLUMNS,
                    ).where(reconcile_commands.c.command_id.in_(owner_ids))
                ).mappings()
            )
            if owner_ids
            else ()
        )
        for record in command_rows:
            _validate_bounded_record(
                record,
                projection_limit,
                payload_columns=_COMMAND_PAYLOAD_COLUMNS,
                field_columns=_COMMAND_FIELD_COLUMNS,
            )
        command_records = {str(record["command_id"]): record for record in command_rows}
        if set(command_records) != set(owner_ids):
            raise RunTransitionConflict("reconciling intent command is missing")

        projections = []
        for run in loaded_runs:
            reconciliation: WaitingReconciliationProjection | None = None
            if run.state is RunState.WAITING_RECONCILIATION:
                logical_key = logical_keys_by_run[run.run_id]
                intent_record = intent_records[logical_key.value]
                intent = intent_snapshot_from_record(intent_record)
                if (
                    intent.intent.binding.run_id != run.run_id
                    or intent.intent.binding.workflow_revision_hash != run.revision_hash
                    or intent.intent.binding.logical_key != logical_key
                ):
                    raise RunTransitionConflict(
                        "waiting run intent binding disagrees with its logical key"
                    )
                pending = None
                owner = intent_record["reconciliation_owner_command_id"]
                if intent.state is EffectIntentState.RECONCILING:
                    if owner is None:
                        raise RunTransitionConflict(
                            "reconciling intent has no command owner"
                        )
                    pending = command_snapshot_from_record(
                        command_records[str(owner)], intent.intent
                    )
                    if pending.state is not ReconcileCommandState.PENDING:
                        raise RunTransitionConflict(
                            "reconciling intent command is not pending"
                        )
                elif (
                    intent.state is not EffectIntentState.WAITING_RECONCILIATION
                    or owner is not None
                ):
                    raise RunTransitionConflict(
                        "waiting reconciliation run has inconsistent intent state"
                    )
                reconciliation = WaitingReconciliationProjection(intent, pending)
            attempt_projections: tuple[AgentAttemptProjection, ...] = ()
            execution = current_agent_executions.get(run.run_id)
            if execution is not None:
                if not isinstance(run, RunV2):
                    raise RunTransitionConflict("V2 agent node belongs to a V1 run")
                records_for_execution = attempt_records.get(execution.value, [])
                if records_for_execution:
                    graph = graphs[run.revision_hash]
                    if not isinstance(graph, WorkflowGraphV2):
                        raise RunTransitionConflict("V2 run has a V1 workflow graph")
                    attempt_projections = tuple(
                        _current_attempt_projection(
                            attempt_record,
                            run=run,
                            graph=graph,
                        )
                        for attempt_record in records_for_execution
                    )
            projections.append(
                RunProjection(
                    run,
                    graphs[run.revision_hash],
                    reconciliation,
                    attempt_projections,
                )
            )
        return tuple(projections)

    def prepare_run_event_stream(
        self, run_id: RunId, after_sequence: int
    ) -> PrepareRunEventStreamResult:
        if type(after_sequence) is not int or after_sequence < 0:
            return EventHistoryCorrupt()
        try:
            with self._connection() as connection:
                record = (
                    connection.execute(
                        sa.select(runs.c.state, runs.c.last_event_sequence).where(
                            runs.c.run_id == run_id.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return RunQueryMissing()
                head = int(record["last_event_sequence"])
                if after_sequence > head:
                    return CursorAhead()
                terminal = str(record["state"]) == RunState.COMPLETED.value
                if head == 0:
                    first_sequence = connection.scalar(
                        sa.select(run_events.c.event_sequence)
                        .where(run_events.c.run_id == run_id.value)
                        .order_by(run_events.c.event_sequence)
                        .limit(1)
                    )
                    if first_sequence is not None or terminal:
                        return EventHistoryCorrupt()
                    return StreamReady(head, terminal, after_sequence)

                required_sequences = {1, head}
                if after_sequence > 0:
                    required_sequences.add(after_sequence)
                endpoint_records = {
                    int(endpoint["event_sequence"]): str(endpoint["event_kind"])
                    for endpoint in connection.execute(
                        sa.select(
                            run_events.c.event_sequence,
                            run_events.c.event_kind,
                        ).where(
                            run_events.c.run_id == run_id.value,
                            run_events.c.event_sequence.in_(required_sequences),
                        )
                    ).mappings()
                }
                if set(endpoint_records) != required_sequences:
                    return EventHistoryCorrupt()
                terminal_kind = RunEventKind.SUBWORKFLOW_COMPLETED.value
                if (endpoint_records[head] == terminal_kind) != terminal or any(
                    sequence < head and kind == terminal_kind
                    for sequence, kind in endpoint_records.items()
                ):
                    return EventHistoryCorrupt()
                return StreamReady(head, terminal, after_sequence)
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (TypeError, ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def read_run_event_page(
        self,
        run_id: RunId,
        after_sequence: int,
        limit: int,
        projection_limit: DurableProjectionLimit | None = None,
    ) -> ReadRunEventPageResult:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("event page limit must be an integer from 1 to 100")
        try:
            with self._connection() as connection:
                run_record = (
                    connection.execute(
                        sa.select(
                            runs.c.state,
                            runs.c.last_event_sequence,
                            runs.c.workflow_format_version,
                        ).where(runs.c.run_id == run_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if run_record is None:
                    return QueryDurableStateCorrupt()
                head = int(run_record["last_event_sequence"])
                if after_sequence < 0 or after_sequence > head:
                    return EventHistoryCorrupt()
                records = tuple(
                    connection.execute(
                        _bounded_projection_select(
                            run_events,
                            projection_limit,
                            payload_columns=_EVENT_PAYLOAD_COLUMNS,
                            field_columns=_EVENT_FIELD_COLUMNS,
                        )
                        .where(
                            run_events.c.run_id == run_id.value,
                            run_events.c.event_sequence > after_sequence,
                        )
                        .order_by(run_events.c.event_sequence)
                        .limit(limit)
                    )
                    .mappings()
                    .all()
                )
                for record in records:
                    _validate_bounded_record(
                        record,
                        projection_limit,
                        payload_columns=_EVENT_PAYLOAD_COLUMNS,
                        field_columns=_EVENT_FIELD_COLUMNS,
                    )
                sequences = tuple(int(record["event_sequence"]) for record in records)
                expected_sequences = tuple(
                    range(after_sequence + 1, min(head, after_sequence + limit) + 1)
                )
                if sequences != expected_sequences:
                    return EventHistoryCorrupt()
                terminal_kind = RunEventKind.SUBWORKFLOW_COMPLETED.value
                terminal_sequences = tuple(
                    int(record["event_sequence"])
                    for record in records
                    if str(record["event_kind"]) == terminal_kind
                )
                terminal = str(run_record["state"]) == RunState.COMPLETED.value
                reached_head = bool(sequences) and sequences[-1] == head
                if terminal_sequences not in ((), (head,)) or (
                    reached_head and ((terminal_sequences == (head,)) != terminal)
                ):
                    return EventHistoryCorrupt()
                events = tuple(
                    self._event_projection(
                        connection,
                        record,
                        projection_limit,
                        int(run_record["workflow_format_version"]),
                    )
                    for record in records
                )
                return RunEventPage(
                    events,
                    terminal_sequences == (head,),
                )
        except ProjectionLimitExceeded:
            return ProjectionTooLarge()
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (
            RevisionHashCollision,
            RunTransitionConflict,
            TypeError,
            ValueError,
            RuntimeError,
            DatabaseError,
        ):
            return QueryDurableStateCorrupt()

    @staticmethod
    def _event_projection(
        connection: Connection,
        record: Mapping[Any, Any],
        projection_limit: DurableProjectionLimit | None,
        workflow_format_version: int,
    ) -> PersistedRunEvent:
        event = event_from_record(record)
        if (
            event.event_kind is RunEventKind.AGENT_FAILED
            and workflow_format_version != 2
        ):
            raise RunTransitionConflict("V1 run carries a V2 agent failure event")
        if event.event_kind is RunEventKind.AGENT_FAILED and event.payload != (
            AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY.value.encode("ascii")
        ):
            raise RunTransitionConflict("agent failure event payload is not canonical")
        if event.event_kind not in {
            RunEventKind.ACTION_RECONCILIATION_RESOLVED,
            RunEventKind.ACTION_COMPLETED,
        }:
            return PersistedRunEvent(event, None, workflow_format_version)
        logical_key = event.receipt_logical_key
        if logical_key is None:
            raise RunTransitionConflict("receipt event has no logical key")
        receipt_record = (
            connection.execute(
                _bounded_projection_select(
                    effect_receipts,
                    projection_limit,
                    payload_columns=_RECEIPT_PAYLOAD_COLUMNS,
                    field_columns=_RECEIPT_FIELD_COLUMNS,
                ).where(effect_receipts.c.logical_key == logical_key.value)
            )
            .mappings()
            .one_or_none()
        )
        if receipt_record is None:
            raise RunTransitionConflict("receipt event has no durable receipt")
        _validate_bounded_record(
            receipt_record,
            projection_limit,
            payload_columns=_RECEIPT_PAYLOAD_COLUMNS,
            field_columns=_RECEIPT_FIELD_COLUMNS,
        )
        receipt = receipt_from_record(receipt_record)
        if (
            receipt.intent.binding.run_id != event.run_id
            or receipt.intent.binding.workflow_revision_hash != event.revision_hash
            or receipt.result.payload_hash != event.receipt_result_hash
        ):
            raise RunTransitionConflict("receipt event binding disagrees")
        return PersistedRunEvent(event, receipt, workflow_format_version)
