"""The conductor workflow: a conversation is one loop document, not a run per message.

This module owns the conductor's product facts (#7): its catalog name, which
atelier doors its agent is granted, and the loop document a deployment
publishes for it. The conductor is a provider-neutral workflow -- an agent
node that chooses, starts and observes catalog workflows through the
product's own MCP doors -- and deliberately NOT a privileged layer: the doors
it operates are the same public loopback API every client uses, and which
provider fulfils the grant is a binding decision (`AgentConfigurationRevision`
naming a doors-capable executor revision), never this document's.

The door grant is the three read-and-start doors only. `answer_wait` and
`publish_artifact` are deliberately absent: humans answer the waits of started
runs (the workbench surfaces them), and a choose/start/observe role needs no
write primitive. The grant is spelled here from the door vocabulary's own typed
owner (`atelier2.host.mcp_tools`), never as re-spelled literals.

The conversation model (#658, revised 25.08.): a conversation is ONE run, and
each operator message is a round of `loop{next_message: wait, conduct: agent}`
-- never a run of its own. The run's very first node is the Wait a person
answers; that answer becomes this round's `message` input to the agent, which
also reads its own report from the round before (`previous_report`, the
previous-round self-edge `is_previous_round_data_edge` carries), honestly
absent in round one. There is no reloaded transcript: what the agent
remembers across rounds is exactly what its own last report's
`carried_context` says, bounded by `CONDUCTOR_CARRIED_CONTEXT_MAXIMUM_LENGTH`
and marked honestly when that bound forced a cut. `CONDUCTOR_LOOP_MAXIMUM_ROUNDS`
caps the conversation at 24 rounds; round 25 starts a new, unrelated run.

RECURSION FENCE, slice 1. A conductor that can start catalog workflows must
not start itself: a conductor starting conductors would be an unbounded billed
tree behind a single message. This slice fences that at the document: the
instruction carries the refusal rule in the agent's own orders, and
`require_conductor_document` refuses to build a document that lost the rule or
that authors a start order naming the conductor. The real fence -- actor-chain
attribution and spend/depth bounds on started-run trees -- is ADR 0009 §9 and
stays a named open edge, not this slice's claim.
"""

from __future__ import annotations

import json

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.contracts.workflows_v3 import AgentNodeV3, WaitNodeV3, WorkflowGraphV3
from atelier2.host.mcp_tools import MCP_SERVER_NAME, McpToolName

CONDUCTOR_WORKFLOW_NAME = "conductor"
CONDUCTOR_ROLE = "conductor"

# The doors the conductor's agent is granted, drawn from the door vocabulary's
# typed owner. Choosing THIS subset is the conductor's own product decision and
# therefore lives here: list, start, observe -- and nothing that answers or
# writes.
CONDUCTOR_DOOR_TOOLS: tuple[McpToolName, ...] = (
    McpToolName.LIST_WORKFLOWS,
    McpToolName.START_RUN,
    McpToolName.RUN_STATUS,
)

CONDUCTOR_DOOR_SERVER_NAME = MCP_SERVER_NAME

CONDUCTOR_WAIT_NODE_ID = "next_message"
CONDUCTOR_AGENT_NODE_ID = "conduct"
CONDUCTOR_LOOP_ID = "conversation"

CONDUCTOR_LOOP_MAXIMUM_ROUNDS = 24
"""The conversation's own round ceiling, named at the document that owns it.

After round 24 the run ends COMPLETED; a 25th message starts a new run with no
link to the old one (#658). A conversation nobody can pay for is a conversation
nobody can bound, so the cap is a document fact, not a runtime default."""

CONDUCTOR_MESSAGE_OUTPUT = "message"
_REPORT_OUTPUT = "report"
_PREVIOUS_REPORT_INPUT = "previous_report"

_REPORT_ANSWER_FIELD = "answer"
_REPORT_STARTED_RUN_IDS_FIELD = "started_run_ids"
_REPORT_CARRIED_CONTEXT_FIELD = "carried_context"
_REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD = "carried_context_truncated"

