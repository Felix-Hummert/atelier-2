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

_BRIEF_INPUT = "brief"
_REPORT_OUTPUT = "report"

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
        "Read the brief. Using only the atelier door tools "
        f"{McpToolName.LIST_WORKFLOWS.value}, {McpToolName.START_RUN.value} "
        f"and {McpToolName.RUN_STATUS.value}: list the catalog, choose the "
        "workflow the brief asks for, start it once with a fresh run_id, read "
        "its status, and answer with a short report naming the started run "
        "reference and its state. Start nothing the brief does not ask for. "
        f"{_RECURSION_FENCE_SENTENCE} You never answer waits of started runs; "
        "humans do."
    )


def conductor_workflow_document(
    brief_schema_revision: str, report_schema_revision: str
) -> bytes:
    """The publishable conductor document, validated against its own contract.

    The two schema revisions are published-catalog facts the caller resolves
    (the hash of the published schema the brief and the report agree to), so
    they arrive as parameters rather than being invented here. The description
    names the run-starting power in plain words, because a reviewer reading the
    catalog must see the side effect where the document is read.
    """

    instruction = _conductor_instruction()
    document = f"""format_version: 3
name: {CONDUCTOR_WORKFLOW_NAME}
description: >-
  The conductor. Its agent operates the atelier's own doors and STARTS REAL
  CATALOG RUNS from the operator's brief: it lists the catalog, starts the
  workflow the brief asks for, observes it, and reports the run reference.
  It never starts the conductor itself, and it cannot answer waits or publish
  artifacts.
graph_inputs:
  - name: {_BRIEF_INPUT}
    schema: {{ref: conductor-brief, revision: "{brief_schema_revision}"}}
nodes:
  - id: conduct
    type: agent
    role: {CONDUCTOR_ROLE}
    mode: headless
    instruction: >-
      {instruction}
    inputs:
      - name: {_BRIEF_INPUT}
        from: {{graph_input: {_BRIEF_INPUT}}}
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
    authored_values = [
        node_input.value for node_input in node.inputs if node_input.value is not None
    ]
    if any(CONDUCTOR_WORKFLOW_NAME in value for value in authored_values):
        raise ConductorDocumentDefect(
            "the conductor document must not author an order naming the "
            "conductor itself: a conductor starting conductors is an unbounded "
            "billed tree"
        )
