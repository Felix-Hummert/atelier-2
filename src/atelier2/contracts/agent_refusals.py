"""The second truth an agent may say: a named refusal, never a verdict.

A verdict steers a loop. A refusal ends the attempt. Mixing them would let an
agent call ``revise`` when it meant ``the order is unclear``, or call a
schema-refused payload a named refusal. This module owns only the refusal
answer: one required reason sentence, judged by a schema derived from that
shape, the same way ``verdicts`` owns ``accepted|revise``.
"""

from __future__ import annotations

import json

from atelier2.contracts.agents import MAXIMUM_AGENT_FIELD_CHARACTERS
from atelier2.contracts.revisions_v3 import PublishedRevision, RevisionKind

AGENT_REFUSAL_FIELD = "reason"
"""The one member of a named agent refusal, owned here for the schema and the reader."""


def _agent_refusal_schema_document() -> bytes:
    return json.dumps(
        {
            "additionalProperties": False,
            "properties": {
                AGENT_REFUSAL_FIELD: {
                    "maxLength": MAXIMUM_AGENT_FIELD_CHARACTERS,
                    "minLength": 1,
                    "type": "string",
                }
            },
            "required": [AGENT_REFUSAL_FIELD],
            "type": "object",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


AGENT_REFUSAL_SCHEMA = PublishedRevision(
    RevisionKind.SCHEMA, _agent_refusal_schema_document()
)
"""The product-owned contract a declared refusal answer is judged by."""


def agent_refusal_reason(answer: bytes) -> str | None:
    """The reason sentence these bytes carry, or None when they are not this form."""

    try:
        decoded = json.loads(answer)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(decoded, dict) or set(decoded) != {AGENT_REFUSAL_FIELD}:
        return None
    reason = decoded[AGENT_REFUSAL_FIELD]
    if (
        not isinstance(reason, str)
        or not 1 <= len(reason) <= MAXIMUM_AGENT_FIELD_CHARACTERS
    ):
        return None
    return reason
