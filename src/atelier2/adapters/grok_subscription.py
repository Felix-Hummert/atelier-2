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
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from atelier2.adapters.bounded_processes import (
    bounded_process_answer,
    bounded_process_streams,
)
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agent_transcripts import (
    MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES,
    AssistantTurn,
    AttemptTranscript,
    TranscriptEvent,
    UnrecognisedProviderOutput,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
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
    "headless-print-json/v2"
)

# The final envelope follows the same escaping bound as the durable answer;
# values before it are the transcript. Measured on grok 1.0.4 / grok-4.6
# (#392): `--output-format json` may concatenate several JSON values without a
# record separator. Strings before the final envelope are turn narration; only
# the final envelope's `text` is the answer. A frame therefore reserves room
# independently for one envelope and one bounded transcript, rather than
# making progress messages consume the answer allowance.
GROK_SUBSCRIPTION_ENVELOPE_BYTES = 8 * MAXIMUM_AGENT_OUTPUT_BYTES_V2
GROK_SUBSCRIPTION_FRAME_BYTES = (
    GROK_SUBSCRIPTION_ENVELOPE_BYTES + MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES
)

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
# covers that cycle; it is not an unbounded subscription loop. The
# workspace-tool vector uses this same default when the node pins no
# `maximum_assistant_turns`.
_HEADLESS_MAXIMUM_TURNS = "16"
_TOOLS_FLAG = "--tools="
_TOOLS_OPTION = "--tools"
_PERMISSION_MODE_FLAG = "--permission-mode"
_DONT_ASK = "dontAsk"
_ALLOW_FLAG = "--allow"
_DENY_FLAG = "--deny"
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


def _unusable_provider_answer(
    transcript: AttemptTranscript | None,
) -> AgentExecutionFailure:
    """This call produced no answer this executor may use, with what it wrote."""

    return AgentExecutionFailure(
        AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY, transcript
    )


class GrokSubscriptionAuthModeUnsupported(ValueError):
    """A published configuration bound a non-subscription profile to this executor."""


class GrokExecutableUnsupported(ValueError):
    """The named executable is not a Grok CLI this executor was measured against."""


class GrokContainmentUnattested(ValueError):
    """The composed profile discovers a surface this executor never granted."""


@dataclass(frozen=True)
class GrokProviderEndedWithoutFinalMessage(AgentExecutionFailure):
    """Grok ended after progress messages without publishing a final envelope."""

    code: AgentAttemptFailureCode = field(
        default=AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY,
        init=False,
    )


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


_BARE_STRING_SCHEMA_FIELDS = frozenset({"type", "minLength", "maxLength"})


def _is_bare_string_schema(document: bytes | None) -> bool:
    """A plain `type: string` schema, optionally with length bounds.

    Measured 19.08.2026 on grok 1.0.4 / grok-4.6 (#392): sending this shape as
    `--json-schema` forces one JSON document. The model fills it with an
    announcement or trails `<|eos|>`. `structuredOutput` is the parsed `text`,
    not a later answer — Extra-data leaves the twin absent
    (`structuredOutputError`); an announcement twin is the same sentence.
    Those bytes stay off the flag. Object schemas still travel.
    """

    if document is None:
        return False
    try:
        payload = json.loads(document)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or payload.get("type") != "string":
        return False
    return set(payload) <= _BARE_STRING_SCHEMA_FIELDS


def _json_schema_flag(declared_output_schema_bytes: bytes | None) -> tuple[str, ...]:
    """The `--json-schema` pair, or nothing where the node declared none.

    These are the exact published document bytes the output seam later
    judges -- not a second serialization. Measured on grok 1.0.4: the flag
    constrains the model and implies `--output-format json`; a provider that
    ignores it is still refused by the seam. The CLI accepts
    `{"type":"string"}` and refuses a boolean schema (`true`) with
    "must be a JSON object describing a JSON Schema"; this seam does not
    rewrite that form. A bare string schema is the exception: the flag stays
    off and the decoder serializes free `text` itself (see
    `_is_bare_string_schema`). Re-measured 19.08.2026 on build d846eb93d9:
    other schemas still carry `structuredOutput` as the parsed `text`. The
    decoder still takes `text` for those, because a string body needs those
    JSON bytes; the parsed value is the unquoted body.
    """
    if declared_output_schema_bytes is None or _is_bare_string_schema(
        declared_output_schema_bytes
    ):
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


