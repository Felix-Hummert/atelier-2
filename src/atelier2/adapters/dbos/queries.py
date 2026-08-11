from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DatabaseError, OperationalError

from atelier2.adapters.dbos.effect_store import (
    command_snapshot_from_record,
    intent_snapshot_from_record,
    receipt_from_record,
)
from atelier2.adapters.dbos.run_store import (
    RunTransitionConflict,
    event_from_record,
    load_graph,
    load_run,
)
from atelier2.adapters.dbos.schema import (
    effect_intents,
    effect_receipts,
    reconcile_commands,
    run_events,
    runs,
    workflow_revisions,
)
from atelier2.adapters.yaml_workflows import parse_workflow_document
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
    GetWorkflowRevisionResult,
    ListWorkflowRevisionsResult,
    QueryDurableStateCorrupt,
    ReadUnavailable,
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
    ) -> None:
        if busy_timeout_seconds <= 0 or query_deadline_seconds <= 0:
            raise ValueError("query time bounds must be positive")
        self._engine = engine
        self._busy_timeout_milliseconds = int(busy_timeout_seconds * 1000)
        self._query_deadline_seconds = query_deadline_seconds

    @contextmanager
    def _connection(self) -> Iterator[Connection]:
        with self._engine.connect() as connection:
            connection.exec_driver_sql(
                f"PRAGMA busy_timeout={self._busy_timeout_milliseconds}"
            )
            raw = connection.connection.driver_connection
            if not isinstance(raw, sqlite3.Connection):
                raise TypeError("durable query adapter requires SQLite")
            deadline = time.monotonic() + self._query_deadline_seconds
            raw.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            try:
                yield connection
            finally:
                raw.set_progress_handler(None, 0)

    def get_workflow_revision(
        self, revision_hash: WorkflowRevisionHash
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
                revision = WorkflowRevision(bytes(document))
                if revision.revision_hash != revision_hash:
                    return QueryDurableStateCorrupt()
                return WorkflowRevisionFound(
                    WorkflowRevisionProjection(
                        revision, parse_workflow_document(revision.document)
                    )
                )
        except OperationalError:
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
        except OperationalError:
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def get_run(self, run_id: RunId) -> GetRunResult:
        try:
            with self._connection() as connection:
                exists = connection.scalar(
                    sa.select(sa.literal(True)).where(runs.c.run_id == run_id.value)
                )
                if exists is None:
                    return RunQueryMissing()
                return RunFound(self._run_projection(connection, run_id))
        except OperationalError:
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def list_runs(self, after: RunId | None, limit: int) -> ListRunsResult:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("run page limit must be an integer from 1 to 100")
        encoded_run_id = sa.cast(runs.c.run_id, sa.LargeBinary)
        try:
            with self._connection() as connection:
                statement = sa.select(runs.c.run_id)
                if after is not None:
                    statement = statement.where(
                        encoded_run_id > after.value.encode("utf-8")
                    )
                run_ids = tuple(
                    RunId(str(value))
                    for value in connection.execute(
                        statement.order_by(encoded_run_id).limit(limit + 1)
                    ).scalars()
                )
                has_more = len(run_ids) > limit
                item_ids = run_ids[:limit]
                return RunPage(
                    tuple(
                        self._run_projection(connection, item_id)
                        for item_id in item_ids
                    ),
                    item_ids[-1] if has_more and item_ids else None,
                )
        except OperationalError:
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
        except OperationalError:
            return ReadUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return QueryDurableStateCorrupt()

    def _run_projection(self, connection: Connection, run_id: RunId) -> RunProjection:
        run = load_run(connection, run_id)
        graph = load_graph(connection, run.revision_hash)
        reconciliation: WaitingReconciliationProjection | None = None
        if run.state is RunState.WAITING_RECONCILIATION:
            intent_record = (
                connection.execute(
                    sa.select(effect_intents).where(
                        effect_intents.c.run_id == run.run_id.value,
                        effect_intents.c.workflow_revision_hash
                        == run.revision_hash.value,
                    )
                )
                .mappings()
                .one_or_none()
            )
            if intent_record is None:
                raise RunTransitionConflict(
                    "WAITING_RECONCILIATION run has no durable intent"
                )
            intent = intent_snapshot_from_record(intent_record)
            pending = None
            owner = intent_record["reconciliation_owner_command_id"]
            if intent.state is EffectIntentState.RECONCILING:
                if owner is None:
                    raise RunTransitionConflict(
                        "reconciling intent has no command owner"
                    )
                command_record = (
                    connection.execute(
                        sa.select(reconcile_commands).where(
                            reconcile_commands.c.command_id == owner
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if command_record is None:
                    raise RunTransitionConflict("reconciling intent command is missing")
                pending = command_snapshot_from_record(command_record, intent.intent)
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
        return RunProjection(run, graph, reconciliation)

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
                count, minimum, maximum = connection.execute(
                    sa.select(
                        sa.func.count(),
                        sa.func.min(run_events.c.event_sequence),
                        sa.func.max(run_events.c.event_sequence),
                    ).where(run_events.c.run_id == run_id.value)
                ).one()
                if (head == 0 and int(count) != 0) or (
                    head > 0
                    and (
                        int(count) != head or int(minimum) != 1 or int(maximum) != head
                    )
                ):
                    return EventHistoryCorrupt()
                terminal = str(record["state"]) == RunState.COMPLETED.value
                terminal_sequences = tuple(
                    int(value)
                    for value in connection.execute(
                        sa.select(run_events.c.event_sequence)
                        .where(
                            run_events.c.run_id == run_id.value,
                            run_events.c.event_kind
                            == RunEventKind.SUBWORKFLOW_COMPLETED.value,
                        )
                        .order_by(run_events.c.event_sequence)
                        .limit(2)
                    ).scalars()
                )
                if (terminal and terminal_sequences != (head,)) or (
                    not terminal and terminal_sequences
                ):
                    return EventHistoryCorrupt()
                return StreamReady(head, terminal, after_sequence)
        except OperationalError:
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
                events = tuple(
                    self._event_projection(connection, record) for record in records
                )
                if events and events[0].event.event_sequence != after_sequence + 1:
                    return QueryDurableStateCorrupt()
                return RunEventPage(
                    events,
                    any(
                        item.event.event_kind is RunEventKind.SUBWORKFLOW_COMPLETED
                        for item in events
                    ),
                )
        except OperationalError:
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
