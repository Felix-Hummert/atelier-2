"""One headless `grok` subscription executor.

Containment flags and the version set are measured against grok 1.0.4. Job
bytes travel through `--prompt-file` so they never appear on the argument
vector; standard input is empty. The child environment is only `HOME`,
`GROK_HOME` and `PATH` -- and `HOME` is containment, not convenience: without
it the CLI resolves the invoking account's own profile.

Every invocation gets one private disposable `HOME`/`GROK_HOME`. The seam
copies only `auth.json` into it, writes an inert compatibility configuration
and the prompt with exclusive no-follow opens, then removes the whole home on
every lifecycle path. Provider-created sessions therefore never enter the
source credential directory or outlive their invocation.

Flags alone do not bound what the CLI discovers. `grok inspect --json` reports
the plugins, hooks, MCP servers, skills, marketplaces, LSP servers, permission
sources, project instructions and external compatibility cells a directory
would load. Each prepared invocation attests its own exact home, configuration
and working directory before the supervisor may launch it.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
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
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)

GROK_SUBSCRIPTION_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("xai"), AgentExecutorRevision("grok-subscription/v1")
)
GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-print-json/v1"
)

# Same portable ceiling as the process port. Measured on grok 1.0.4
# `--output-format json`: the durable answer is `text`; `thought` is the
# turn narration and is not the answer. Metadata size is not a tighter
# allowance. A durable-legal answer still fits: JSON escaping expands one
# source byte to at most six frame bytes (6 * 49,152 = 294,912), leaving
# the remainder for metadata.
GROK_SUBSCRIPTION_FRAME_BYTES = MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES

CONFORMANT_GROK_VERSIONS = frozenset({(1, 0, 4)})

_VERSION_FLAG = "--version"
_VERSION_PROBE_TIMEOUT_SECONDS = 30.0
_VERSION_PROBE_OUTPUT_BYTES = 4_096

_OUTPUT_FORMAT_FLAG = "--output-format"
_JSON_OUTPUT_FORMAT = "json"
_JSON_SCHEMA_FLAG = "--json-schema"
_MODEL_FLAG = "--model"
_PROMPT_FILE_FLAG = "--prompt-file"
_MAXIMUM_TURNS_FLAG = "--max-turns"
# Headless one-answer class, not a heartbeat. A Diff-Review-sized order
# (~14 KB, #295) dies at one turn (`max turns reached`) because the CLI
# spends turns on read/tool work before the one JSON answer. Sixteen
# covers that cycle; it is not an unbounded subscription loop.
_HEADLESS_MAXIMUM_TURNS = "16"
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
)
_CONFIG_LAYERS_FIELD = "layers"
_AGENTS_FIELD = "agents"
_AGENT_SOURCE_FIELD = "source"
_AGENT_SOURCE_TYPE_FIELD = "type"
_BUILTIN_AGENT_SOURCE = "builtin"
_PERMISSIONS_FIELD = "permissions"
_PERMISSION_SOURCES_FIELD = "sources"

_EXTERNAL_COMPATIBILITY_CELLS = (
    ("cursor", "skills"),
    ("cursor", "rules"),
    ("cursor", "agents"),
    ("cursor", "mcps"),
    ("cursor", "hooks"),
    ("cursor", "sessions"),
    ("claude", "skills"),
    ("claude", "rules"),
    ("claude", "agents"),
    ("claude", "mcps"),
    ("claude", "hooks"),
    ("claude", "sessions"),
    ("codex", "sessions"),
)
_EXTERNAL_COMPATIBILITY_FIELD = "externalCompat"
_REMOTE_SETTINGS_LOADED_FIELD = "remoteSettingsLoaded"
_COMPATIBILITY_CELLS_FIELD = "cells"
_COMPATIBILITY_VENDOR_FIELD = "vendor"
_COMPATIBILITY_SURFACE_FIELD = "surface"
_COMPATIBILITY_ENABLED_FIELD = "enabled"
_COMPATIBILITY_SOURCE_FIELD = "source"
_CONFIG_SOURCE_ROLE_FIELD = "role"
_CONFIG_SOURCE_PATH_FIELD = "path"
_USER_CONFIG_SOURCE_ROLE = "user"
_CONFIG_SOURCE = "config"

_JOB_DIRECTORY_PREFIX = "atelier2-grok-job-"
_JOB_FILE_NAME = "job"
_CONFIG_FILE_NAME = "config.toml"
_AUTHENTICATION_FILE_NAME = "auth.json"
_JOB_DIRECTORY_MODE = 0o700
_JOB_FILE_MODE = 0o600
_CONFIG_FILE_MODE = 0o600
_AUTHENTICATION_FILE_MODE = 0o400
_JOB_FILE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
_MAXIMUM_AUTHENTICATION_FILE_BYTES = 1_048_576

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
        authentication = credential_directory / _AUTHENTICATION_FILE_NAME
        try:
            authentication_status = authentication.stat(follow_symlinks=False)
        except OSError as error:
            raise ValueError(
                "the Grok credential directory must contain a regular auth.json"
            ) from error
        if (
            not stat.S_ISREG(authentication_status.st_mode)
            or stat.S_IMODE(authentication_status.st_mode) & 0o077
        ):
            raise ValueError("the Grok auth.json must be a private regular file")
        if not self.search_path.strip():
            raise ValueError("the Grok executable search path must be nonempty")


def _json_schema_flag(declared_output_schema_bytes: bytes | None) -> tuple[str, ...]:
    """The `--json-schema` pair, or nothing where the node declared none.

    These are the exact published document bytes the output seam later
    judges -- not a second serialization. Measured on grok 1.0.4: the flag
    constrains the model and implies `--output-format json`; a provider that
    ignores it is still refused by the seam. The CLI accepts
    `{"type":"string"}` and refuses a boolean schema (`true`) with
    "must be a JSON object describing a JSON Schema"; this seam does not
    rewrite that form.
    """
    if declared_output_schema_bytes is None:
        return ()
    try:
        return (
            _JSON_SCHEMA_FLAG,
            declared_output_schema_bytes.decode("utf-8"),
        )
    except UnicodeDecodeError as error:
        raise ValueError("declared output schema bytes must be UTF-8") from error


def _child_environment(
    settings: GrokSubscriptionSettings, state_directory: Path
) -> tuple[tuple[str, str], ...]:
    """The complete environment a launched Grok inherits, and nothing else."""

    # `GROK_HOME` alone does not isolate the CLI. Measured on grok 1.0.4: with
    # `GROK_HOME` pointed at an empty directory and no `HOME` in the child
    # environment, `grok inspect` still discovered 1 plugin, 1 hook, 19 skills,
    # a project instruction, a permission source and ten plugin-sourced agents
    # -- it resolves the invoking account's home and loads that profile. Naming
    # `HOME` as the same private directory is what empties every surface.
    return (
        (_HOME_VARIABLE, str(state_directory)),
        (_CREDENTIAL_DIRECTORY_VARIABLE, str(state_directory)),
        (_SEARCH_PATH_VARIABLE, settings.search_path),
    )


def _configuration_bytes() -> bytes:
    sections: list[str] = []
    for vendor in ("cursor", "claude", "codex"):
        lines = [f"[compat.{vendor}]"]
        lines.extend(
            f"{surface} = false"
            for candidate, surface in _EXTERNAL_COMPATIBILITY_CELLS
            if candidate == vendor
        )
        sections.append("\n".join(lines))
    return ("\n\n".join(sections) + "\n").encode("ascii")


def _discovered_surfaces(
    inspected: dict[str, object], state_directory: Path
) -> tuple[str, ...]:
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
    config_sources = inspected.get("configSources")
    expected_layers = [
        {
            _CONFIG_SOURCE_ROLE_FIELD: _USER_CONFIG_SOURCE_ROLE,
            _CONFIG_SOURCE_PATH_FIELD: str(state_directory / _CONFIG_FILE_NAME),
        }
    ]
    if (
        not isinstance(config_sources, dict)
        or config_sources.get(_CONFIG_LAYERS_FIELD) != expected_layers
    ):
        discovered.append("configSources")
    compatibility = inspected.get(_EXTERNAL_COMPATIBILITY_FIELD)
    expected_cells = [
        {
            _COMPATIBILITY_VENDOR_FIELD: vendor,
            _COMPATIBILITY_SURFACE_FIELD: surface,
            _COMPATIBILITY_ENABLED_FIELD: False,
            _COMPATIBILITY_SOURCE_FIELD: _CONFIG_SOURCE,
        }
        for vendor, surface in _EXTERNAL_COMPATIBILITY_CELLS
    ]
    if (
        not isinstance(compatibility, dict)
        or compatibility.get(_REMOTE_SETTINGS_LOADED_FIELD) is not False
        or compatibility.get(_COMPATIBILITY_CELLS_FIELD) != expected_cells
    ):
        discovered.append(_EXTERNAL_COMPATIBILITY_FIELD)
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
    state_directory: Path,
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
            cwd=state_directory,
            env=dict(_child_environment(settings, state_directory)),
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
    discovered = _discovered_surfaces(inspected, state_directory)
    if discovered:
        raise GrokContainmentUnattested(
            "serving Grok subscription agents requires a profile that discovers "
            "no plugin, hook, MCP server, skill, marketplace, LSP server, "
            "permission source, project instruction, external compatibility cell "
            "or non-built-in agent; this exact invocation discovers "
            f"{', '.join(discovered)}"
        )


def _write_private_file(path: Path, payload: bytes, mode: int) -> None:
    descriptor = os.open(path, _JOB_FILE_FLAGS, mode)
    try:
        remaining = memoryview(payload)
        while remaining:
            written = os.write(descriptor, remaining)
            if written < 1:
                raise OSError("private Grok file write made no progress")
            remaining = remaining[written:]
    finally:
        os.close(descriptor)


def _authentication_bytes(settings: GrokSubscriptionSettings) -> bytes:
    path = settings.credential_directory / _AUTHENTICATION_FILE_NAME
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode) or stat.S_IMODE(status.st_mode) & 0o077:
            raise ValueError("the Grok auth.json must be a private regular file")
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                return b"".join(chunks)
            size += len(chunk)
            if size > _MAXIMUM_AUTHENTICATION_FILE_BYTES:
                raise ValueError("the Grok auth.json exceeds its private copy bound")
            chunks.append(chunk)
    finally:
        os.close(descriptor)


def _open_job_directory(
    settings: GrokSubscriptionSettings, job_bytes: bytes
) -> tuple[Path, Path]:
    """Prepare one private, disposable Grok home and prompt."""

    directory = Path(
        tempfile.mkdtemp(prefix=_JOB_DIRECTORY_PREFIX, dir=settings.workspace)
    )
    os.chmod(directory, _JOB_DIRECTORY_MODE)
    prepared = False
    try:
        _write_private_file(
            directory / _AUTHENTICATION_FILE_NAME,
            _authentication_bytes(settings),
            _AUTHENTICATION_FILE_MODE,
        )
        _write_private_file(
            directory / _CONFIG_FILE_NAME,
            _configuration_bytes(),
            _CONFIG_FILE_MODE,
        )
        job_file = directory / _JOB_FILE_NAME
        _write_private_file(job_file, job_bytes, _JOB_FILE_MODE)
        prepared = True
        return directory, job_file
    finally:
        if not prepared:
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass


@dataclass(frozen=True)
class GrokSubscriptionExecutor:
    settings: GrokSubscriptionSettings
    _invocation_directories: set[Path] = field(
        default_factory=set, init=False, compare=False, repr=False
    )
    _lifecycle_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, compare=False, repr=False
    )
    _closed: threading.Event = field(
        default_factory=threading.Event, init=False, compare=False, repr=False
    )

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        binding = request.resolved_binding
        if binding.auth_profile.auth_mode is not AuthMode.SUBSCRIPTION:
            raise GrokSubscriptionAuthModeUnsupported(
                "the Grok subscription executor serves subscription profiles only"
            )
        settings = self.settings
        state_directory, job_file = _open_job_directory(settings, request.job_bytes)
        registered = False
        try:
            attest_grok_containment(settings, state_directory)
            schema_flag = _json_schema_flag(request.declared_output_schema_bytes)
            command = AgentProcessCommand(
                (
                    # Measured on grok 1.0.4 (build d846eb93d9): `-p` is the
                    # short alias for `--single <PROMPT>`, a flag that
                    # requires an inline value, not the bare print flag this
                    # executor once assumed -- the CLI refuses the launch with
                    # "a value is required for '--single <PROMPT>' but none
                    # was supplied" before any billing occurs. `--prompt-file`
                    # is left as the only single-turn carrier.
                    str(settings.executable),
                    _OUTPUT_FORMAT_FLAG,
                    _JSON_OUTPUT_FORMAT,
                    *schema_flag,
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
                    _HEADLESS_MAXIMUM_TURNS,
                ),
                _child_environment(settings, state_directory),
                b"",
                standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
            )
            with self._lifecycle_lock:
                if self._closed.is_set():
                    raise RuntimeError("the Grok executor is closed")
                self._invocation_directories.add(state_directory)
                registered = True
            return command
        finally:
            if not registered:
                try:
                    shutil.rmtree(state_directory)
                except FileNotFoundError:
                    pass

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        del invocation
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
        # Measured on grok 1.0.4 `--output-format json`: `text` is the final
        # answer message; `thought` is the turn narration. An empty or missing
        # `text` is a named refusal — passing `thought` or the raw frame would
        # hand the schema seam the story of the run.
        text = envelope.get(_TEXT_FIELD)
        if not isinstance(text, str) or text == "":
            return _UNUSABLE_PROVIDER_ANSWER
        output_bytes = text.encode("utf-8")
        if len(output_bytes) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            return _UNUSABLE_PROVIDER_ANSWER
        return AgentExecutionResult(output_bytes)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        """Take back the private home this invocation handed the provider.

        The directory holds a copy of the operator's own `auth.json`, so it is
        taken back on every path rather than with the attempt's workspace: the
        workspace keeps what a provider left behind until the attempt is
        durably terminal, and a credential must not wait that long.
        """

        environment = dict(command.environment)
        home = environment.get(_HOME_VARIABLE)
        grok_home = environment.get(_CREDENTIAL_DIRECTORY_VARIABLE)
        if home is None or home != grok_home:
            raise ValueError("Grok invocation state binding is missing")
        directory = Path(home)
        with self._lifecycle_lock:
            if directory not in self._invocation_directories:
                return
            try:
                shutil.rmtree(directory)
            except FileNotFoundError:
                pass
            self._invocation_directories.remove(directory)

    def close(self) -> None:
        with self._lifecycle_lock:
            self._closed.set()
            directories = tuple(self._invocation_directories)
            errors: list[Exception] = []
            for directory in directories:
                try:
                    shutil.rmtree(directory)
                except FileNotFoundError:
                    self._invocation_directories.remove(directory)
                except OSError as error:
                    errors.append(error)
                else:
                    self._invocation_directories.remove(directory)
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("Grok invocation cleanup failed", errors)


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
