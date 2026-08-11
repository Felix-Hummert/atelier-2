from __future__ import annotations

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
    run_from_record,
    validate_run_graph_binding,
)
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    reconcile_commands,
    run_events,
    runs,
    workflow_revisions,
)
from atelier2.contracts.effects import (
    EffectIntentState,
    ReconcileCommandId,
    ReconcileCommandState,
)
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.runs import (
    RevisionHashCollision,
    RunId,
    RunState,
    WorkflowRevision,
    WorkflowRevisionHash,
)
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
    PROJECTION_LIMIT_DETAIL,
    GetWorkflowRevisionResult,
    ListWorkflowRevisionsResult,
    ProjectionLimitExceeded,
    QueryDurableStateCorrupt,
    ReadUnavailable,
    WorkflowProjectionLimit,
    WorkflowRevisionFound,
    WorkflowRevisionMissing,
    WorkflowRevisionPage,
    WorkflowRevisionProjection,
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
        if busy_timeout_seconds <= 0 or query_deadline_seconds <= 0:
            raise ValueError("query time bounds must be positive")
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
        projection_limit: WorkflowProjectionLimit | None = None,
    ) -> GetWorkflowRevisionResult:
        try:
            with self._connection() as connection:
                document = connection.scalar(
                    sa.select(workflow_revisions.c.document).where(
                        workflow_revisions.c.revision_hash == revision_hash.value
                    )
                )
                if document is None:
                    return WorkflowRevisionMissing()
                document_bytes = bytes(document)
                if projection_limit is not None:
                    projection_limit.validate_document(document_bytes)
                revision = WorkflowRevision(document_bytes)
                if revision.revision_hash != revision_hash:
                    return QueryDurableStateCorrupt()
                graph = graph_from_document(revision_hash, revision.document)
                if projection_limit is not None:
                    projection_limit.validate_graph(graph)
                return WorkflowRevisionFound(
                    WorkflowRevisionProjection(revision, graph)
                )
        except ProjectionLimitExceeded:
            return ReadUnavailable(PROJECTION_LIMIT_DETAIL)
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

    def get_run(
        self,
        run_id: RunId,
        projection_limit: WorkflowProjectionLimit | None = None,
    ) -> GetRunResult:
        try:
            with self._connection() as connection:
                record = (
                    connection.execute(
                        sa.select(runs).where(runs.c.run_id == run_id.value)
                    )
                    .mappings()
                    .one_or_none()
                )
                if record is None:
                    return RunQueryMissing()
                return RunFound(
                    self._run_projections(connection, (record,), projection_limit)[0]
                )
        except ProjectionLimitExceeded:
            return ReadUnavailable(PROJECTION_LIMIT_DETAIL)
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def list_runs(
        self,
        after: RunId | None,
        limit: int,
        projection_limit: WorkflowProjectionLimit | None = None,
    ) -> ListRunsResult:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("run page limit must be an integer from 1 to 100")
        try:
            with self._connection() as connection:
                statement = sa.select(runs)
                if after is not None:
                    statement = statement.where(runs.c.run_id > after.value)
                records = tuple(
                    connection.execute(
                        statement.order_by(runs.c.run_id).limit(limit + 1)
                    ).mappings()
                )
                has_more = len(records) > limit
                item_records = records[:limit]
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
            return ReadUnavailable(PROJECTION_LIMIT_DETAIL)
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (UnicodeEncodeError, ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def get_reconciliation_retry_target(
        self, run_id: RunId, command_id: ReconcileCommandId
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
                        sa.select(reconcile_commands).where(
                            reconcile_commands.c.command_id == command_id.value
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if command_record is None:
                    return ReconciliationRetryTargetMissing()
                intent_record = (
                    connection.execute(
                        sa.select(effect_intents).where(
                            effect_intents.c.logical_key
                            == command_record["logical_key"]
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if intent_record is None:
                    return QueryDurableStateCorrupt()
                intent = intent_snapshot_from_record(intent_record)
                if intent.intent.binding.run_id != run_id:
                    return ReconciliationRetryCommandConflict()
                return ReconciliationRetryTargetFound(intent)
        except (OperationalError, PoolTimeoutError):
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def _run_projections(
        self,
        connection: Connection,
        records: Sequence[Mapping[Any, Any]],
        projection_limit: WorkflowProjectionLimit | None = None,
    ) -> tuple[RunProjection, ...]:
        if not records:
            return ()
        loaded_runs = tuple(run_from_record(record) for record in records)
        revision_hashes = {run.revision_hash for run in loaded_runs}
        revision_records = {
            WorkflowRevisionHash(str(record["revision_hash"])): bytes(
                record["document"]
            )
            for record in connection.execute(
                sa.select(
                    workflow_revisions.c.revision_hash,
                    workflow_revisions.c.document,
                ).where(
                    workflow_revisions.c.revision_hash.in_(
                        tuple(value.value for value in revision_hashes)
                    )
                )
            ).mappings()
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

        waiting_runs = tuple(
            run for run in loaded_runs if run.state is RunState.WAITING_RECONCILIATION
        )
        intent_records: dict[tuple[str, str], Mapping[Any, Any]] = {}
        if waiting_runs:
            waiting_ids = tuple(run.run_id.value for run in waiting_runs)
            for record in connection.execute(
                sa.select(effect_intents).where(
                    effect_intents.c.run_id.in_(waiting_ids)
                )
            ).mappings():
                key = (str(record["run_id"]), str(record["workflow_revision_hash"]))
                if key in intent_records:
                    raise RunTransitionConflict(
                        "waiting run has more than one durable intent"
                    )
                intent_records[key] = record
        expected_intent_keys = {
            (run.run_id.value, run.revision_hash.value) for run in waiting_runs
        }
        if set(intent_records) != expected_intent_keys:
            raise RunTransitionConflict(
                "WAITING_RECONCILIATION run has no exact durable intent"
            )

        owner_ids = tuple(
            str(record["reconciliation_owner_command_id"])
            for record in intent_records.values()
            if record["reconciliation_owner_command_id"] is not None
        )
        command_records = (
            {
                str(record["command_id"]): record
                for record in connection.execute(
                    sa.select(reconcile_commands).where(
                        reconcile_commands.c.command_id.in_(owner_ids)
                    )
                ).mappings()
            }
            if owner_ids
            else {}
        )
        if set(command_records) != set(owner_ids):
            raise RunTransitionConflict("reconciling intent command is missing")

        projections = []
        for run in loaded_runs:
            reconciliation: WaitingReconciliationProjection | None = None
            if run.state is RunState.WAITING_RECONCILIATION:
                intent_record = intent_records[
                    (run.run_id.value, run.revision_hash.value)
                ]
                intent = intent_snapshot_from_record(intent_record)
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
            projections.append(
                RunProjection(run, graphs[run.revision_hash], reconciliation)
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
        self, run_id: RunId, after_sequence: int, limit: int
    ) -> ReadRunEventPageResult:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("event page limit must be an integer from 1 to 100")
        try:
            with self._connection() as connection:
                run_record = (
                    connection.execute(
                        sa.select(runs.c.state, runs.c.last_event_sequence).where(
                            runs.c.run_id == run_id.value
                        )
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
                        sa.select(run_events)
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
                    self._event_projection(connection, record) for record in records
                )
                return RunEventPage(
                    events,
                    terminal_sequences == (head,),
                )
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
        connection: Connection, record: Mapping[Any, Any]
    ) -> PersistedRunEvent:
        event = event_from_record(record)
        if event.event_kind not in {
            RunEventKind.ACTION_RECONCILIATION_RESOLVED,
            RunEventKind.ACTION_COMPLETED,
        }:
            return PersistedRunEvent(event, None)
        logical_key = event.receipt_logical_key
        if logical_key is None:
            raise RunTransitionConflict("receipt event has no logical key")
        receipt_record = (
            connection.execute(
                sa.select(effect_receipts).where(
                    effect_receipts.c.logical_key == logical_key.value
                )
            )
            .mappings()
            .one_or_none()
        )
        if receipt_record is None:
            raise RunTransitionConflict("receipt event has no durable receipt")
        receipt = receipt_from_record(receipt_record)
        if (
            receipt.intent.binding.run_id != event.run_id
            or receipt.intent.binding.workflow_revision_hash != event.revision_hash
            or receipt.result.payload_hash != event.receipt_result_hash
        ):
            raise RunTransitionConflict("receipt event binding disagrees")
        return PersistedRunEvent(event, receipt)
