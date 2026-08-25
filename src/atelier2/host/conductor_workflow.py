"""The conductor workflow: the operator's brief becomes one episodic run.

This module owns the conductor's product facts (#7): its catalog name, which
atelier doors its agent is granted, and the one-node workflow document a
deployment publishes for it. The conductor is a provider-neutral workflow --
an agent node that chooses, starts and observes catalog workflows through the
product's own MCP doors -- and deliberately NOT a privileged layer: the doors
it operates are the same public loopback API every client uses, and which
provider fulfils the grant is a binding decision (`AgentConfigurationRevision`
naming a doors-capable executor revision), never this document's.

The door grant is the three read-and-start doors only. `answer_wait` and
`publish_artifact` are deliberately absent: humans answer the waits of started
runs (the workbench surfaces them), and a choose/start/observe role needs no
write primitive. The grant is spelled here from the door vocabulary's own typed
owner (`atelier2.host.mcp_tools`), never as re-spelled literals.

The chat feed model (#7, decided 25.08.): each workbench message is ONE
episodic conductor run. The message and the bounded prior transcript travel as
the typed `brief` run input (`CONDUCTOR_BRIEF_SCHEMA`), and the episode's
terminal `report` output (`CONDUCTOR_REPORT_SCHEMA`) carries the reply back
into the workbench stream. Both canonical schema documents live here so the
instruction, the schemas and their publisher can never drift apart -- the
probe proved exactly that drift refuses every real run.

RECURSION FENCE, slice 1. A conductor that can start catalog workflows must
not start itself: a conductor starting conductors would be an unbounded billed
tree behind a single brief. This slice fences that at the document: the
instruction carries the refusal rule in the agent's own orders, and
`conductor_workflow_document` refuses to build a document that lost the rule or
that authors a start order naming the conductor. The real fence -- actor-chain
attribution and spend/depth bounds on started-run trees -- is ADR 0009 §9 and
stays a named open edge, not this slice's claim.
"""

from __future__ import annotations

import json

from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.contracts.workflows_v3 import AgentNodeV3, WorkflowGraphV3
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

CONDUCTOR_BRIEF_INPUT = "brief"
_REPORT_OUTPUT = "report"

# The speakers a chat transcript carries into the brief. The workbench maps its
# own rendering labels onto these tokens; they are the brief contract's, not the
# UI's.
CONDUCTOR_OPERATOR_SPEAKER = "operator"
CONDUCTOR_CONDUCTOR_SPEAKER = "conductor"

_REPORT_ANSWER_FIELD = "answer"
_REPORT_STARTED_RUN_IDS_FIELD = "started_run_ids"


def _canonical_schema_bytes(schema: dict[str, object]) -> bytes:
    return json.dumps(schema, sort_keys=True, separators=(",", ":")).encode()


# The brief a workbench message becomes: the message itself, the bounded prior
# transcript that gives it context, and the count of oldest messages the byte
# ceiling forced out -- carried in the structure itself, so a conductor episode
# never mistakes a truncated conversation for a whole one. As an inline run
# order the whole brief is bounded by `MAXIMUM_INSTANCE_DOCUMENT_BYTES`
# (`atelier2.contracts.schemas_v3`); the sender truncates oldest-first to fit.
CONDUCTOR_BRIEF_SCHEMA = _canonical_schema_bytes(
    {
        "type": "object",
        "required": ["message", "prior_transcript", "dropped_oldest_messages"],
        "additionalProperties": False,
        "properties": {
            "message": {"type": "string", "minLength": 1},
            "prior_transcript": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["speaker", "text"],
                    "additionalProperties": False,
                    "properties": {
                        "speaker": {
                            "enum": [
                                CONDUCTOR_OPERATOR_SPEAKER,
                                CONDUCTOR_CONDUCTOR_SPEAKER,
                            ]
                        },
                        "text": {"type": "string"},
                    },
                },
            },
            "dropped_oldest_messages": {"type": "integer", "minimum": 0},
        },
    }
)

