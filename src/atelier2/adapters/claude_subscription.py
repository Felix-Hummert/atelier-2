from __future__ import annotations

import json
import os
import subprocess
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
    AgentExecutionCapability,
    AgentExecutionFailure,
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
# Issue #9 binds headless as every provider's duty and interactive as the
# declared extra. This executor drives one non-interactive print call, so it
# declares headless and nothing else; a node demanding interactive is refused
# at validation (see `atelier2.adapters.dbos.starter`).
CLAUDE_SUBSCRIPTION_DECLARED_CAPABILITIES = frozenset(
    {AgentExecutionCapability.HEADLESS}
)

# The oldest Claude Code whose containment flags were measured for this
# executor. The flag semantics below are version-pinned, so an older executable
# is refused at composition rather than trusted to mean the same thing.
MINIMUM_CLAUDE_VERSION = (2, 1, 221)

_VERSION_FLAG = "--version"
_VERSION_PROBE_TIMEOUT_SECONDS = 30.0

_PRINT_FLAG = "-p"
_OUTPUT_FORMAT_FLAG = "--output-format"
_JSON_OUTPUT_FORMAT = "json"
_MODEL_FLAG = "--model"

# The containment flags, each measured against claude 2.1.221 (see the class
# docstring for what each one was observed to stop). The two that carry an
# empty value use the `--flag=` equals form deliberately: the process seam
# refuses an empty argument vector element, and the equals form states "this
# option, with nothing" in a single nonempty argument.
_NO_TOOLS = "--tools="
_NO_SETTING_SOURCES = "--setting-sources="
_STRICT_MCP_CONFIG_FLAG = "--strict-mcp-config"
_MCP_CONFIG_FLAG = "--mcp-config"
_EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
_SAFE_MODE_FLAG = "--safe-mode"
_DISABLE_SLASH_COMMANDS_FLAG = "--disable-slash-commands"
_NO_CHROME_FLAG = "--no-chrome"
_NO_SESSION_PERSISTENCE_FLAG = "--no-session-persistence"
_MAXIMUM_TURNS_FLAG = "--max-turns"
# Stopgap until issue #26 owns a provider-neutral budget: a tool-free print
# call needs exactly one assistant turn, so one turn cannot truncate an answer
# and no unbounded subscription loop can start.
_HEARTBEAT_MAXIMUM_TURNS = "1"

_CREDENTIAL_DIRECTORY_VARIABLE = "CLAUDE_CONFIG_DIR"
_SEARCH_PATH_VARIABLE = "PATH"
# Read straight out of the 2.1.221 executable: `ETr` returns before appending
# to the prompt history when this is set, so the operator's history file never
# receives a job this seam sent. It is distinct from session persistence, which
# owns the resumable transcript.
_SKIP_PROMPT_HISTORY_VARIABLE = "CLAUDE_CODE_SKIP_PROMPT_HISTORY"
_SKIP_PROMPT_HISTORY = "1"
# Also read straight out of that executable: a finite value of at least zero
# replaces the built-in retry count, so a billed call cannot silently become
# several. Zero is the bound this heartbeat wants.
_MAXIMUM_RETRIES_VARIABLE = "CLAUDE_CODE_MAX_RETRIES"
_NO_RETRIES = "0"

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


class ClaudeExecutableUnsupported(ValueError):
    """The named executable is not a Claude Code this executor was measured against."""


def _parsed_version(reported: str) -> tuple[int, int, int] | None:
    """Read `2.1.221 (Claude Code)` as the version it names."""

    leading = reported.strip().split(" ", 1)[0]
    parts = leading.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch


