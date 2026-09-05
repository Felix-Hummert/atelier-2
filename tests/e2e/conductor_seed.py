"""The e2e harness's own copy of the conductor conversation-loop document.

`src/atelier2/host/conductor_workflow.py` no longer builds this document
(operator ruling 05.09.2026, audit #1244 finding 10): no production caller
ever published it, only tests did. The Workbench chat surface stays live
until #1099's terminal seat replaces it, so `workbench-conductor.spec.ts`
still needs one served instance to publish a real "conductor" catalog
revision it can connect to and drive a conversation against -- this module
is that revision's only owner now, sized to exactly what the spec drives:
the wait/agent node ids and message-schema kind `conductorConversationShape`
(`frontend/src/lib/conductorEpisode.ts`) reads to recognize the episode, the
report fields `readableWaitAnswer` (`conductorConversation.ts`) reads back,
and the round ceiling the round-cap spec asserts by its literal number.
"""

from __future__ import annotations

import json

CONDUCTOR_WORKFLOW_NAME = "conductor"
CONDUCTOR_ROLE = "conductor"

CONDUCTOR_WAIT_NODE_ID = "next_message"
CONDUCTOR_AGENT_NODE_ID = "conduct"
CONDUCTOR_LOOP_ID = "conversation"

# `workbench-conductor.spec.ts` asserts this exact number as the round the
# conversation ends on and the round a 25th message starts fresh past.
CONDUCTOR_LOOP_MAXIMUM_ROUNDS = 24

CONDUCTOR_MESSAGE_OUTPUT = "message"
_REPORT_OUTPUT = "report"
_PREVIOUS_REPORT_INPUT = "previous_report"

_REPORT_ANSWER_FIELD = "answer"
_REPORT_STARTED_RUN_IDS_FIELD = "started_run_ids"
_REPORT_CARRIED_CONTEXT_FIELD = "carried_context"
_REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD = "carried_context_truncated"


def _canonical_schema_bytes(schema: dict[str, object]) -> bytes:
    return json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()


# `conductorConversationShape` classifies the wait's answer schema as
# "string" only for exactly this shape.
CONDUCTOR_MESSAGE_SCHEMA = _canonical_schema_bytes({"type": "string", "minLength": 1})

# What `CONDUCTOR_FAKE_REPORT` (`serve_cockpit.py`) answers with, and what
# `readableWaitAnswer` reads `answer` back out of.
CONDUCTOR_REPORT_SCHEMA = _canonical_schema_bytes(
    {
        "type": "object",
        "required": [
            _REPORT_ANSWER_FIELD,
            _REPORT_STARTED_RUN_IDS_FIELD,
            _REPORT_CARRIED_CONTEXT_FIELD,
            _REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD,
        ],
        "additionalProperties": False,
        "properties": {
            _REPORT_ANSWER_FIELD: {"type": "string", "minLength": 1},
            _REPORT_STARTED_RUN_IDS_FIELD: {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            _REPORT_CARRIED_CONTEXT_FIELD: {"type": "string"},
            _REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD: {"type": "boolean"},
        },
    }
)

# The e2e harness's executor is the fixed-answer fake `RecordingAgentExecutorFactoryV2`
# (`serve_cockpit.py`), never a real model reading this text, so it only needs
# to be a real, nonempty instruction -- not the product's own conductor orders.
_INSTRUCTION = (
    "You run one round of an ongoing conversation loop. Read the operator's "
    f"message this round from the {CONDUCTOR_WAIT_NODE_ID!r} answer and "
    "answer with exactly one JSON object naming "
    f'"{_REPORT_ANSWER_FIELD}", "{_REPORT_STARTED_RUN_IDS_FIELD}", '
    f'"{_REPORT_CARRIED_CONTEXT_FIELD}" and '
    f'"{_REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD}".'
)


def conductor_workflow_document(
    message_schema_revision: str, report_schema_revision: str
) -> bytes:
    """The e2e harness's publishable conductor document.

    The two schema revisions are published-catalog facts the caller resolves
    (the hash of the published schema the wait answer and the report agree
    to), so they arrive as parameters rather than being invented here.
    """

    return f"""format_version: 3
name: {CONDUCTOR_WORKFLOW_NAME}
description: >-
  Answers your workshop messages round after round: it reads what you just
  said, starts the real run you ask for, and reports back with the run
  reference -- up to {CONDUCTOR_LOOP_MAXIMUM_ROUNDS} rounds per conversation.
nodes:
  - id: {CONDUCTOR_WAIT_NODE_ID}
    type: wait
    prompt: What would you like the conductor to do?
    outputs:
      - name: {CONDUCTOR_MESSAGE_OUTPUT}
        schema: {{ref: conductor-message, revision: "{message_schema_revision}"}}
  - id: {CONDUCTOR_AGENT_NODE_ID}
    type: agent
    role: {CONDUCTOR_ROLE}
    mode: headless_with_tools
    instruction: >-
      {_INSTRUCTION}
    depends_on: [{CONDUCTOR_WAIT_NODE_ID}]
    inputs:
      - name: {CONDUCTOR_MESSAGE_OUTPUT}
        from: {{node: {CONDUCTOR_WAIT_NODE_ID}, output: {CONDUCTOR_MESSAGE_OUTPUT}}}
      - name: {_PREVIOUS_REPORT_INPUT}
        from: {{node: {CONDUCTOR_AGENT_NODE_ID}, output: {_REPORT_OUTPUT}}}
    outputs:
      - name: {_REPORT_OUTPUT}
        schema: {{ref: conductor-report, revision: "{report_schema_revision}"}}
loops:
  - id: {CONDUCTOR_LOOP_ID}
    body: [{CONDUCTOR_WAIT_NODE_ID}, {CONDUCTOR_AGENT_NODE_ID}]
    maximum_rounds: {CONDUCTOR_LOOP_MAXIMUM_ROUNDS}
""".encode()
