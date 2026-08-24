"""Where the webhook delivery cursor's durable position and revision live.

One store, two operations: read where delivery stands, and move it on --
conditionally, never as a silent overwrite. `DurableWriteUnavailable` and
`DurableStateCorrupt` are the same two words every other durable store in
this layer already answers a failed read or write with; this port opens no
third one.
"""

from __future__ import annotations

from typing import Protocol

from atelier2.contracts.webhook_delivery import (
    AdvanceWebhookDeliveryCursor,
    AdvanceWebhookDeliveryCursorOutcome,
    WebhookDeliveryCursorState,
)
from atelier2.ports.durable_runs import DurableStateCorrupt, DurableWriteUnavailable

type ReadWebhookDeliveryCursorResult = (
    WebhookDeliveryCursorState | DurableWriteUnavailable | DurableStateCorrupt
)

type AdvanceWebhookDeliveryCursorResult = (
    AdvanceWebhookDeliveryCursorOutcome | DurableWriteUnavailable | DurableStateCorrupt
)


class WebhookDeliveryPublisher(Protocol):
    """The durable home of the one attention-feed webhook delivery cursor."""

    def read_cursor(self) -> ReadWebhookDeliveryCursorResult: ...

    def advance_cursor(
        self, command: AdvanceWebhookDeliveryCursor
    ) -> AdvanceWebhookDeliveryCursorResult: ...