def read_claude_version(executable: Path) -> tuple[int, int, int]:
    """Ask one executable which Claude Code it is. Runs it with `--version`.

    The containment this executor claims is a property of the flags of a
    measured release, not of every file named `claude`, so the deployment reads
    the answer instead of assuming it.
    """

    try:
        completed = subprocess.run(
            (str(executable), _VERSION_FLAG),
            capture_output=True,
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ClaudeExecutableUnsupported(
            f"the Claude executable did not answer {_VERSION_FLAG}: {error}"
        ) from error
    if completed.returncode != 0:
        raise ClaudeExecutableUnsupported(
            f"the Claude executable refused {_VERSION_FLAG} "
            f"with exit code {completed.returncode}"
        )
    version = _parsed_version(completed.stdout.decode("utf-8", "replace"))
    if version is None:
        raise ClaudeExecutableUnsupported(
            f"the Claude executable did not report a version at {_VERSION_FLAG}"
        )
    return version


def verify_claude_capability(executable: Path) -> tuple[int, int, int]:
    """Refuse an executable older than the release these flags were measured on."""

    version = read_claude_version(executable)
    if version < MINIMUM_CLAUDE_VERSION:
        reported = ".".join(str(part) for part in version)
        required = ".".join(str(part) for part in MINIMUM_CLAUDE_VERSION)
        raise ClaudeExecutableUnsupported(
            f"serving Claude subscription agents requires Claude Code {required} "
            f"or newer, not {reported}: this executor's containment flags were "
            "measured against that release and an older one cannot be assumed "
            "to honour them"
        )
    return version


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
    credential directory. Every flag and variable below exists in claude
    2.1.221 and was measured there -- against a workspace holding a `CLAUDE.md`
    and a `.claude/settings.json` whose `SessionStart` hook creates a file, and
    against the executable's own `--help` and strings, with a deliberately
    bogus flag as the control (it answers `error: unknown option`):

    * without them the CLI ran that hook, obeyed that `CLAUDE.md`, and wrote a
      resumable transcript under `CLAUDE_CONFIG_DIR/projects/`;
    * `--setting-sources=` (no user/project/local settings file) and
      `--safe-mode` (no plugins, skills, MCP servers, custom agents, hooks or
      `CLAUDE.md` discovery) each stopped the hook and the project prompt;
    * `--strict-mcp-config` with an explicitly empty `--mcp-config` leaves zero
      MCP servers whatever another configuration declares;
    * `--tools=` removes every tool: asked to run a shell command, the model
      printed an imitation of a tool call as text while the same prompt with
      the default tools really created the file;
    * `--disable-slash-commands` disables all skills, and `--no-chrome`
      disables the Claude in Chrome integration;
    * `--no-session-persistence` left no transcript behind at all;
    * `CLAUDE_CODE_SKIP_PROMPT_HISTORY` makes the CLI return before appending
      to the prompt history, and `CLAUDE_CODE_MAX_RETRIES=0` replaces its
      built-in retry count, so one billed call cannot silently become several.

    Two flags carry an empty value through the `--flag=` equals form rather
    than as a following argument. The process seam refuses an empty element in
    an argument vector, and both forms were measured to parse identically here.

    `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB` is deliberately NOT set. It is a real
    variable, but the executable reads it as an opt-OUT of scrubbing (its own
    diagnostics say "set CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0 to disable (loses
    subprocess isolation)"), it governs the environment handed to tool
    subprocesses, and it needs bubblewrap. A tool-free call spawns no tool
    subprocess, so setting it to 1 would assert a containment this invocation
    does not obtain from it.

    Residual risk, deliberately not claimed as closed. The process still runs
    as the serving user and can read `CLAUDE_CONFIG_DIR`; tool-free, it spawns
    no descendant that could inherit it, but nothing in the operating system
    forbids one. An administrator-managed (policy) settings file is outside
    every control above by design -- `--safe-mode`'s own help says so -- and
    would still apply. The operator ruled on 2026-08-14 that OS-enforced
    isolation stays planned as its own slice but does not gate this local
    heartbeat; until it lands, this executor is composed only for a loopback
    deployment (see `atelier2.host`).
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
                _NO_TOOLS,
                _NO_SETTING_SOURCES,
                _SAFE_MODE_FLAG,
                _STRICT_MCP_CONFIG_FLAG,
                _MCP_CONFIG_FLAG,
                _EMPTY_MCP_CONFIG,
                _DISABLE_SLASH_COMMANDS_FLAG,
                _NO_CHROME_FLAG,
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
                (_SKIP_PROMPT_HISTORY_VARIABLE, _SKIP_PROMPT_HISTORY),
                (_MAXIMUM_RETRIES_VARIABLE, _NO_RETRIES),
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
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        return CLAUDE_SUBSCRIPTION_DECLARED_CAPABILITIES

    def open(self) -> ClaudeSubscriptionExecutor:
        return ClaudeSubscriptionExecutor(self.settings)
