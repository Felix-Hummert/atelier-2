"""Structured process logs for the served instance.

The named reader is the diagnosing agent. One JSON line per record, stderr
only, on the named process loggers that share the handler — not on the root.
A logger outside that list is outside the contract and may fall to lastResort
as plain text. Missing optional keys are omitted, not nulled.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import TextIO

PROCESS_LOGGER_NAME = "atelier2"
_SHARED_HANDLER_NAME = "atelier2-process-json"
_UNIFIED_LOGGERS = (PROCESS_LOGGER_NAME, "uvicorn", "uvicorn.error", "dbos")
_SILENCED_LOGGERS = ("uvicorn.access",)
_OPTIONAL_KEYS = ("event", "run_id", "node_id", "attempt_id", "exception")


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line, with a readable sentence in ``message``.

    ``exception`` renders from the record's ``exc_info``/``stack_info`` when
    the call site did not already set it via ``extra``.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _OPTIONAL_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if "exception" not in payload:
            rendered = self._render_traceback(record)
            if rendered is not None:
                payload["exception"] = rendered
        return json.dumps(payload, ensure_ascii=False)

    def _render_traceback(self, record: logging.LogRecord) -> str | None:
        parts = []
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
            parts.append(record.exc_text)
        if record.stack_info:
            parts.append(self.formatStack(record.stack_info))
        return "\n".join(parts) if parts else None


def configure_process_logging(stream: TextIO | None = None) -> None:
    """Install the one JSON stderr handler every process logger shares.

    Call this as the first action of ``serve()``, before
    ``compose_application`` constructs DBOS. DBOS installs its own text
    handler only when ``dbos`` has none; uvicorn rewrites logging unless
    ``log_config`` is left unset. Access lines have no reader and stay off.
    """

    target = sys.stderr if stream is None else stream
    handler = logging.StreamHandler(target)
    handler.set_name(_SHARED_HANDLER_NAME)
    handler.setFormatter(JsonLogFormatter())

    for name in _UNIFIED_LOGGERS:
        logger = logging.getLogger(name)
        _replace_handlers(logger, handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logger.disabled = False

    for name in _SILENCED_LOGGERS:
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = False
        logger.disabled = True


def _replace_handlers(logger: logging.Logger, handler: logging.Handler) -> None:
    logger.handlers.clear()
    logger.addHandler(handler)
