from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AuthMode,
    ProviderId,
)
from atelier2.ports.agent_executions import (
    MAXIMUM_PROVIDER_FRAME_BYTES,
    AgentExecutionFailure,
    AgentExecutionMode,
    AgentExecutorKey,
    AgentProcessCompletion,
    AgentProcessInvocation,
)

CLAUDE_SUBSCRIPTION_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("anthropic"), AgentExecutorRevision("claude-subscription/v1")
)
CLAUDE_SUBSCRIPTION_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-print-json/v1"
)
CLAUDE_SUBSCRIPTION_SUPPORTED_MODES = frozenset({AgentExecutionMode.HEADLESS})

_PRINT_FLAG = "-p"
_OUTPUT_FORMAT_FLAG = "--output-format"
_JSON_OUTPUT_FORMAT = "json"
_MODEL_FLAG = "--model"

# The containment flags, each measured against claude 2.1.221 (see the class
# docstring for what each one was observed to stop).
_TOOLS_FLAG = "--tools"
_NO_TOOLS = ""
_SETTING_SOURCES_FLAG = "--setting-sources"
_NO_SETTING_SOURCES = ""
_STRICT_MCP_CONFIG_FLAG = "--strict-mcp-config"
_SAFE_MODE_FLAG = "--safe-mode"
_NO_SESSION_PERSISTENCE_FLAG = "--no-session-persistence"
_MAXIMUM_TURNS_FLAG = "--max-turns"
# Stopgap until issue #26 owns a provider-neutral budget: a tool-free print
# call needs exactly one assistant turn, so one turn cannot truncate an answer
# and no unbounded subscription loop can start.
_HEARTBEAT_MAXIMUM_TURNS = "1"

_CREDENTIAL_DIRECTORY_VARIABLE = "CLAUDE_CONFIG_DIR"
_SEARCH_PATH_VARIABLE = "PATH"

_ENVELOPE_TYPE_FIELD = "type"
_RESULT_ENVELOPE_TYPE = "result"
_ERROR_FLAG_FIELD = "is_error"
_RESULT_FIELD = "result"

# The durable attempt contract knows exactly one failure code today, so every
# unusable provider answer projects onto it until that enum, its SQL guards and
# the frozen attempt wire field gain a wider vocabulary together.
_UNUSABLE_PROVIDER_ANSWER = AgentExecutionFailure(
    AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
)


class ClaudeSubscriptionAuthModeUnsupported(ValueError):
    """A published configuration bound a non-subscription profile to this executor."""


@dataclass(frozen=True)
class ClaudeSubscriptionSettings:
    """The deployment values one Claude subscription executor may use.

    `search_path` is the serving host's own executable search path: the Claude
    CLI resolves its interpreter and its own child tools through it, and the
    launched process receives no other inherited environment.
    """

    executable: Path
    workspace: Path
    credential_directory: Path
    search_path: str

    def __post_init__(self) -> None:
        executable = self.executable.resolve()
        workspace = self.workspace.resolve()
        credential_directory = self.credential_directory.resolve()
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "workspace", workspace)
        object.__setattr__(self, "credential_directory", credential_directory)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("the Claude executable must be an executable file")
        if not workspace.is_dir():
            raise ValueError("the Claude workspace must be an existing directory")
        if not credential_directory.is_dir():
            raise ValueError(
                "the Claude credential directory must be an existing directory"
            )
        if not self.search_path.strip():
            raise ValueError("the Claude executable search path must be nonempty")