CONDUCTOR_CARRIED_CONTEXT_MAXIMUM_LENGTH = 12_288
"""The bound on what one round's report carries forward to the next round,
in JSON Schema `maxLength` characters (code points), not bytes.

Well under `MAXIMUM_AGENT_OUTPUT_BYTES_V2` (49_152, the whole report's own
ceiling as raw agent output) and, for plain ASCII text, comfortably under
`MAXIMUM_INSTANCE_DOCUMENT_BYTES` (16_384, `atelier2.contracts.schemas_v3`),
the report's own instance-document byte ceiling once it is the declared
output. The two ceilings measure different things, so this length alone does
not guarantee the byte door admits the report: a `carried_context` near this
bound that is heavy with characters JSON must escape (raw newlines, quotes) or
with multibyte characters can still push the whole instance past
`MAXIMUM_INSTANCE_DOCUMENT_BYTES` and be refused loud
(`InstanceRefusal.INSTANCE_TOO_LARGE`) rather than silently truncated. That gap
is named on #658 with an owner, not hardened here."""


def _canonical_schema_bytes(schema: dict[str, object]) -> bytes:
    return json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()


# The Wait node's one output: the operator's message this round, verbatim. No
# transcript travels with it -- what the agent remembers of earlier rounds is
# carried forward only through its own previous report (see below).
CONDUCTOR_MESSAGE_SCHEMA = _canonical_schema_bytes({"type": "string", "minLength": 1})

# The report a round must answer with: the reply the workbench renders, every
# run the round started, and the honest state of the agent's own memory. JSON
# on purpose -- the probe (#7, 25.08.) proved that a prose instruction under a
# JSON output schema refuses every real run (`output-schema-refused:
# instance-not-json`), so schema and instruction are built here from the same
# field names. `carried_context` is this round's only handoff to the next --
# there is no reloaded transcript -- and `carried_context_truncated` is the
# honesty marker: true only when this round's carried_context had to drop
# something the full conversation held, so a next round (or an operator
# reading the receipt) can see a shortened memory rather than mistake it for
# a whole one.
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
            _REPORT_CARRIED_CONTEXT_FIELD: {
                "type": "string",
                "maxLength": CONDUCTOR_CARRIED_CONTEXT_MAXIMUM_LENGTH,
            },
            _REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD: {"type": "boolean"},
        },
    }
)

# The fence sentence travels inside the instruction so every published revision
# carries its own rule; the builder below refuses a document that lost it.
_RECURSION_FENCE_SENTENCE = (
    f"Never start the workflow named '{CONDUCTOR_WORKFLOW_NAME}'; refuse a "
    "message that asks for it."
)


class ConductorDocumentDefect(ValueError):
    """The built conductor document violates the conductor's own contract."""


def _conductor_instruction() -> str:
    """The thin orchestration instruction, and only that.

    No persona and no system behaviour: those belong to the published agent
    definition (#66), and a second owner here would drift against it. The
    instruction names the granted doors from their typed owner and carries the
    recursion fence in the agent's own orders.
    """

    return (
        "You run one round of an ongoing conversation loop. Read the "
        f"operator's message this round from the {CONDUCTOR_WAIT_NODE_ID!r} "
        f"answer, and, from a previous round, your own last report as "
        f"{_PREVIOUS_REPORT_INPUT} -- absent in the conversation's first "
        "round. There is no other transcript: what you remember of earlier "
        f"rounds is exactly what your own last {_REPORT_CARRIED_CONTEXT_FIELD} "
        "said. Using only the atelier door tools "
        f"{McpToolName.LIST_WORKFLOWS.value}, "
        f"{McpToolName.START_RUN.value} and {McpToolName.RUN_STATUS.value}: "
        "when the message asks for catalog work, list the catalog, start the "
        "workflow it asks for once with a fresh run_id, and read its status. "
        "Start nothing the message does not ask for. Offer only workflows the "
        f"{McpToolName.LIST_WORKFLOWS.value} result actually names; never "
        "promise help with a workflow the catalog does not hold. "
        f"{_RECURSION_FENCE_SENTENCE} You never answer waits of started runs; "
        "humans do. Answer with exactly one JSON object and no other text: "
        f'{{"{_REPORT_ANSWER_FIELD}": a short reply to the operator, naming '
        "any started run reference and its state, "
        f'"{_REPORT_STARTED_RUN_IDS_FIELD}": the run_id of every run you '
        f'started, an empty array when none, "{_REPORT_CARRIED_CONTEXT_FIELD}": '
        "what you need to remember of this conversation for your own next "
        f"round, at most {CONDUCTOR_CARRIED_CONTEXT_MAXIMUM_LENGTH} "
        f'characters, "{_REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD}": true only '
        "when that carried context had to drop something the conversation so "
        "far held, false when it is complete}."
    )