def _headless_arguments(
    executable: Path,
    model: str,
    job_file: Path,
    declared_output_schema_bytes: bytes | None,
) -> tuple[str, ...]:
    """The exact argument vector one tool-free invocation is launched with.

    Measured on grok 1.0.4 (build d846eb93d9): `-p` is the short alias for
    `--single <PROMPT>`, a flag that requires an inline value, not the bare
    print flag this executor once assumed -- the CLI refuses the launch with
    "a value is required for '--single <PROMPT>' but none was supplied"
    before any billing occurs. `--prompt-file` is left as the only
    single-turn carrier.
    """

    return (
        str(executable),
        _OUTPUT_FORMAT_FLAG,
        _JSON_OUTPUT_FORMAT,
        *_json_schema_flag(declared_output_schema_bytes),
        _MODEL_FLAG,
        model,
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
    )


def _json_values(standard_output: bytes) -> tuple[object, ...] | None:
    """Read every JSON value Grok concatenated onto standard output.

    Grok 1.0.4 does not put a separator between its JSON values. `raw_decode`
    returns the first value and the character where the next one begins, which
    makes it the framing owner instead of treating a whole stream as one JSON
    instance. Invalid UTF-8 and an unreadable value leave the raw frame for the
    transcript rather than inventing a partial answer.
    """

    try:
        source = standard_output.decode("utf-8")
    except UnicodeDecodeError:
        return None
    decoder = json.JSONDecoder()
    values: list[object] = []
    index = 0
    while index < len(source):
        while index < len(source) and source[index] in " \t\r\n":
            index += 1
        if index == len(source):
            break
        try:
            value, index = decoder.raw_decode(source, index)
        except (ValueError, RecursionError):
            return None
        values.append(value)
    return tuple(values)