# The report an episode must answer with: the reply the workbench renders, and
# every run the episode started, so the stream can say honestly what one
# message set in motion. JSON on purpose -- the probe (#7, 25.08.) proved that
# a prose instruction under a JSON output schema refuses every real run
# (`output-schema-refused: instance-not-json`), so schema and instruction are
# built here from the same two field names. The instruction below still asks
# for one bare JSON object, but no episode's correctness rests on the model
# obeying that sentence: asking in prose made the same brief succeed or refuse
# by coin flip (#663), and what an answer's declared value is now has an owner
# in the executor decode path (`declared_instance_in_answer`).
CONDUCTOR_REPORT_SCHEMA = _canonical_schema_bytes(
    {
        "type": "object",
        "required": [_REPORT_ANSWER_FIELD, _REPORT_STARTED_RUN_IDS_FIELD],
        "additionalProperties": False,
        "properties": {
            _REPORT_ANSWER_FIELD: {"type": "string", "minLength": 1},
            _REPORT_STARTED_RUN_IDS_FIELD: {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
        },
    }
)

# The fence sentence travels inside the instruction so every published revision
# carries its own rule; the builder below refuses a document that lost it.
_RECURSION_FENCE_SENTENCE = (
    f"Never start the workflow named '{CONDUCTOR_WORKFLOW_NAME}'; refuse a "
    "brief that asks for it."
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
        "Read the brief: its message is the operator's current request, its "
        "prior_transcript the conversation so far. Using only the atelier "
        f"door tools {McpToolName.LIST_WORKFLOWS.value}, "
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
        "started, an empty array when none}."
    )


def conductor_workflow_document(
    brief_schema_revision: str, report_schema_revision: str
) -> bytes:
    """The publishable conductor document, validated against its own contract.

    The two schema revisions are published-catalog facts the caller resolves
    (the hash of the published schema the brief and the report agree to), so
    they arrive as parameters rather than being invented here. The description
    names the run-starting power in plain words, because a reviewer reading the
    catalog must see the side effect where the document is read -- and it is
    the copy the run view shows the operator (#654), so it speaks in human
    words while the agent's orders stay in the instruction.
    """

    instruction = _conductor_instruction()
    document = f"""format_version: 3
name: {CONDUCTOR_WORKFLOW_NAME}
description: >-
  Answers your workshop message: it reads the workflow catalog, starts the
  real run you ask for, and reports back with the run reference.
graph_inputs:
  - name: {CONDUCTOR_BRIEF_INPUT}
    schema: {{ref: conductor-brief, revision: "{brief_schema_revision}"}}
nodes:
  - id: conduct
    type: agent
    role: {CONDUCTOR_ROLE}
    mode: headless_with_tools
    instruction: >-
      {instruction}
    inputs:
      - name: {CONDUCTOR_BRIEF_INPUT}
        from: {{graph_input: {CONDUCTOR_BRIEF_INPUT}}}
    outputs:
      - name: {_REPORT_OUTPUT}
        schema: {{ref: conductor-report, revision: "{report_schema_revision}"}}
""".encode()
    require_conductor_document(document)
    return document


def require_conductor_document(document: bytes) -> None:
    """Refuse a conductor document that violates the conductor's own rules.

    The builder validates its own product so an edit of the constants above
    cannot silently ship a conductor without its fence or with an authored
    order that starts the conductor itself, and whoever publishes a conductor
    revision validates through the same door. Parsing uses the production
    grammar, so a document this refusal passes is one the catalog can admit.
    """

    parsed = parse_workflow_document(document)
    if not isinstance(parsed, WorkflowGraphV3):
        raise ConductorDocumentDefect("the conductor document must be format V3")
    agent_nodes = [node for node in parsed.nodes if isinstance(node, AgentNodeV3)]
    if len(parsed.nodes) != 1 or len(agent_nodes) != 1:
        raise ConductorDocumentDefect(
            "the conductor document must hold exactly one agent node"
        )
    node = agent_nodes[0]
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
    for report_field in (_REPORT_ANSWER_FIELD, _REPORT_STARTED_RUN_IDS_FIELD):
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
