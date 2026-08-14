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

_PRINT_FLAG = "-p"
_OUTPUT_FORMAT_FLAG = "--output-format"
_JSON_OUTPUT_FORMAT = "json"
_MODEL_FLAG = "--model"

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
    """One headless `claude --print` invocation shape and its JSON envelope."""

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

    def open(self) -> ClaudeSubscriptionExecutor:
        return ClaudeSubscriptionExecutor(self.settings)
