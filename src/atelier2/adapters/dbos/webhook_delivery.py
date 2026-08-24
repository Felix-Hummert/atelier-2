"""The V31 `webhook_delivery_cursor` table: one durable row, one CAS.

A singleton row (`cursor_id` fixed to `_CURSOR_ID`) rather than a table keyed
by subscriber, because `#433`'s ratified plan opens no subscription table --
one connected webhook, one durable position on the attention feed. The row
is created on its first advance rather than at schema initialization, so a
store that never delivers a webhook carries no row for one either.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DatabaseError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from atelier2.adapters.dbos.schema import webhook_delivery_cursor
from atelier2.adapters.dbos.transactions import canonical_write_transaction
from atelier2.contracts.runs import RunId
from atelier2.contracts.webhook_delivery import (
    WEBHOOK_DELIVERY_CURSOR_REVISION_INITIAL,
    AdvanceWebhookDeliveryCursor,
    WebhookDeliveryCursor,
    WebhookDeliveryCursorAdvanced,
    WebhookDeliveryCursorRevision,
    WebhookDeliveryCursorState,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable
from atelier2.ports.webhook_delivery import (
    AdvanceWebhookDeliveryCursorResult,
    ReadWebhookDeliveryCursorResult,
)

_CURSOR_ID: Final = "attention-events"


class _WebhookCursorAdvanceRaceViolation(RuntimeError):
    """The CAS update matched no row inside a transaction that already held
    the write lock -- durable rows do not form the transition just decided."""


def _state_from_record(record: Mapping[Any, Any]) -> WebhookDeliveryCursorState:
    run_id = record["run_id"]
    event_sequence = record["event_sequence"]
    cursor = (
        None
        if run_id is None
        else WebhookDeliveryCursor(RunId(str(run_id)), int(event_sequence))
    )
    return WebhookDeliveryCursorState(
        cursor, WebhookDeliveryCursorRevision(int(record["revision"]))
    )


class DbosWebhookDeliveryPublisher:
    """The V31 `webhook_delivery_cursor` table's read and CAS-advance side."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def read_cursor(self) -> ReadWebhookDeliveryCursorResult:
        try:
            with self._engine.connect() as connection:
                record = (
                    connection.execute(
                        sa.select(webhook_delivery_cursor).where(
                            webhook_delivery_cursor.c.cursor_id == _CURSOR_ID
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
            if record is None:
                return WebhookDeliveryCursorState(
                    None, WEBHOOK_DELIVERY_CURSOR_REVISION_INITIAL
                )
            return _state_from_record(record)
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()

    def advance_cursor(
        self, command: AdvanceWebhookDeliveryCursor
    ) -> AdvanceWebhookDeliveryCursorResult:
        try:
            with canonical_write_transaction(self._engine) as connection:
                connection.execute(
                    sa.insert(webhook_delivery_cursor)
                    .prefix_with("OR IGNORE")
                    .values(
                        cursor_id=_CURSOR_ID,
                        run_id=None,
                        event_sequence=None,
                        revision=WEBHOOK_DELIVERY_CURSOR_REVISION_INITIAL.value,
                    )
                )
                record = (
                    connection.execute(
                        sa.select(webhook_delivery_cursor).where(
                            webhook_delivery_cursor.c.cursor_id == _CURSOR_ID
                        )
                    )
                    .mappings()
                    .one()
                )
                state = _state_from_record(record)
                outcome = state.advance(command)
                if isinstance(outcome, WebhookDeliveryCursorAdvanced):
                    updated = connection.execute(
                        webhook_delivery_cursor.update()
                        .where(
                            webhook_delivery_cursor.c.cursor_id == _CURSOR_ID,
                            webhook_delivery_cursor.c.revision
                            == command.expected_revision.value,
                        )
                        .values(
                            run_id=outcome.cursor.run_id.value,
                            event_sequence=outcome.cursor.event_sequence,
                            revision=outcome.revision.value,
                        )
                    )
                    if updated.rowcount != 1:
                        raise _WebhookCursorAdvanceRaceViolation(
                            "webhook delivery cursor CAS changed no row"
                        )
                return outcome
        except (OperationalError, PoolTimeoutError):
            return DurableWriteUnavailable()
        except (ValueError, RuntimeError, DatabaseError):
            return DurableStateCorrupt()
