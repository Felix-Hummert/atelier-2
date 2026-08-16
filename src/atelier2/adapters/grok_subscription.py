"""One headless `grok -p` subscription executor.

Containment flags and the version set are measured against grok 1.0.4. Job
bytes travel through `--prompt-file` so they never appear on the argument
vector; standard input is empty. The child environment is only `HOME`,
`GROK_HOME` and `PATH` -- and `HOME` is containment, not convenience: without
it the CLI resolves the invoking account's own profile.

The prompt file is the one thing this seam writes, so it is written into a
private per-execution directory and opened `O_EXCL | O_NOFOLLOW` at mode 0600:
a preplaced symlink at a predictable name cannot redirect job bytes into
another writable target, and the directory is removed on every lifecycle path.

Flags alone do not bound what the CLI discovers. `grok inspect --json` reports
the plugins, hooks, MCP servers, skills, marketplaces, LSP servers, permission
sources and project instructions a directory would load, so the host attests
that exact profile with this executor's own environment and refuses to serve
when anything but built-in agents is discoverable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
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
# Measured against grok 1.0.4 `--help`: each of these names an ambient surface
# a single headless turn must not reach. They are containment, not preference.
_NO_MEMORY_FLAG = "--no-memory"
_NO_SUBAGENTS_FLAG = "--no-subagents"
_NO_WEB_SEARCH_FLAG = "--disable-web-search"

_INSPECT_COMMAND = "inspect"
_INSPECT_JSON_FLAG = "--json"
_INSPECT_OUTPUT_BYTES = 1_048_576
# `grok inspect --json` names every surface it would load for a directory. A
# discovered plugin, hook, MCP server, skill, marketplace, LSP server,
# permission source or project instruction is trust this seam never granted.
_DISCOVERY_SURFACES = (
    "plugins",
    "hooks",
    "mcpServers",
    "skills",
    "marketplaces",
    "lspServers",
    "projectInstructions",
    "configSources",
)
_CONFIG_LAYERS_FIELD = "layers"
_AGENTS_FIELD = "agents"
_AGENT_SOURCE_FIELD = "source"
_AGENT_SOURCE_TYPE_FIELD = "type"
_BUILTIN_AGENT_SOURCE = "builtin"
_PERMISSIONS_FIELD = "permissions"
_PERMISSION_SOURCES_FIELD = "sources"

_JOB_DIRECTORY_PREFIX = "atelier2-grok-job-"
_JOB_FILE_NAME = "job"
_JOB_DIRECTORY_MODE = 0o700
_JOB_FILE_MODE = 0o600
_JOB_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW

_HOME_VARIABLE = "HOME"
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


class GrokContainmentUnattested(ValueError):
    """The composed profile discovers a surface this executor never granted."""


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


def _child_environment(
    settings: GrokSubscriptionSettings,
) -> tuple[tuple[str, str], ...]:
    """The complete environment a launched Grok inherits, and nothing else."""

    # `GROK_HOME` alone does not isolate the CLI. Measured on grok 1.0.4: with
    # `GROK_HOME` pointed at an empty directory and no `HOME` in the child
    # environment, `grok inspect` still discovered 1 plugin, 1 hook, 19 skills,
    # a project instruction, a permission source and ten plugin-sourced agents
    # -- it resolves the invoking account's home and loads that profile. Naming
    # `HOME` as the same private directory is what empties every surface.
    return (
        (_HOME_VARIABLE, str(settings.credential_directory)),
        (_CREDENTIAL_DIRECTORY_VARIABLE, str(settings.credential_directory)),
        (_SEARCH_PATH_VARIABLE, settings.search_path),
    )


def _discovered_surfaces(inspected: dict[str, object]) -> tuple[str, ...]:
    """Name every surface the reported configuration would load."""

    discovered: list[str] = []
    for surface in _DISCOVERY_SURFACES:
        entries = inspected.get(surface)
        if isinstance(entries, dict):
            entries = entries.get(_CONFIG_LAYERS_FIELD)
        if isinstance(entries, list) and entries:
            discovered.append(f"{surface}={len(entries)}")
    permissions = inspected.get(_PERMISSIONS_FIELD)
    if isinstance(permissions, dict):
        sources = permissions.get(_PERMISSION_SOURCES_FIELD)
        if isinstance(sources, list) and sources:
            discovered.append(f"{_PERMISSIONS_FIELD}.{_PERMISSION_SOURCES_FIELD}")
    agents = inspected.get(_AGENTS_FIELD)
    if isinstance(agents, list):
        for agent in agents:
            if not isinstance(agent, dict):
                discovered.append(_AGENTS_FIELD)
                continue
            source = agent.get(_AGENT_SOURCE_FIELD)
            kind = (
                source.get(_AGENT_SOURCE_TYPE_FIELD)
                if isinstance(source, dict)
                else None
            )
            if kind != _BUILTIN_AGENT_SOURCE:
                discovered.append(f"{_AGENTS_FIELD}:{kind}")
    return tuple(discovered)


def attest_grok_containment(
    settings: GrokSubscriptionSettings,
    timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS,
) -> None:
    """Refuse to serve when the composed profile discovers a trusted surface.

    `--tools=` removes built-ins; it says nothing about the plugins, hooks, MCP
    servers and agent definitions the CLI loads from the workspace and from
    `GROK_HOME`. Trusted hook or MCP code would run with the server's own
    privileges, so this asks the CLI what it would load, with exactly the
    environment and working directory a job would get, and refuses on anything.
    """

    try:
        process = subprocess.Popen(
            (
                str(settings.executable),
                _INSPECT_COMMAND,
                _INSPECT_JSON_FLAG,
            ),
            cwd=settings.workspace,
            env=dict(_child_environment(settings)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise GrokContainmentUnattested(
            f"the Grok executable did not answer {_INSPECT_COMMAND}: {error}"
        ) from error
    try:
        return_code, answer = bounded_process_answer(
            process, timeout_seconds, _INSPECT_OUTPUT_BYTES
        )
    except OSError as error:
        raise GrokContainmentUnattested(
            f"the Grok executable did not answer {_INSPECT_COMMAND}: {error}"
        ) from error
    if return_code != 0:
        raise GrokContainmentUnattested(
            f"the Grok executable refused {_INSPECT_COMMAND} with exit code "
            f"{return_code}: the served profile is unattested"
        )
    try:
        inspected: object = json.loads(answer)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GrokContainmentUnattested(
            f"the Grok executable did not report {_INSPECT_COMMAND} as JSON"
        ) from error
    if not isinstance(inspected, dict):
        raise GrokContainmentUnattested(
            f"the Grok executable did not report {_INSPECT_COMMAND} as an object"
        )
    discovered = _discovered_surfaces(inspected)
    if discovered:
        raise GrokContainmentUnattested(
            "serving Grok subscription agents requires a profile that discovers "
            "no plugin, hook, MCP server, skill, marketplace, LSP server, "
            "permission source, project instruction or non-built-in agent; this "
            f"workspace and credential directory discover {', '.join(discovered)}"
        )


def _open_job_file(settings: GrokSubscriptionSettings, job_bytes: bytes) -> Path:
    """Write one job into a private file no symlink can redirect."""

    directory = Path(
        tempfile.mkdtemp(prefix=_JOB_DIRECTORY_PREFIX, dir=settings.workspace)
    )
    os.chmod(directory, _JOB_DIRECTORY_MODE)
    path = directory / _JOB_FILE_NAME
    try:
        descriptor = os.open(path, _JOB_FILE_FLAGS, _JOB_FILE_MODE)
        try:
            os.write(descriptor, job_bytes)
        finally:
            os.close(descriptor)
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return path


@dataclass(frozen=True)
class GrokSubscriptionExecutor:
    settings: GrokSubscriptionSettings
    _job_directories: set[Path] = field(
        default_factory=set, init=False, compare=False, repr=False
    )

    def prepare_process(
        self, request: AgentExecutionRequestV2
    ) -> AgentProcessInvocation:
        binding = request.resolved_binding
        if binding.auth_profile.auth_mode is not AuthMode.SUBSCRIPTION:
            raise GrokSubscriptionAuthModeUnsupported(
                "the Grok subscription executor serves subscription profiles only"
            )
        settings = self.settings
        job_file = _open_job_file(settings, request.job_bytes)
        self._job_directories.add(job_file.parent)
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
                _NO_MEMORY_FLAG,
                _NO_SUBAGENTS_FLAG,
                _NO_WEB_SEARCH_FLAG,
                _MAXIMUM_TURNS_FLAG,
                _HEARTBEAT_MAXIMUM_TURNS,
            ),
            settings.workspace,
            _child_environment(settings),
            b"",
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        )

    def _discard_jobs(self) -> None:
        """Job bytes outlive no lifecycle path: success, failure, cancel, close."""

        for directory in tuple(self._job_directories):
            shutil.rmtree(directory, ignore_errors=True)
            self._job_directories.discard(directory)

    def decode_process_completion(
        self, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        self._discard_jobs()
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
        self._discard_jobs()


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
