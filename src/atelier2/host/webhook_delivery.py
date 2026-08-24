"""The served instance's first background loop: deliver the attention feed.

Phase 2 of `#433` composes the phase-1 delivery decision, the real HTTP edge
and the durable cursor into one loop that runs for the life of the served app.
It reads the durable cursor, delivers the pending `WAITING_INPUT` /
`AGENT_FAILED` attention events in order, and advances the cursor only after a
2xx -- every retry, restart and at-least-once guarantee already proven at the
decision layer, unchanged here.

Head-of-line blocking is this slice's accepted behaviour: a receiver that keeps
failing holds the cursor on its event, so later attention events wait behind
it. The loop never advances past a failure, which is exactly what keeps a lost
delivery impossible without a subscription table (`#433`'s ratified plan).

The loop runs in a background thread, not the event loop: the delivery decision
and the HTTP call are both blocking, and a thread keeps them off the async
request path. `WebhookDeliveryLoop.stop` signals the thread and joins it, so a
served instance shutting down leaves no orphaned worker -- and because the
cursor only ever moved after a durable 2xx, an interrupted delivery redelivers
rather than being lost.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.types import Lifespan

from atelier2.adapters.http_webhook_transport import HttpWebhookTransport
from atelier2.application.deliver_attention_webhook import (
    AttentionWebhookCursorConflict,
    AttentionWebhookDelivered,
    AttentionWebhookPermanentFailure,
    AttentionWebhookTransientFailure,
    NoAttentionEventsPending,
    deliver_attention_webhook,
)
from atelier2.ports.run_events import RunEventQueries
from atelier2.ports.webhook_delivery import WebhookDeliveryPublisher
from atelier2.ports.webhook_transport import WebhookTransport

DEFAULT_POLL_INTERVAL_SECONDS: Final = 1.0
_ALLOWED_URL_SCHEMES: Final = frozenset({"http", "https"})

_logger = logging.getLogger("atelier2")


@dataclass(frozen=True)
class WebhookDeliverySettings:
    """Where the attention feed is delivered, and the key it is signed under.

    All-or-nothing by construction: an instance either carries both a target
    URL and a signing-key file path, or no webhook at all. A URL this record
    cannot read as an absolute `http`/`https` address is refused at startup
    rather than delivered to nowhere at runtime.

    The path is a reference, never the value (ADR 0009 §6): the key itself is
    read once, at composition, by `resolve_signing_key` -- this record holds
    only where to find it.
    """

    target_url: str
    signing_key_path: Path

    def __post_init__(self) -> None:
        parsed = urlsplit(self.target_url)
        if parsed.scheme not in _ALLOWED_URL_SCHEMES or not parsed.netloc:
            raise ValueError(
                "a webhook target url must be an absolute http or https url, "
                f"not {self.target_url!r}"
            )


def resolve_signing_key(signing_key_path: Path) -> bytes:
    """Read the webhook signing key once, at startup, from its file path.

    Fail loud: a webhook configured with an unreadable or empty key file is a
    half-configured webhook, and half-configured must not silently sign nothing
    or disable delivery. The bytes returned live only in the delivery loop that
    holds them; they are written to no log, event, projection or durable store.
    """

    try:
        key = signing_key_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ValueError(
            f"webhook signing key file {signing_key_path} did not resolve: {error}"
        ) from error
    if not key:
        raise ValueError(f"webhook signing key file {signing_key_path} is empty")
    return key.encode("utf-8")


class WebhookDeliveryLoop:
    """The background delivery worker for one served instance's webhook."""

    def __init__(
        self,
        publisher: WebhookDeliveryPublisher,
        queries: RunEventQueries,
        transport: WebhookTransport,
        signing_key: bytes,
        *,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._publisher = publisher
        self._queries = queries
        self._transport = transport
        self._signing_key = signing_key
        self._poll_interval_seconds = poll_interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def deliver_pending(self) -> None:
        """Deliver every currently pending event, in order, until idle or stuck.

        Returns when the feed is drained, a delivery fails (head-of-line
        blocking on that event), or a stop was signalled. Each `Delivered`
        advanced the cursor durably, so a return here never loses a position.
        """

        while not self._stop.is_set():
            outcome = deliver_attention_webhook(
                self._publisher, self._queries, self._transport, self._signing_key
            )
            match outcome:
                case AttentionWebhookDelivered():
                    continue
                case NoAttentionEventsPending():
                    return
                case AttentionWebhookTransientFailure(_payload, detail):
                    _logger.warning("webhook delivery held: %s", detail)
                    return
                case AttentionWebhookPermanentFailure(_payload, detail):
                    _logger.warning("webhook delivery refused: %s", detail)
                    return
                case AttentionWebhookCursorConflict():
                    _logger.info(
                        "webhook delivery cursor advanced by another writer; "
                        "re-reading and continuing"
                    )
                    return
                case _:
                    _logger.warning(
                        "webhook delivery could not read durable state: %s",
                        type(outcome).__name__,
                    )
                    return

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("webhook delivery loop already started")
        self._stop.clear()
        thread = threading.Thread(
            target=self._run, name="atelier2-webhook-delivery", daemon=True
        )
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join()
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.deliver_pending()
            except Exception:
                # A transient adapter bug must delay delivery, never disable it:
                # letting the exception kill the daemon thread would silently
                # stop delivery until the process restarts. The cursor only ever
                # moves after a durable 2xx, so a failed pass loses nothing -- we
                # log loudly (never the signing key or payload, neither of which
                # is in the traceback) and retry on the next poll.
                _logger.exception(
                    "webhook delivery hit an unexpected error; "
                    "retrying on the next poll"
                )
            self._stop.wait(self._poll_interval_seconds)


def webhook_delivery_lifespan(
    loop: WebhookDeliveryLoop, transport: HttpWebhookTransport
) -> Lifespan[FastAPI]:
    """Bind the delivery loop and its HTTP client to the served app's lifespan.

    The loop is started when the app comes up, signalled and joined when it goes
    down, and the HTTP client is closed after the worker has stopped -- so the
    background task and its one outbound connection pool are exactly as alive as
    the instance serving them, and shutdown leaves neither orphaned. The join
    and close run off the event loop so a mid-delivery shutdown does not block
    the async loop while the worker finishes.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        loop.start()
        try:
            yield
        finally:
            await asyncio.to_thread(_stop_and_close, loop, transport)

    return lifespan


def _stop_and_close(loop: WebhookDeliveryLoop, transport: HttpWebhookTransport) -> None:
    loop.stop()
    transport.close()