def conductor_workflow_document(
    message_schema_revision: str, report_schema_revision: str
) -> bytes:
    """The publishable conductor document, validated against its own contract.

    The two schema revisions are published-catalog facts the caller resolves
    (the hash of the published schema the wait answer and the report agree
    to), so they arrive as parameters rather than being invented here. The
    description names the run-starting power in plain words, because a
    reviewer reading the catalog must see the side effect where the document
    is read -- and it is the copy the run view shows the operator (#654), so
    it speaks in human words while the agent's orders stay in the instruction.
    """

    instruction = _conductor_instruction()
    document = f"""format_version: 3
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
      {instruction}
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
    require_conductor_document(document)
    return document


def require_conductor_document(document: bytes) -> None:
    """Refuse a conductor document that violates the conductor's own rules.

    The builder validates its own product so an edit of the constants above
    cannot silently ship a conductor without its fence, without its round
    ceiling, or with an authored order that starts the conductor itself, and
    whoever publishes a conductor revision validates through the same door.
    Parsing uses the production grammar, so a document this refusal passes is
    one the catalog can admit.
    """

    parsed = parse_workflow_document(document)
    if not isinstance(parsed, WorkflowGraphV3):
        raise ConductorDocumentDefect("the conductor document must be format V3")

    wait_nodes = [node for node in parsed.nodes if isinstance(node, WaitNodeV3)]
    agent_nodes = [node for node in parsed.nodes if isinstance(node, AgentNodeV3)]
    if len(parsed.nodes) != 2 or len(wait_nodes) != 1 or len(agent_nodes) != 1:
        raise ConductorDocumentDefect(
            "the conductor document must hold exactly one wait node and one "
            "agent node: one round of its conversation loop"
        )
    node = agent_nodes[0]

    if len(parsed.loops) != 1:
        raise ConductorDocumentDefect(
            "the conductor document must repeat its round as exactly one loop"
        )
    loop = parsed.loops[0]
    if loop.body != (wait_nodes[0].id, node.id):
        raise ConductorDocumentDefect(
            "the conductor loop must enter at the wait node and close at the "
            "agent node, in that order"
        )
    if loop.maximum_rounds != CONDUCTOR_LOOP_MAXIMUM_ROUNDS:
        raise ConductorDocumentDefect(
            "the conductor loop must cap its rounds at the conductor's own "
            f"named ceiling ({CONDUCTOR_LOOP_MAXIMUM_ROUNDS})"
        )
    if loop.repeat_while is not None:
        raise ConductorDocumentDefect(
            "the conductor conversation ends only at its round ceiling, never "
            "on a verdict"
        )

    if _RECURSION_FENCE_SENTENCE not in node.instruction:
        raise ConductorDocumentDefect(
            "the conductor instruction must carry the recursion fence: "
            f"{_RECURSION_FENCE_SENTENCE!r}"
        )
    if node.mode != "headless_with_tools":
        # The doors grant is a tool-bearing call; a `headless` node could never
        # bind a doors-capable executor (`_refuse_incompatible_mode`,
        # `atelier2.contracts.capabilities_v3`) -- the defect the first landed
        # document shipped with.
        raise ConductorDocumentDefect(
            "the conductor node must run headless_with_tools: its doors grant "
            "is a tool-bearing call"
        )
    for report_field in (
        _REPORT_ANSWER_FIELD,
        _REPORT_STARTED_RUN_IDS_FIELD,
        _REPORT_CARRIED_CONTEXT_FIELD,
        _REPORT_CARRIED_CONTEXT_TRUNCATED_FIELD,
    ):
        if f'"{report_field}"' not in node.instruction:
            # The probe (#7, 25.08.) proved a prose instruction under the JSON
            # report schema refuses every real run; the builder refuses to
            # rebuild that drift.
            raise ConductorDocumentDefect(
                "the conductor instruction must demand the JSON report field "
                f"{report_field!r} its output schema enforces"
            )
    authored_values = [
        node_input.value for node_input in node.inputs if node_input.value is not None
    ]
    if any(CONDUCTOR_WORKFLOW_NAME in value for value in authored_values):
        raise ConductorDocumentDefect(
            "the conductor document must not author an order naming the "
            "conductor itself: a conductor starting conductors is an unbounded "
            "billed tree"
        )
