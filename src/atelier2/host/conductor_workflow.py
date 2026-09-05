"""The conductor's door grant (#7): the atelier doors its agent is granted.

The conductor is a provider-neutral role -- an agent node that chooses,
starts and observes catalog workflows through the product's own MCP doors --
and deliberately NOT a privileged layer: the doors it operates are the same
public loopback API every client uses, and which provider fulfils the grant
is a binding decision (`AgentConfigurationRevision` naming a doors-capable
executor revision), never this module's.

The door grant is the three read-and-start doors only. `answer_wait` and
`publish_artifact` are deliberately absent: humans answer the waits of started
runs (the workbench surfaces them), and a choose/start/observe role needs no
write primitive. The grant is spelled here from the door vocabulary's own typed
owner (`atelier2.host.mcp_tools`), never as re-spelled literals.

Operator ruling 05.09.2026 (audit #1244 finding 10): the conductor's own
conversation-loop workflow document (`loop{wait, agent}`, its report schema,
its round ceiling) was never published to the live catalog -- only tests
built and validated it -- and #1078 closed without picking that build back
up, so it is deleted rather than kept parked. What stays live is this door
grant, which the workbench's Claude atelier-doors executor already serves.
"""

from __future__ import annotations

from atelier2.host.mcp_tools import MCP_SERVER_NAME, McpToolName

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
