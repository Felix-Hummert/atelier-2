"""The Claude atelier-doors executor: its vector, its identity, its gates.

Every proof here is deterministic and unbilled. The billed conformance probe --
a door really fires, a built-in stays imitation-only beside a live MCP server,
no customization returns through safe mode's absence -- is a separate
operator-gated step and deliberately has no test here: an offline test claiming
it would lie.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from atelier2.adapters.claude_subscription import (
    CLAUDE_ATELIER_DOORS_EXECUTOR_KEY,
    CLAUDE_ATELIER_DOORS_OPERATIONAL_IDENTITY,
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    CLAUDE_SUBSCRIPTION_FRAME_BYTES,
    CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY,
    ClaudeAtelierDoorsExecutorFactory,
    ClaudeAtelierDoorsSettings,
    ClaudeExecutableUnsupported,
    ClaudeSubscriptionAuthModeUnsupported,
    attest_atelier_doors_invocation,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.host.conductor_workflow import (
    CONDUCTOR_DOOR_SERVER_NAME,
    CONDUCTOR_DOOR_TOOLS,
)
from atelier2.host.mcp_tools import MCP_SERVER_NAME, McpToolName
from tests.integration.test_claude_subscription import (
    INTROSPECTING_CLAUDE,
    argument_after,
    launched,
    leased,
    parsing_claude,
    provider_workspace,
)
from tests.scenarios.agents import claude_subscription_deployment

_LOOPBACK_SERVICE_URL = "http://127.0.0.1:8422"


def doors_deployment(root: Path, name: str, program: str) -> ClaudeAtelierDoorsSettings:
    """One atelier-doors deployment composed the way the serving host does.

    The server name and door tools come from the conductor contract's typed
    owners, and the door command launches this test's interpreter as the stdio
    door -- the same shape `_atelier_doors_settings` composes in production.
    """

    directory = root / name
    directory.mkdir()
    door_command = (
        "/usr/bin/env",
        "python3",
        "-m",
        "atelier2",
        "mcp",
        "--service",
        _LOOPBACK_SERVICE_URL,
    )
    return ClaudeAtelierDoorsSettings(
        claude_subscription_deployment(directory, program),
        CONDUCTOR_DOOR_SERVER_NAME,
        tuple(tool.value for tool in CONDUCTOR_DOOR_TOOLS),
        door_command,
    )


def doors_request(
    model: str = "claude-opus-4-6",
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION,
    job: bytes = b"choose a workflow, start it, and report the run",
    maximum_assistant_turns: int | None = None,
) -> AgentExecutionRequestV2:
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), auth_mode)
    configuration = AgentConfigurationRevision(
        model,
        auth.revision_hash,
        CLAUDE_ATELIER_DOORS_EXECUTOR_KEY.executor_revision,
        AgentExecutionCapability.HEADLESS_WITH_TOOLS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    run_id = RunId("run-conductor")
    revision_hash = WorkflowRevisionHash("3" * 64)
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, "conduct"),
        run_id,
        revision_hash,
        "conduct",
        ResolvedAgentBinding(AgentRole("conductor"), configuration, auth),
        CLAUDE_ATELIER_DOORS_OPERATIONAL_IDENTITY,
        job,
        maximum_assistant_turns=maximum_assistant_turns,
    )


def doors_flags(settings: ClaudeAtelierDoorsSettings) -> tuple[str, ...]:
    """Every flag the real atelier-doors invocation carries."""

    command = (
        ClaudeAtelierDoorsExecutorFactory(settings)
        .open()
        .prepare_process(doors_request())
    )
    return tuple(
        argument for argument in command.arguments if argument.startswith("--")
    )


@pytest.mark.proves("the-doors-vector-admits-exactly-the-granted-doors")
def test_the_doors_vector_admits_exactly_the_granted_doors(tmp_path: Path) -> None:
    """The containment is the vector, and every piece of it is asserted exactly.

    Beyond the gate's link: the allowlist is derived from the door vocabulary's
    typed owner and admits neither a built-in nor either write-shaped door, the
    one MCP server is the serving host's own door command, and `--safe-mode` is
    absent because it was measured to prevent that server from spawning at all.
    """

    settings = doors_deployment(tmp_path, "deployment", INTROSPECTING_CLAUDE)
    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()
    request = doors_request(model="claude-sonnet-4-6", job=b"start the build")

    command = executor.prepare_process(request)

    allowlist = ",".join(
        f"mcp__{MCP_SERVER_NAME}__{tool.value}" for tool in CONDUCTOR_DOOR_TOOLS
    )
    assert command.arguments == (
        str(settings.deployment.executable),
        "-p",
        "--output-format",
        "json",
        "--model",
        "claude-sonnet-4-6",
        "--tools=",
        "--allowedTools",
        allowlist,
        "--setting-sources=",
        "--strict-mcp-config",
        "--mcp-config",
        settings.door_mcp_config(),
        "--disable-slash-commands",
        "--no-chrome",
        "--no-session-persistence",
        "--max-turns",
        "6",
    )
    assert "--safe-mode" not in command.arguments
    for tool in (McpToolName.ANSWER_WAIT, McpToolName.PUBLISH_ARTIFACT):
        assert tool.value not in allowlist
    for built_in in ("Bash", "Edit", "Glob", "Grep", "Read", "Write"):
        assert built_in not in allowlist
    door = json.loads(settings.door_mcp_config())
    assert set(door) == {"mcpServers"}
    assert set(door["mcpServers"]) == {MCP_SERVER_NAME}
    assert door["mcpServers"][MCP_SERVER_NAME] == {
        "command": settings.door_command[0],
        "args": list(settings.door_command[1:]),
    }
    assert command.environment == (
        ("CLAUDE_CONFIG_DIR", str(settings.deployment.credential_directory)),
        ("PATH", settings.deployment.search_path),
        ("CLAUDE_CODE_SKIP_PROMPT_HISTORY", "1"),
        ("CLAUDE_CODE_MAX_RETRIES", "0"),
        ("CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", "1"),
    )
    assert command.standard_output_frame_bytes == CLAUDE_SUBSCRIPTION_FRAME_BYTES
    assert all(argument for argument in command.arguments)

    workspace = provider_workspace(tmp_path)
    result = executor.decode_process_completion(
        leased(request, command, workspace), launched(command, workspace)
    )
    assert isinstance(result, AgentExecutionResult)
    observed = json.loads(result.output_bytes)
    assert observed["arguments"][1:] == list(command.arguments[1:])
    assert observed["job"] == "start the build"
    assert "HOME" not in observed["environment"]


def test_a_pinned_budget_replaces_the_default_turn_bound(tmp_path: Path) -> None:
    settings = doors_deployment(tmp_path, "deployment", INTROSPECTING_CLAUDE)
    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()

    default_command = executor.prepare_process(doors_request())
    pinned_command = executor.prepare_process(doors_request(maximum_assistant_turns=3))

    assert argument_after(default_command.arguments, "--max-turns") == "6"
    assert argument_after(pinned_command.arguments, "--max-turns") == "3"


def test_a_non_subscription_profile_reaches_no_door_bearing_process(
    tmp_path: Path,
) -> None:
    settings = doors_deployment(tmp_path, "deployment", INTROSPECTING_CLAUDE)
    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()

    with pytest.raises(ClaudeSubscriptionAuthModeUnsupported, match="subscription"):
        executor.prepare_process(doors_request(auth_mode=AuthMode.API_KEY))


def test_the_factory_offers_its_own_identity_beside_both_siblings(
    tmp_path: Path,
) -> None:
    """A third operation of one provider, not a revision of either sibling."""

    settings = doors_deployment(tmp_path, "deployment", INTROSPECTING_CLAUDE)
    factory = ClaudeAtelierDoorsExecutorFactory(settings)

    assert factory.key == CLAUDE_ATELIER_DOORS_EXECUTOR_KEY
    assert factory.key.provider_id == CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.provider_id
    assert factory.key.executor_revision not in {
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
        CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision,
    }
    assert factory.declared_capabilities == frozenset(
        {AgentExecutionCapability.HEADLESS_WITH_TOOLS}
    )
    assert factory.open().close() is None


def test_settings_refuse_an_empty_door_grant(tmp_path: Path) -> None:
    """A doors executor with no doors, no server or no command is not a deployment."""

    directory = tmp_path / "deployment"
    directory.mkdir()
    deployment = claude_subscription_deployment(directory, INTROSPECTING_CLAUDE)
    door_command = ("/usr/bin/env", "python3", "-m", "atelier2", "mcp")

    with pytest.raises(ValueError, match="door tools"):
        ClaudeAtelierDoorsSettings(deployment, MCP_SERVER_NAME, (), door_command)
    with pytest.raises(ValueError, match="server name"):
        ClaudeAtelierDoorsSettings(
            deployment, " ", (McpToolName.LIST_WORKFLOWS.value,), door_command
        )
    with pytest.raises(ValueError, match="door command"):
        ClaudeAtelierDoorsSettings(
            deployment, MCP_SERVER_NAME, (McpToolName.LIST_WORKFLOWS.value,), ()
        )


def test_an_executable_that_starts_this_exact_invocation_is_attested(
    tmp_path: Path,
) -> None:
    reference = doors_deployment(tmp_path, "reference", INTROSPECTING_CLAUDE)
    settings = doors_deployment(
        tmp_path, "deployment", parsing_claude(doors_flags(reference))
    )

    assert attest_atelier_doors_invocation(settings) is None


def test_an_executable_missing_any_flag_of_this_invocation_is_refused_by_that_flag(
    tmp_path: Path,
) -> None:
    """Every flag of the vector is a containment decision, so every one is probed."""

    reference = doors_deployment(tmp_path, "reference", INTROSPECTING_CLAUDE)
    flags = doors_flags(reference)

    assert flags
    for missing in flags:
        settings = doors_deployment(
            tmp_path,
            f"without{flags.index(missing)}",
            parsing_claude(flag for flag in flags if flag != missing),
        )

        with pytest.raises(ClaudeExecutableUnsupported, match=re.escape(missing)):
            attest_atelier_doors_invocation(settings)


def test_an_executable_that_never_names_an_unknown_flag_cannot_be_attested(
    tmp_path: Path,
) -> None:
    """Without the control, "said nothing" and "has nothing to say" look alike."""

    reference = doors_deployment(tmp_path, "reference", INTROSPECTING_CLAUDE)
    settings = doors_deployment(
        tmp_path,
        "deployment",
        parsing_claude(doors_flags(reference), refuses_unknown=False),
    )

    with pytest.raises(ClaudeExecutableUnsupported, match="no release can know"):
        attest_atelier_doors_invocation(settings)