def _canonical_json(value: object) -> str:
    """One decoded provider value in the transcript's readable representation."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _grok_value_step(value: object) -> TranscriptEvent:
    """Keep narration as speech and every other intermediate shape as evidence."""

    if isinstance(value, str):
        return AssistantTurn(value)
    return UnrecognisedProviderOutput(_canonical_json(value))


def _grok_transcript(values: Sequence[object]) -> AttemptTranscript | None:
    """What Grok published before an answer this adapter may accept, if any."""

    return (
        AttemptTranscript.of(_grok_value_step(value) for value in values)
        if values
        else None
    )


def _unreadable_grok_transcript(standard_output: bytes) -> AttemptTranscript | None:
    """Keep an unreadable raw frame as bounded, redacted evidence."""

    if not standard_output:
        return None
    return AttemptTranscript.of(
        [UnrecognisedProviderOutput(standard_output.decode("utf-8", "replace"))]
    )


@dataclass(frozen=True)
class GrokSubscriptionProcessCommand(AgentProcessCommand):
    """One Grok headless command plus the decode mark that travels with it.

    A bare string schema never takes `--json-schema`. The model writes free
    text; decode serializes it as one JSON string. That yes/no is this field,
    set at prepare and read at decode. Spark 5341439755 / Desk 5341572142:
    a HOME-keyed executor set was a prepare→decode state bridge. The
    invocation is the correlation object; this executor holds none.
    """

    serialize_free_text_as_json_string: bool = field(default=False, kw_only=True)


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

    def _invocation_arguments(
        self,
        model: str,
        job_file: Path,
        declared_output_schema_bytes: bytes | None,
        maximum_assistant_turns: int | None = None,
    ) -> tuple[str, ...]:
        del maximum_assistant_turns
        return _headless_arguments(
            self.settings.executable,
            model,
            job_file,
            declared_output_schema_bytes,
        )

    _unsupported_auth_message = (
        "the Grok subscription executor serves subscription profiles only"
    )

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        binding = request.resolved_binding
        if binding.auth_profile.auth_mode is not AuthMode.SUBSCRIPTION:
            raise GrokSubscriptionAuthModeUnsupported(self._unsupported_auth_message)
        settings = self.settings
        state_directory, job_file = _open_job_directory(settings, request.job_bytes)
        registered = False
        try:
            attest_grok_containment(settings, state_directory)
            command = GrokSubscriptionProcessCommand(
                self._invocation_arguments(
                    binding.configuration.model,
                    job_file,
                    request.declared_output_schema_bytes,
                    request.maximum_assistant_turns,
                ),
                _child_environment(settings, state_directory),
                b"",
                standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
                serialize_free_text_as_json_string=_is_bare_string_schema(
                    request.declared_output_schema_bytes
                ),
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
        values = _json_values(completion.standard_output)
        if values is None:
            return _unusable_provider_answer(
                _unreadable_grok_transcript(completion.standard_output)
            )
        if completion.return_code != 0:
            return _unusable_provider_answer(_grok_transcript(values))
        if not values:
            return GrokProviderEndedWithoutFinalMessage()
        envelope = values[-1]
        if not isinstance(envelope, dict):
            return GrokProviderEndedWithoutFinalMessage(_grok_transcript(values))
        # Last value wins: only the final JSON value can be the envelope. Measured
        # on grok 1.0.4 / grok-4.6 (#392), values before it are progress messages;
        # a progress value after an envelope is therefore
        # GrokProviderEndedWithoutFinalMessage. `text` is the final answer and
        # `thought` is narration. `--json-schema`
        # adds `structuredOutput` as the parsed form of `text`, not a later
        # assistant message. An empty or missing `text` is a named refusal —
        # passing narration, the parsed value, or the raw frame would hand the
        # schema seam the story of the run or an unquoted string body.
        # A bare string schema never took that flag: free `text` is serialized
        # here so the seam sees one JSON string by construction. The yes/no
        # travels on the command (see `GrokSubscriptionProcessCommand`);
        # HOME and executor state are not consulted.
        text = envelope.get(_TEXT_FIELD)
        if not isinstance(text, str) or text == "":
            return GrokProviderEndedWithoutFinalMessage(_grok_transcript(values))
        command = invocation.command
        canonicalize = (
            isinstance(command, GrokSubscriptionProcessCommand)
            and command.serialize_free_text_as_json_string
        )
        if canonicalize:
            output_bytes = json.dumps(text, ensure_ascii=False).encode("utf-8")
        else:
            output_bytes = text.encode("utf-8")
        if len(output_bytes) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            return _unusable_provider_answer(_grok_transcript(values))
        return AgentExecutionResult(output_bytes, _grok_transcript(values[:-1]))

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


GROK_WORKSPACE_TOOLS_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("xai"), AgentExecutorRevision("grok-subscription-tools/v1")
)
# A second operation of the same CLI, not a later revision of the first. Its
# argument vector differs from the tool-free one in the tool grant, and that
# decision is what an operational identity stands for, so every durable
# attempt record keeps saying which of the two ran.
GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-workspace-tools-json/v1"
)

# Headless user-guide names `run_terminal_cmd`; Getting Started names
# `run_terminal_command`. Measured on grok 1.0.4: both parse, because the CLI
# does not check tool IDs at parse time. This executor is a headless
# operation, so it offers the Headless-documented ID. That the model then
# actually calls this ID is the billed secret-file probe after landing, not a
# parse proof.
WORKSPACE_TOOLS = (
    "read_file",
    "list_dir",
    "grep",
    "search_replace",
    "run_terminal_cmd",
)
_WORKSPACE_TOOL_LIST = ",".join(WORKSPACE_TOOLS)
# Permission classes, not tool IDs. `--allow` is repeatable; `--allowedTools`
# is the same flag. `--always-approve` and `--permission-mode
# bypassPermissions` exist and parse; both would run every tool. `dontAsk`
# plus these five classes is the grant: only those rules plus built-in
# read-only, no silent all-tools.
WORKSPACE_ALLOW_RULES = ("Read", "Edit", "Write", "Grep", "Bash")
# Docs: MCP meta-tools stay visible unless denied. `--deny MCPTool` parses.
# Combined with private HOME and the inert compat config, that is the MCP
# containment; the executor does not claim OS isolation.
_MCP_TOOL_DENY_RULE = "MCPTool"

# What the CLI says when it could not read an argument, measured on grok
# 1.0.4 against a flag no release can know. A Clap refusal without an
# isolated HOME can exit 0, so return code 0 is not a parse proof. The probe
# runs in the same private HOME/GROK_HOME a job would get. The marker is
# `unexpected argument`, not Claude's `unknown option`.
_ARGUMENT_REFUSAL_MARKER = "unexpected argument"
_UNKNOWN_FLAG_CONTROL = "--atelier2-no-grok-knows-this"
# The probe never reaches a model, so this names none: it keeps the vector's
# shape exact while saying plainly that no billed call stands behind it.
_INVOCATION_PROBE_MODEL = "atelier2-invocation-probe"
_INVOCATION_PROBE_OUTPUT_BYTES = 16_384
_INVOCATION_PROBE_PREFIX = "atelier2-grok-invocation-"


def _workspace_tool_arguments(
    executable: Path,
    model: str,
    job_file: Path,
    declared_output_schema_bytes: bytes | None,
    maximum_assistant_turns: int | None = None,
) -> tuple[str, ...]:
    """The exact argument vector one workspace-tool invocation is launched with."""

    allow: list[str] = []
    for rule in WORKSPACE_ALLOW_RULES:
        allow.extend((_ALLOW_FLAG, rule))
    return (
        str(executable),
        _OUTPUT_FORMAT_FLAG,
        _JSON_OUTPUT_FORMAT,
        *_json_schema_flag(declared_output_schema_bytes),
        _MODEL_FLAG,
        model,
        _PROMPT_FILE_FLAG,
        str(job_file),
        _TOOLS_OPTION,
        _WORKSPACE_TOOL_LIST,
        *allow,
        _DENY_FLAG,
        _MCP_TOOL_DENY_RULE,
        _PERMISSION_MODE_FLAG,
        _DONT_ASK,
        _NO_MEMORY_FLAG,
        _NO_SUBAGENTS_FLAG,
        _NO_WEB_SEARCH_FLAG,
        _MAXIMUM_TURNS_FLAG,
        (
            str(maximum_assistant_turns)
            if maximum_assistant_turns is not None
            else _HEADLESS_MAXIMUM_TURNS
        ),
    )


def _jobless_invocation_answer(
    settings: GrokSubscriptionSettings,
    arguments: tuple[str, ...],
    state_directory: Path,
    timeout_seconds: float,
) -> str:
    """Start this exact invocation with no credentials, and read back how it refused.

    Both streams are read, because what has to be told apart here is only said
    on the diagnostic one. The call is handed the deployment's search path and
    a private HOME/GROK_HOME -- the launch environment a job would get --
    because a Clap refusal without that isolation can exit 0. Auth is not
    copied: a prompt file with credentials would be a billed call.
    """

    try:
        process = subprocess.Popen(
            arguments,
            cwd=state_directory,
            env=dict(_child_environment(settings, state_directory)),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as error:
        raise GrokExecutableUnsupported(
            f"the Grok executable could not start this executor's invocation: {error}"
        ) from error
    try:
        return_code, answer, diagnostics = bounded_process_streams(
            process, timeout_seconds, _INVOCATION_PROBE_OUTPUT_BYTES
        )
    except OSError as error:
        raise GrokExecutableUnsupported(
            f"the Grok executable could not start this executor's invocation: {error}"
        ) from error
    if return_code == 0:
        raise GrokExecutableUnsupported(
            "the Grok executable answered a jobless invocation successfully: "
            "this probe rests on a call with no credentials ending in a "
            "refusal, so a release that runs one instead has to be measured "
            "again before this executor may be composed against it"
        )
    return (answer + diagnostics).decode("utf-8", "replace")


def attest_grok_workspace_tool_invocation(
    settings: GrokSubscriptionSettings,
    timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS,
) -> None:
    """Refuse an executable that cannot start this executor's exact invocation.

    A version answer is not startability. So this launches the argument vector
    the workspace-tool executor really prepares -- every flag, a private HOME
    -- and hands it no credentials: a CLI that read the whole vector reaches
    its own unsigned-in refusal, and a CLI that did not stops at the argument
    it could not read. Neither reaches a model, so the attestation is free
    and runs at every composition rather than at the first run that binds a
    node.

    The negative observation only means something if this executable can still
    make the positive one, so the control runs beside it: the same vector with
    one flag no release can know must be refused as an unexpected argument.
    Without that control, "said nothing about an unexpected argument" would
    also be what a release that stopped saying it looks like.
    """

    state_directory = Path(
        tempfile.mkdtemp(prefix=_INVOCATION_PROBE_PREFIX, dir=settings.workspace)
    )
    try:
        os.chmod(state_directory, _JOB_DIRECTORY_MODE)
        _write_private_file(
            state_directory / _CONFIG_FILE_NAME,
            _configuration_bytes(),
            _CONFIG_FILE_MODE,
        )
        job_file = state_directory / _JOB_FILE_NAME
        _write_private_file(job_file, b"", _JOB_FILE_MODE)
        arguments = _workspace_tool_arguments(
            settings.executable, _INVOCATION_PROBE_MODEL, job_file, None
        )
        started = _jobless_invocation_answer(
            settings, arguments, state_directory, timeout_seconds
        )
        if _ARGUMENT_REFUSAL_MARKER in started:
            raise GrokExecutableUnsupported(
                "the Grok executable refused an argument of this executor's "
                f"invocation: {started.strip()}. Serving workspace-tool agents "
                "needs every flag of that vector to exist and parse, because "
                "each one is a containment decision this executor states"
            )
        control = _jobless_invocation_answer(
            settings,
            (*arguments, _UNKNOWN_FLAG_CONTROL),
            state_directory,
            timeout_seconds,
        )
        if _ARGUMENT_REFUSAL_MARKER not in control:
            raise GrokExecutableUnsupported(
                "the Grok executable did not refuse a flag no release can "
                f"know, answering instead: {control.strip()}. The probe above "
                "reads a missing flag out of exactly that refusal, so an "
                "executable that never states one cannot be attested by it"
            )
    finally:
        try:
            shutil.rmtree(state_directory)
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class GrokWorkspaceToolExecutor(GrokSubscriptionExecutor):
    """One headless `grok` call that may use tools where it stands.

    This is the tool-free executor's sibling and deliberately not its successor.
    The two differ in the tool grant, and a node keeps the tool-free one unless
    its durable binding asks for `HEADLESS_WITH_TOOLS`; the capability the
    factory below declares is what makes that ask the only way to reach this
    class.

    WHAT IT GRANTS. Grok splits the two switches Claude combines: `--tools`
    names the built-in IDs the model may see, and `--allow` names the five
    permission classes it may run without asking, under `--permission-mode
    dontAsk`. `--deny MCPTool` keeps MCP meta-tools from remaining visible.
    Both halves are measured, not chosen: parse does not check tool IDs, so
    this executor does not pretend it validates them.

    WHAT IT KEEPS. Every other flag and the whole private HOME of the
    tool-free call, unchanged: `--prompt-file`, `--output-format json`, the
    inert compatibility configuration, no memory, no subagents, no web
    search, a bounded turn count. The decoder still takes `text` and refuses
    an empty or missing one; it does not hop the output schema.

    WHAT IT DOES NOT CLAIM. No operating-system isolation. The process runs as
    the serving user, and the named tools reach every path that user reaches
    -- including the credential directory this invocation hands it. The
    attempt's workspace is where the process is *started*, not a boundary it
    is held inside. `--always-approve`, `--yolo` and `bypassPermissions` exist
    and are not used. `--sandbox` exists and is not claimed. A tool this
    vector did not name cannot be used; where a named tool may reach is the
    CLI's own business and no promise of this module's.

    WHAT IS NOT MEASURED, said here rather than discovered later. The
    tool-free executor rests on a measured envelope against a real
    subscription answer. This one has no billed tool call yet on any release:
    what is measured is that the vector starts and parses whole in a private
    HOME. That a real answer then uses exactly these tools -- in particular
    the Headless-documented shell ID -- is the half a billed secret-file
    probe has to establish, under the operator's gate, after landing. Until
    it is run, this executor is composed only where an operator armed it by
    name.
    """

    _unsupported_auth_message = (
        "the Grok workspace-tool executor serves subscription profiles only"
    )

    def _invocation_arguments(
        self,
        model: str,
        job_file: Path,
        declared_output_schema_bytes: bytes | None,
        maximum_assistant_turns: int | None = None,
    ) -> tuple[str, ...]:
        return _workspace_tool_arguments(
            self.settings.executable,
            model,
            job_file,
            declared_output_schema_bytes,
            maximum_assistant_turns,
        )


@dataclass(frozen=True)
class GrokWorkspaceToolExecutorFactory:
    """The host-composed factory for one Grok workspace-tool executor."""

    settings: GrokSubscriptionSettings

    @property
    def key(self) -> AgentExecutorKey:
        return GROK_WORKSPACE_TOOLS_EXECUTOR_KEY

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        """Only headless-with-tools, and the omissions are the guard.

        Plain `HEADLESS` is missing on purpose. A configuration asking for it is
        asking for a call that can touch nothing, and answering that ask with
        this invocation would hand a node tools its binding never requested. The
        tool-free executor serves it, and a binding that names this executor's
        revision while asking for `HEADLESS` is refused by the starter rather
        than quietly widened. Interactive is missing for the same reason it is
        missing from the tool-free executor: there is no terminal here.
        """

        return frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS})

    def open(self) -> GrokWorkspaceToolExecutor:
        return GrokWorkspaceToolExecutor(self.settings)
