"""The attention feed: the events that need an operator's hand, by instant.

WAITING_INPUT, AGENT_FAILED, and ACTION_RECONCILIATION_REQUIRED across runs --
each names a run that stands still until a person answers, judges, or
reconciles.

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
from atelier2.contracts.when import RecordedAt
from atelier2.contracts.workflow_formats import WorkflowFormatVersion
from atelier2.ports.run_events import (
    AttentionCursorUnknown,
    AttentionEvent,
    AttentionEventPage,
    ReadAttentionEventPageResult,
)
from atelier2.ports.workflow_revisions import DurableProjectionLimit

ATTENTION_EVENT_KINDS: Final[tuple[RunEventKind, ...]] = (
    RunEventKind.WAITING_INPUT,
    RunEventKind.AGENT_FAILED,
    RunEventKind.ACTION_RECONCILIATION_REQUIRED,
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
    excluded_identities: tuple[tuple[RunId, int], ...],
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
        .add_columns(runs.c.workflow_format_version, event_instants.c.recorded_at)
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
        identities = ((run_id, sequence),) + tuple(
            (extra_run_id.value, extra_sequence)
            for extra_run_id, extra_sequence in excluded_identities
        )
        statement = statement.where(
            _same_instant_identity_exclusion(recorded_at, identities)
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
            AttentionEvent(
                project_event(
                    connection,
                    record,
                    WorkflowFormatVersion(int(record["workflow_format_version"])),
                    projection_limit,
                ),
                RecordedAt(str(record["recorded_at"])),
            )
        )
    return AttentionEventPage(tuple(events))


def _same_instant_identity_exclusion(
    recorded_at: str, identities: tuple[tuple[str, int], ...]
) -> sa.ColumnElement[bool]:
    """recorded_at > T OR (recorded_at == T AND identity not in the set)."""
    later_instant = event_instants.c.recorded_at > recorded_at
    unique_identities = tuple(dict.fromkeys(identities))
    if not unique_identities:
        return later_instant
    already_emitted = sa.or_(
        *[
            sa.and_(
                run_events.c.run_id == run_id,
                run_events.c.event_sequence == sequence,
            )
            for run_id, sequence in unique_identities
        ]
    )
    return sa.or_(
        later_instant,
        sa.and_(
            event_instants.c.recorded_at == recorded_at,
            sa.not_(already_emitted),
        ),
    )


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
