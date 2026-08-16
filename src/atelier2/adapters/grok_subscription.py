"""One headless `grok -p` subscription executor.

Containment flags and the version set are measured against grok 1.0.4. Job
bytes travel through `--prompt-file` so they never appear on the argument
vector; standard input is empty. The child environment is only `GROK_HOME`
and `PATH`.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from atelier2.adapters.bounded_processes import bounded_process_answer
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES,
    AgentExecutionCapability,
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

GROK_SUBSCRIPTION_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("xai"), AgentExecutorRevision("grok-subscription/v1")
)
GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-print-json/v1"
)

# Same portable ceiling as the process port. A Grok JSON envelope carries the
# durable answer in `text`; 1.0.4 metadata has not been measured, so this
# executor does not invent a tighter envelope allowance. A durable-legal
# answer still fits: JSON escaping expands one source byte to at most six
# frame bytes (6 * 49,152 = 294,912), leaving the remainder for metadata.
GROK_SUBSCRIPTION_FRAME_BYTES = MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES

CONFORMANT_GROK_VERSIONS = frozenset({(1, 0, 4)})

_VERSION_FLAG = "--version"
_VERSION_PROBE_TIMEOUT_SECONDS = 30.0
_VERSION_PROBE_OUTPUT_BYTES = 4_096

_PRINT_FLAG = "-p"
_OUTPUT_FORMAT_FLAG = "--output-format"
_JSON_OUTPUT_FORMAT = "json"
_MODEL_FLAG = "--model"
_PROMPT_FILE_FLAG = "--prompt-file"
_MAXIMUM_TURNS_FLAG = "--max-turns"
_HEARTBEAT_MAXIMUM_TURNS = "1"
_TOOLS_FLAG = "--tools="
_PERMISSION_MODE_FLAG = "--permission-mode"
_DONT_ASK = "dontAsk"

_CREDENTIAL_DIRECTORY_VARIABLE = "GROK_HOME"
_SEARCH_PATH_VARIABLE = "PATH"
_TEXT_FIELD = "text"

_UNUSABLE_PROVIDER_ANSWER = AgentExecutionFailure(
    AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
)


class GrokSubscriptionAuthModeUnsupported(ValueError):
    """A published configuration bound a non-subscription profile to this executor."""


class GrokExecutableUnsupported(ValueError):
    """The named executable is not a Grok CLI this executor was measured against."""


def _parsed_version(reported: str) -> tuple[int, int, int] | None:
    """Read `grok 1.0.4 (d846eb93d9) [stable]` or `1.0.4 (...)` as the version."""

    tokens = reported.strip().split()
    if not tokens:
        return None
    leading = (
        tokens[1] if tokens[0].lower() == "grok" and len(tokens) > 1 else tokens[0]
    )
    parts = leading.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def read_grok_version(
    executable: Path, timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS
) -> tuple[int, int, int]:
    """Ask one executable which Grok it is. Runs it with `--version`."""

    with tempfile.TemporaryDirectory(prefix="atelier2-grok-version-") as probe_root:
        try:
            process = subprocess.Popen(
                (str(executable), _VERSION_FLAG),
                cwd=probe_root,
                env={},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise GrokExecutableUnsupported(
                f"the Grok executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
        try:
            return_code, answer = bounded_process_answer(
                process, timeout_seconds, _VERSION_PROBE_OUTPUT_BYTES
            )
        except OSError as error:
            raise GrokExecutableUnsupported(
                f"the Grok executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
    if return_code != 0:
        raise GrokExecutableUnsupported(
            f"the Grok executable refused {_VERSION_FLAG} with exit code {return_code}"
        )
    version = _parsed_version(answer.decode("utf-8", "replace"))
    if version is None:
        raise GrokExecutableUnsupported(
            f"the Grok executable did not report a version at {_VERSION_FLAG}"
        )
    return version


def verify_grok_capability(
    executable: Path, timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS
) -> tuple[int, int, int]:
    """Refuse an executable outside the reviewed conformance set."""

    version = read_grok_version(executable, timeout_seconds)
    if version not in CONFORMANT_GROK_VERSIONS:
        reported = ".".join(str(part) for part in version)
        conformant = ", ".join(
            ".".join(str(part) for part in candidate)
            for candidate in sorted(CONFORMANT_GROK_VERSIONS)
        )
        raise GrokExecutableUnsupported(
            f"serving Grok subscription agents requires Grok {conformant}, "
            f"not {reported}: this executor's invocation semantics were measured "
            "against that exact release"
        )
    return version


@dataclass(frozen=True)
class GrokSubscriptionSettings:
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
            raise ValueError("the Grok executable must be an executable file")
        if not workspace.is_dir():
            raise ValueError("the Grok workspace must be an existing directory")
        if not credential_directory.is_dir():
            raise ValueError(
                "the Grok credential directory must be an existing directory"
            )
        if not self.search_path.strip():
            raise ValueError("the Grok executable search path must be nonempty")


def _job_file(
    settings: GrokSubscriptionSettings, request: AgentExecutionRequestV2
) -> Path:
    return settings.workspace / f"atelier2-grok-job-{request.node_execution_id.value}"


@dataclass(frozen=True)
class GrokSubscriptionExecutor:
    settings: GrokSubscriptionSettings

    def prepare_process(
        self, request: AgentExecutionRequestV2
    ) -> AgentProcessInvocation:
        binding = request.resolved_binding
        if binding.auth_profile.auth_mode is not AuthMode.SUBSCRIPTION:
            raise GrokSubscriptionAuthModeUnsupported(
                "the Grok subscription executor serves subscription profiles only"
            )
        settings = self.settings
        job_file = _job_file(settings, request)
        job_file.write_bytes(request.job_bytes)
        return AgentProcessInvocation(
            (
                str(settings.executable),
                _PRINT_FLAG,
                _OUTPUT_FORMAT_FLAG,
                _JSON_OUTPUT_FORMAT,
                _MODEL_FLAG,
                binding.configuration.model,
                _PROMPT_FILE_FLAG,
                str(job_file),
                _TOOLS_FLAG,
                _PERMISSION_MODE_FLAG,
                _DONT_ASK,
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
            b"",
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        )

    def decode_process_completion(
        self, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        if completion.return_code != 0:
            return _UNUSABLE_PROVIDER_ANSWER
        if len(completion.standard_output) > GROK_SUBSCRIPTION_FRAME_BYTES:
            return _UNUSABLE_PROVIDER_ANSWER
        try:
            envelope: object = json.loads(completion.standard_output)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return _UNUSABLE_PROVIDER_ANSWER
        if not isinstance(envelope, dict):
            return _UNUSABLE_PROVIDER_ANSWER
        text = envelope.get(_TEXT_FIELD)
        if not isinstance(text, str):
            return _UNUSABLE_PROVIDER_ANSWER
        output_bytes = text.encode("utf-8")
        if len(output_bytes) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            return _UNUSABLE_PROVIDER_ANSWER
        return AgentExecutionResult(output_bytes)

    def close(self) -> None:
        return


@dataclass(frozen=True)
class GrokSubscriptionExecutorFactory:
    settings: GrokSubscriptionSettings

    @property
    def key(self) -> AgentExecutorKey:
        return GROK_SUBSCRIPTION_EXECUTOR_KEY

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        return frozenset({AgentExecutionCapability.HEADLESS})

    def open(self) -> GrokSubscriptionExecutor:
        return GrokSubscriptionExecutor(self.settings)
