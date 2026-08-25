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
from atelier2.contracts.schemas_v3 import (
    InstanceAccepted,
    InstanceRefusal,
    InstanceRefused,
    InstanceVerdict,
    SchemaAccepted,
    read_instance_document,
    read_schema_document,
)
from atelier2.host.conductor_workflow import (
    CONDUCTOR_DOOR_SERVER_NAME,
    CONDUCTOR_DOOR_TOOLS,
    CONDUCTOR_REPORT_SCHEMA,
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
    declared_output_schema_bytes: bytes | None = None,
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
        declared_output_schema_bytes=declared_output_schema_bytes,
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


# The report every fake episode below answers with. Its two field names are
# this scenario's, not a second owner's: what makes them right is that
# `CONDUCTOR_REPORT_SCHEMA` -- the published contract the run really pins --
# admits the value, which every assertion here goes through.
_EPISODE_REPORT = {
    "answer": "Started the tidy workflow; run-tidy-1 is running.",
    "started_run_ids": ["run-tidy-1"],
}

# The four shapes one identical brief really came back in (#663, live 25.08.):
# bare, with a trailing newline, introduced by a sentence, and inside a
# Markdown fence. Only the first two decoded, so an episode's success was a
# coin flip.
_OBSERVED_ANSWER_SHAPES = (
    "{report}",
    "{report}\n",
    "Here is the report:\n\n{report}",
    "```json\n{report}\n```",
)


def cycling_claude(shapes: tuple[str, ...]) -> str:
    """A fake CLI that answers one report through these wrappers in turn.

    A fake that answered identically every time would prove nothing about a
    provider whose defect is that it does not: the counter beside the program
    is what makes ten identical episodes really meet the varying answer one
    identical brief produced live.
    """

    return (
        "import json, os, sys\n"
        f"report = json.dumps({_EPISODE_REPORT!r})\n"
        f"shapes = {shapes!r}\n"
        "counter = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'answered')\n"
        "answered = int(open(counter).read()) if os.path.exists(counter) else 0\n"
        "open(counter, 'w').write(str(answered + 1))\n"
        "sys.stdin.buffer.read()\n"
        "json.dump(\n"
        "    {\n"
        "        'type': 'result',\n"
        "        'is_error': False,\n"
        "        'result': shapes[answered % len(shapes)].replace('{report}', report),\n"
        "    },\n"
        "    sys.stdout,\n"
        ")\n"
    )


def answering_claude(answer: str) -> str:
    """A fake CLI that answers exactly this text to every episode."""

    return (
        "import json, sys\n"
        "sys.stdin.buffer.read()\n"
        f"json.dump({{'type': 'result', 'is_error': False, 'result': {answer!r}}}, "
        "sys.stdout)\n"
    )


def episode_output(settings: ClaudeAtelierDoorsSettings, workspace: Path) -> bytes:
    """One whole episode: the real vector launched, and its real decode."""

    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()
    request = doors_request(declared_output_schema_bytes=CONDUCTOR_REPORT_SCHEMA)
    command = executor.prepare_process(request)
    outcome = executor.decode_process_completion(
        leased(request, command, workspace), launched(command, workspace)
    )
    assert isinstance(outcome, AgentExecutionResult), outcome
    return outcome.output_bytes


def report_verdict(output: bytes) -> InstanceVerdict:
    """What the output seam makes of these bytes, through its own owner."""

    schema = read_schema_document(CONDUCTOR_REPORT_SCHEMA)
    assert isinstance(schema, SchemaAccepted), schema
    return read_instance_document(output, schema)


@pytest.mark.proves("an-episode-answers-the-value-its-schema-declared")
def test_ten_identical_episodes_all_answer_a_value_the_report_schema_admits(
    tmp_path: Path,
) -> None:
    """The defect this closes: one brief, one schema, and a coin flip between them.

    Two live episodes of the same brief on the same job hash ended one
    COMPLETED and one `output-schema-refused: instance-not-json` (#663). Ten
    episodes here meet every wrapper that was observed, and each one answers
    the value the declared schema admits -- the same value, whatever prose the
    provider put around it.
    """

    settings = doors_deployment(
        tmp_path, "cycling", cycling_claude(_OBSERVED_ANSWER_SHAPES)
    )
    workspace = provider_workspace(tmp_path)

    verdicts = [episode_output(settings, workspace) for _ in range(10)]

    assert [report_verdict(output) for output in verdicts] == [
        InstanceAccepted(_EPISODE_REPORT)
    ] * 10


@pytest.mark.proves("an-episode-answering-no-such-value-is-still-refused")
@pytest.mark.parametrize(
    ("answer", "refusal"),
    [
        pytest.param(
            "I could not reach the catalog, sorry.",
            InstanceRefusal.INSTANCE_NOT_JSON,
            id="prose only",
        ),
        pytest.param(
            '{"answer": "done"}',
            InstanceRefusal.SCHEMA_VIOLATED,
            id="a bare JSON object missing a required field",
        ),
        pytest.param(
            'Here you go:\n```json\n{"answer": "done"}\n```',
            InstanceRefusal.INSTANCE_NOT_JSON,
            id="a wrapped JSON object missing a required field",
        ),
    ],
)
def test_an_episode_carrying_no_declared_value_is_refused_rather_than_narrowed(
    tmp_path: Path, answer: str, refusal: InstanceRefusal
) -> None:
    """Fail loud: narrowing may find a declared value, never invent or repair one.

    The last case is where that costs something, said here rather than found
    later: an answer whose wrapped value is real but of the wrong shape travels
    on whole, so the seam names the wrapper (`instance-not-json`) instead of the
    field the value is missing. That is exactly what the same answer was named
    before this narrowing existed, and making a refusal say more about a value
    it is refusing is its own subject.
    """

    settings = doors_deployment(tmp_path, "refusing", answering_claude(answer))
    workspace = provider_workspace(tmp_path)

    verdict = report_verdict(episode_output(settings, workspace))

    assert isinstance(verdict, InstanceRefused)
    assert verdict.refusal is refusal


def test_an_episode_whose_node_declared_no_schema_keeps_the_answer_it_was_given(
    tmp_path: Path,
) -> None:
    """Narrowing is the declared schema's, so a node without one is untouched."""

    settings = doors_deployment(tmp_path, "unbound", answering_claude("plain words"))
    executor = ClaudeAtelierDoorsExecutorFactory(settings).open()
    request = doors_request()
    command = executor.prepare_process(request)
    workspace = provider_workspace(tmp_path)

    outcome = executor.decode_process_completion(
        leased(request, command, workspace), launched(command, workspace)
    )

    assert outcome == AgentExecutionResult(b"plain words")