@dataclass(frozen=True)
class ClaudeSubscriptionExecutor:
    """One bare headless `claude --print` invocation and its JSON envelope.

    The invocation is as close to text-in/text-out as subscription
    authentication allows, because the process is handed the operator's
    credential directory. Each flag was measured against claude 2.1.221 with a
    workspace holding a `CLAUDE.md` and a `.claude/settings.json` whose
    `SessionStart` hook creates a file:

    * without them the CLI ran that hook, obeyed that `CLAUDE.md`, and wrote a
      resumable transcript under `CLAUDE_CONFIG_DIR/projects/`;
    * `--setting-sources ""` (no user/project/local settings file) and
      `--safe-mode` (no plugins, skills, MCP servers, custom agents, hooks or
      `CLAUDE.md` discovery) each stopped the hook and the project prompt;
    * `--strict-mcp-config` without any `--mcp-config` leaves zero MCP servers
      whatever another configuration declares;
    * `--tools ""` removes every tool: asked to run a shell command, the model
      printed an imitation of a tool call as text while the same prompt with
      the default tools really created the file;
    * `--no-session-persistence` left no transcript behind at all.

    Residual risk, deliberately not claimed as closed. The process still runs
    as the serving user and can read `CLAUDE_CONFIG_DIR`; tool-free, it spawns
    no descendant that could inherit it, but nothing in the operating system
    forbids one. An administrator-managed settings file is outside every control
    above by design and would still apply. Full OS-enforced isolation
    of the credential lease is deferred to the operator ruling recorded on the
    pull request for issue #29; until that ruling, this executor is composed
    only for a loopback deployment (see `atelier2.host`).
    """

    settings: ClaudeSubscriptionSettings

    def prepare_process(
        self, request: AgentExecutionRequestV2
    ) -> AgentProcessInvocation:
        binding = request.resolved_binding
        if binding.auth_profile.auth_mode is not AuthMode.SUBSCRIPTION:
            raise ClaudeSubscriptionAuthModeUnsupported(
                "the Claude subscription executor serves subscription profiles only"
            )
        settings = self.settings
        return AgentProcessInvocation(
            (
                str(settings.executable),
                _PRINT_FLAG,
                _OUTPUT_FORMAT_FLAG,
                _JSON_OUTPUT_FORMAT,
                _MODEL_FLAG,
                binding.configuration.model,
                _TOOLS_FLAG,
                _NO_TOOLS,
                _SETTING_SOURCES_FLAG,
                _NO_SETTING_SOURCES,
                _STRICT_MCP_CONFIG_FLAG,
                _SAFE_MODE_FLAG,
                _NO_SESSION_PERSISTENCE_FLAG,
                _MAXIMUM_TURNS_FLAG,
                _HEARTBEAT_MAXIMUM_TURNS,
            ),
            settings.workspace,
            (
                (
                    _CREDENTIAL_DIRECTORY_VARIABLE,
                    str(settings.credential_directory),
                ),
                (_SEARCH_PATH_VARIABLE, settings.search_path),
            ),
            # The job travels through standard input so no prompt ever appears
            # in the process table of a host the operator shares.
            request.job_bytes,
        )

    def decode_process_completion(
        self, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        if completion.return_code != 0:
            return _UNUSABLE_PROVIDER_ANSWER
        if len(completion.standard_output) > MAXIMUM_PROVIDER_FRAME_BYTES:
            return _UNUSABLE_PROVIDER_ANSWER
        try:
            envelope: object = json.loads(completion.standard_output)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _UNUSABLE_PROVIDER_ANSWER
        if not isinstance(envelope, dict):
            return _UNUSABLE_PROVIDER_ANSWER
        result = envelope.get(_RESULT_FIELD)
        if (
            envelope.get(_ENVELOPE_TYPE_FIELD) != _RESULT_ENVELOPE_TYPE
            or envelope.get(_ERROR_FLAG_FIELD) is not False
            or not isinstance(result, str)
        ):
            return _UNUSABLE_PROVIDER_ANSWER
        output_bytes = result.encode("utf-8")
        if len(output_bytes) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            return _UNUSABLE_PROVIDER_ANSWER
        return AgentExecutionResult(output_bytes)

    def close(self) -> None:
        return


@dataclass(frozen=True)
class ClaudeSubscriptionExecutorFactory:
    settings: ClaudeSubscriptionSettings

    @property
    def key(self) -> AgentExecutorKey:
        return CLAUDE_SUBSCRIPTION_EXECUTOR_KEY

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return CLAUDE_SUBSCRIPTION_OPERATIONAL_IDENTITY

    @property
    def supported_modes(self) -> frozenset[AgentExecutionMode]:
        return CLAUDE_SUBSCRIPTION_SUPPORTED_MODES

    def open(self) -> ClaudeSubscriptionExecutor:
        return ClaudeSubscriptionExecutor(self.settings)
