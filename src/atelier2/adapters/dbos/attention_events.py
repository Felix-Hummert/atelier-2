"""The attention feed: WAITING_INPUT and AGENT_FAILED across runs, by instant.

Pre-V22 events have no event_instants row. This feed inner-joins that table so
those rows stay off it rather than inventing a time.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.engine import Connection

from atelier2.adapters.dbos.schema import event_instants, run_events, runs
from atelier2.contracts.executions import RunEventKind
from atelier2.contracts.run_events import PersistedRunEvent
from atelier2.contracts.runs import RunId
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.run_events import (
    AttentionCursorUnknown,
    AttentionEventPage,
    ReadAttentionEventPageResult,
)
from atelier2.ports.workflow_revisions import DurableProjectionLimit

ATTENTION_EVENT_KINDS: Final[tuple[RunEventKind, ...]] = (
    RunEventKind.WAITING_INPUT,
    RunEventKind.AGENT_FAILED,
)
_ATTENTION_KIND_VALUES: Final[tuple[str, ...]] = tuple(
    kind.value for kind in ATTENTION_EVENT_KINDS
)

_ProjectEvent = Callable[
    [Connection, Mapping[Any, Any], WorkflowFormatVersion, DurableProjectionLimit],
    PersistedRunEvent,
]


def load_attention_event_page(
    connection: Connection,
    after_run_id: RunId | None,
    after_sequence: int | None,
    limit: int,
    projection_limit: DurableProjectionLimit,
    project_event: _ProjectEvent,
) -> ReadAttentionEventPageResult:
    from atelier2.adapters.dbos.queries import (
        _EVENT_FIELD_COLUMNS,
        _EVENT_PAYLOAD_COLUMNS,
        _bounded_projection_select,
        _validate_bounded_record,
    )

    origin = _resume_origin(connection, after_run_id, after_sequence)
    if isinstance(origin, AttentionCursorUnknown):
        return origin
    statement = (
        _bounded_projection_select(
            run_events,
            projection_limit,
            payload_columns=_EVENT_PAYLOAD_COLUMNS,
            field_columns=_EVENT_FIELD_COLUMNS,
        )
        .join(
            event_instants,
            sa.and_(
                event_instants.c.run_id == run_events.c.run_id,
                event_instants.c.event_sequence == run_events.c.event_sequence,
            ),
        )
        .join(runs, runs.c.run_id == run_events.c.run_id)
        .add_columns(runs.c.workflow_format_version)
        .where(run_events.c.event_kind.in_(_ATTENTION_KIND_VALUES))
        .order_by(
            event_instants.c.recorded_at,
            run_events.c.run_id,
            run_events.c.event_sequence,
        )
        .limit(limit)
    )
    if origin is not None:
        recorded_at, run_id, sequence = origin
        statement = statement.where(
            sa.or_(
                event_instants.c.recorded_at > recorded_at,
                sa.and_(
                    event_instants.c.recorded_at == recorded_at,
                    run_events.c.run_id > run_id,
                ),
                sa.and_(
                    event_instants.c.recorded_at == recorded_at,
                    run_events.c.run_id == run_id,
                    run_events.c.event_sequence > sequence,
                ),
            )
        )
    records = tuple(connection.execute(statement).mappings().all())
    events = []
    for record in records:
        _validate_bounded_record(
            record,
            projection_limit,
            payload_columns=_EVENT_PAYLOAD_COLUMNS,
            field_columns=_EVENT_FIELD_COLUMNS,
        )
        events.append(
            project_event(
                connection,
                record,
                WorkflowFormatVersion(int(record["workflow_format_version"])),
                projection_limit,
            )
        )
    return AttentionEventPage(tuple(events))


def _resume_origin(
    connection: Connection,
    after_run_id: RunId | None,
    after_sequence: int | None,
) -> tuple[str, str, int] | AttentionCursorUnknown | None:
    if after_run_id is None:
        return None
    if after_sequence is None:
        return AttentionCursorUnknown()
    recorded_at = connection.scalar(
        sa.select(event_instants.c.recorded_at)
        .select_from(run_events)
        .join(
            event_instants,
            sa.and_(
                event_instants.c.run_id == run_events.c.run_id,
                event_instants.c.event_sequence == run_events.c.event_sequence,
            ),
        )
        .where(
            run_events.c.run_id == after_run_id.value,
            run_events.c.event_sequence == after_sequence,
            run_events.c.event_kind.in_(_ATTENTION_KIND_VALUES),
        )
    )
    if recorded_at is None:
        return AttentionCursorUnknown()
    return (str(recorded_at), after_run_id.value, after_sequence)
