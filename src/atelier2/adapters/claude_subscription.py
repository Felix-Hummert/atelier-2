from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
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
    ToolCalled,
    ToolReturned,
    TranscriptEvent,
    UnrecognisedProviderOutput,
    Usage,
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
from atelier2.contracts.schemas_v3 import (
    SchemaAccepted,
    declared_instance_in_answer,
    read_schema_document,
)
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentExecutorKey,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)

CLAUDE_SUBSCRIPTION_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("anthropic"), AgentExecutorRevision("claude-subscription/v1")
)
# The operation, not the release that performs it: one headless print call whose
# argument vector, environment and stream contract are what this name stands
# for. Admitting a version therefore leaves it alone while the matrix finds that
# operation unchanged, and every durable attempt record keeps comparing against
# the same identity. A release that changed one of them -- a control that stops
# meaning what it meant, an envelope this executor must decode differently --
# would be a different operation and would need a new identity here rather than
# a widened conformance set.
#
# V2 is exactly that. The vector now asks for `--output-format stream-json`
# (with the `--verbose` that format demands) instead of `json`, so the process
# writes every turn and tool result on its way to the same final envelope
# (#666). A different vector reaching a different wire shape is a different
# operation, whatever it ends up answering, so the name moves with it and no
# attempt recorded under V1 is ever read as having run this one.
CLAUDE_SUBSCRIPTION_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-print-stream-json/v2"
)

# The largest final `result` line one call may write. It is deliberately larger
# than the durable output bound: the line carries the durable result *inside* a
# JSON envelope, so a raw bound equal to MAXIMUM_AGENT_OUTPUT_BYTES_V2 would
# refuse answers the durable contract accepts. Derivation, so no durably
# acceptable result is ever refused as a frame:
#   * JSON string escaping expands one source byte to at most six frame bytes
#     (a C0 control byte becomes a six-character backslash-u escape), so a
#     durable-legal result occupies at most 6 * 49,152 = 294,912 frame bytes.
#   * The remaining 98,304 bytes are the envelope metadata allowance. The
#     largest metadata a real `claude -p` answer of a conformant version was
#     measured carrying around its result is 1,631 bytes, so the allowance is
#     about sixty times the observed envelope -- room for more models in
#     `modelUsage`, permission denials and future fields without ever bounding
#     an answer the durable contract would accept.
CLAUDE_SUBSCRIPTION_ENVELOPE_BYTES = 8 * MAXIMUM_AGENT_OUTPUT_BYTES_V2

# The largest raw frame one whole `--output-format stream-json` call may write
# on standard output. The port owns the field; this number is this provider's,
# because it is this provider's wire format that produces the frame.
#
# The stream is the envelope above plus everything the call wrote before it, and
# what it wrote before it is exactly the steps a transcript may keep: one whole
# transcript's worth is therefore the allowance, and a stream past it is one
# whose steps this repository could not have kept whole anyway. The two halves
# are added rather than shared because they bound different things -- the final
# answer, and the story that led to it -- and a single number would silently
# trade one against the other.
CLAUDE_SUBSCRIPTION_FRAME_BYTES = (
    CLAUDE_SUBSCRIPTION_ENVELOPE_BYTES + MAXIMUM_ATTEMPT_TRANSCRIPT_BYTES
)

# Every Claude Code whose containment behaviour was measured end to end for
# this executor. A version bound would only prove that the flags were
# introduced, never that a later release still means by them what this executor
# claims, so conformance is an exact reviewed set rather than a floor. A version
# enters it only once the one-call conformance matrix has been rerun against
# that exact executable. That rerun establishes for the new member that every
# flag and variable the invocation below carries exists there and really parses,
# what the whole vector leaves standing when it acts together, and the envelope,
# meter and frame shape this module decodes. The observations a single contained
# call cannot make are named where that invocation records them and stay
# recorded from the release they were first run against -- naming one version
# instead of the set would still say less than the set.
CONFORMANT_CLAUDE_VERSIONS = frozenset({(2, 1, 221), (2, 1, 233)})

_VERSION_FLAG = "--version"
# How long this deployment waits for one probe of this CLI to answer. Both
# probes below share it: neither reaches a model, both are one short startup,
# and a deployment that wanted to wait differently for one of them would be
# saying something about the executable rather than about the probe.
_PROBE_TIMEOUT_SECONDS = 30.0
# The measured answer is one short line. The probe reads an external program's
# streams, so it holds them to the smallest bound that answer fits inside
# rather than to whatever the program decides to write.
_VERSION_PROBE_OUTPUT_BYTES = 4_096

# Every directory this CLI treats as an administrator policy root and every
# entry it loads from one, read out of each conformant executable: the platform
# switch answers `/etc/claude-code`, the macOS bundle directory, or the Windows
# policy directory, and a WSL host reads that Windows directory through DrvFs.
# The product of roots and entries is deliberately a superset of what any one
# host reads, because a policy surface this attestation does not name is a
# surface it silently permits.
MANAGED_POLICY_ROOTS = (
    Path("/etc/claude-code"),
    Path("/Library/Application Support/ClaudeCode"),
    Path("/mnt/c/Program Files/ClaudeCode"),
)
MANAGED_POLICY_ENTRIES = (
    "managed-settings.json",
    "managed-settings.d",
    "managed-mcp.json",
)
# Managed settings delivered by Anthropic to a managed account are cached under
# the credential directory, so the deployment's own directory is the fourth
# policy root and this is its entry.
REMOTE_MANAGED_POLICY_ENTRY = "remote-settings.json"

# The credential record the CLI keeps in that same directory, and the field of
# it that decides whether server-managed settings are delivered at all. Read
# out of each conformant executable: a first-party OAuth session fetches managed
# settings at startup -- after any scan of the cache above could see them --
# when this persisted subscription type is `enterprise` or `team`, and also
# when it is absent, and it does not fetch them for a personal subscription.
# So the delivery this executor cannot scan for is refused by attesting the
# same value the CLI's own delivery gate reads, and an absent or unreadable
# type is refused rather than read as personal.
CREDENTIAL_RECORD_ENTRY = ".credentials.json"
PERSONAL_SUBSCRIPTION_TYPES = frozenset({"max", "pro"})
_OAUTH_CREDENTIAL_FIELD = "claudeAiOauth"
_SUBSCRIPTION_TYPE_FIELD = "subscriptionType"

_PRINT_FLAG = "-p"
_OUTPUT_FORMAT_FLAG = "--output-format"
# The format that publishes the steps as well as the answer, and the flag it
# insists on. Measured on 2.1.221 (#694): asked for this format without
# `--verbose` the CLI exits 1 saying so, and with it standard output is pure
# NDJSON -- a session header, the assistant turns and tool results, and last a
# `result` line whose key set is exactly what `--output-format json` produced.
# The two therefore travel as one decision and are never spelled apart.
_STREAM_JSON_OUTPUT_FORMAT = "stream-json"
_VERBOSE_FLAG = "--verbose"
_MODEL_FLAG = "--model"

# The containment flags, each measured against every conformant version (see
# the class docstring for what each one was observed to stop). The two that
# carry an empty value use the `--flag=` equals form deliberately: the process
# seam refuses an empty argument vector element, and the equals form states
# "this option, with nothing" in a single nonempty argument.
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
# The tool-free executor intentionally does not consume a workflow Budget. Its
# fixed one-turn cap is an internal safety invariant even when a request carries
# a bound: one turn can print the answer and no unbounded subscription loop can
# start.
_HEARTBEAT_MAXIMUM_TURNS = "1"

_CREDENTIAL_DIRECTORY_VARIABLE = "CLAUDE_CONFIG_DIR"
_SEARCH_PATH_VARIABLE = "PATH"
# Read straight out of each conformant executable: the prompt-history writer
# returns before appending when this is set, so the operator's history file
# never receives a job this seam sent. It is distinct from session persistence,
# which owns the resumable transcript.
_SKIP_PROMPT_HISTORY_VARIABLE = "CLAUDE_CODE_SKIP_PROMPT_HISTORY"
_SKIP_PROMPT_HISTORY = "1"
# Also read straight out of those executables: a value parsed as a finite number
# of at least zero replaces the built-in retry count, so a billed call cannot
# silently become several. Zero is the bound this heartbeat wants. What the
# reading has to establish for each conformant version is that zero itself
# reaches the override, because a release that read it as "unset" would quietly
# restore its own count.
_MAXIMUM_RETRIES_VARIABLE = "CLAUDE_CODE_MAX_RETRIES"
_NO_RETRIES = "0"
# Defence in depth for the child this invocation does not expect to have: set,
# the CLI strips Anthropic and cloud-provider credentials from every subprocess
# environment. That the tool-free call answers exactly as it does unset is
# recorded from the release this invocation was first measured against.
# Measured on every conformant version against the invocation below: on a
# search path where bubblewrap does not resolve the CLI refuses to start at all
# rather than running unscrubbed -- which is why `ClaudeSubscriptionSettings`
# proves bubblewrap is reachable before this deployment composes.
_SUBPROCESS_ENVIRONMENT_SCRUB_VARIABLE = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"
_SCRUB_SUBPROCESS_ENVIRONMENT = "1"
_BUBBLEWRAP_EXECUTABLE = "bwrap"

_ENVELOPE_TYPE_FIELD = "type"
_RESULT_ENVELOPE_TYPE = "result"
_ERROR_FLAG_FIELD = "is_error"
_RESULT_FIELD = "result"

# The stream line shapes this executor reads as steps. Everything else on the
# stream is kept whole rather than mapped, so a line shape a release adds is
# evidence this transcript still carries instead of a step it silently drops.
_ASSISTANT_LINE_TYPE = "assistant"
_USER_LINE_TYPE = "user"
_MESSAGE_FIELD = "message"
_CONTENT_FIELD = "content"
_TEXT_BLOCK_TYPE = "text"
_TOOL_USE_BLOCK_TYPE = "tool_use"
_TOOL_RESULT_BLOCK_TYPE = "tool_result"
_TEXT_FIELD = "text"
_TOOL_NAME_FIELD = "name"
_TOOL_INPUT_FIELD = "input"
_TOOL_USE_ID_FIELD = "id"
_ANSWERED_TOOL_USE_ID_FIELD = "tool_use_id"
_USAGE_FIELD = "usage"
_INPUT_TOKENS_FIELD = "input_tokens"
_OUTPUT_TOKENS_FIELD = "output_tokens"
_CACHE_READ_TOKENS_FIELD = "cache_read_input_tokens"
_CACHE_CREATION_TOKENS_FIELD = "cache_creation_input_tokens"


def _unusable_provider_answer(
    transcript: AttemptTranscript | None,
) -> AgentExecutionFailure:
    """This call produced no answer this executor may use, with what it did write.

    The durable attempt contract knows exactly one failure code today, so every
    unusable provider answer projects onto it until that enum, its SQL guards
    and the frozen attempt wire field gain a wider vocabulary together. What
    tells two such endings apart is the transcript travelling with it.
    """

    return AgentExecutionFailure(
        AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY, transcript
    )


class ClaudeSubscriptionAuthModeUnsupported(ValueError):
    """A published configuration bound a non-subscription profile to this executor."""


class ClaudeExecutableUnsupported(ValueError):
    """The named executable is not a Claude Code this executor was measured against."""


class ClaudeManagedPolicyPresent(ValueError):
    """Administrator policy can act here, and no invocation flag disables it."""


def _parsed_version(reported: str) -> tuple[int, int, int] | None:
    """Read `2.1.221 (Claude Code)` as the version it names."""

    leading = reported.strip().split(" ", 1)[0]
    parts = leading.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    major, minor, patch = (int(part) for part in parts)
    return major, minor, patch


def read_claude_version(
    executable: Path, timeout_seconds: float = _PROBE_TIMEOUT_SECONDS
) -> tuple[int, int, int]:
    """Ask one executable which Claude Code it is. Runs it with `--version`.

    The containment this executor claims is a property of the flags of a
    measured release, not of every file named `claude`, so the deployment reads
    the answer instead of assuming it.

    This is the first execution of the external binary and therefore the start
    of its trust boundary, so it gets no more than the billed invocation does:
    `--version` was measured to need no environment at all, so it receives
    none, and it runs in an empty private directory rather than wherever the
    server happens to stand. Both its streams are read under one byte bound and
    one deadline, and whatever the probe leaves running is killed as a session.
    """

    with tempfile.TemporaryDirectory(prefix="atelier2-claude-version-") as probe_root:
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
            raise ClaudeExecutableUnsupported(
                f"the Claude executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
        try:
            return_code, answer = bounded_process_answer(
                process, timeout_seconds, _VERSION_PROBE_OUTPUT_BYTES
            )
        except OSError as error:
            raise ClaudeExecutableUnsupported(
                f"the Claude executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
    if return_code != 0:
        raise ClaudeExecutableUnsupported(
            f"the Claude executable refused {_VERSION_FLAG} "
            f"with exit code {return_code}"
        )
    version = _parsed_version(answer.decode("utf-8", "replace"))
    if version is None:
        raise ClaudeExecutableUnsupported(
            f"the Claude executable did not report a version at {_VERSION_FLAG}"
        )
    return version


def verify_claude_capability(
    executable: Path, timeout_seconds: float = _PROBE_TIMEOUT_SECONDS
) -> tuple[int, int, int]:
    """Refuse an executable outside the reviewed conformance set."""

    version = read_claude_version(executable, timeout_seconds)
    if version not in CONFORMANT_CLAUDE_VERSIONS:
        reported = ".".join(str(part) for part in version)
        conformant = ", ".join(
            ".".join(str(part) for part in candidate)
            for candidate in sorted(CONFORMANT_CLAUDE_VERSIONS)
        )
        raise ClaudeExecutableUnsupported(
            f"serving Claude subscription agents requires Claude Code {conformant}, "
            f"not {reported}: this executor's containment semantics were measured "
            "against those exact releases, and another one can change a control "
            "they depend on. Admitting a new version needs the one-call conformance "
            "matrix rerun against it and a decision about this executor's "
            "operational identity, not a version comparison"
        )
    return version


def _managed_policy_surfaces(
    credential_directory: Path, policy_roots: Sequence[Path]
) -> Iterator[Path]:
    for root in policy_roots:
        for entry in MANAGED_POLICY_ENTRIES:
            yield root / entry
    yield credential_directory / REMOTE_MANAGED_POLICY_ENTRY


def _stored_subscription_type(credential_directory: Path) -> str | None:
    """Read the subscription type the CLI's delivery gate reads, or nothing."""

    record = credential_directory / CREDENTIAL_RECORD_ENTRY
    try:
        stored: object = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(stored, dict):
        return None
    oauth = stored.get(_OAUTH_CREDENTIAL_FIELD)
    if not isinstance(oauth, dict):
        return None
    subscription_type = oauth.get(_SUBSCRIPTION_TYPE_FIELD)
    return subscription_type if isinstance(subscription_type, str) else None


def attest_no_managed_policy(
    credential_directory: Path, policy_roots: Sequence[Path]
) -> None:
    """Refuse composition where administrator policy can still act.

    The tool-free containment premise this executor rests on holds only where
    no managed policy can act, and this attestation is what enforces exactly
    that. Safe mode, `--setting-sources=` and `--tools=` stop project and user
    customization, but administrator policy is outside all of them by design --
    safe mode's own help says so -- so policy-configured hooks, status line and
    file-suggestion commands would still run, and each of those is a child
    process beside the credential directory this invocation is handed. Refusing
    the deployment is the only honest answer, because no flag can disable them.

    Policy reaches a run two ways, and both are refused here. It can already be
    on disk, which the policy surfaces above are scanned for; or it can be
    delivered by Anthropic to a managed account when the CLI starts, which no
    scan preceding the call can ever see, and which a non-interactive `claude
    -p` applies for that run. What decides whether that delivery happens at all
    is the account, so the credential offered for it is attested to be a
    personal subscription -- reading the same persisted subscription type the
    CLI's own delivery gate reads, and refusing every other value including an
    absent one, because an unknown type is a fetching one.

    That is a disk attestation of the account, not proof about the network: it
    trusts a record the CLI itself writes, and it is read once at composition,
    so an account converted to a managed one while the server runs is outside
    it. Full OS isolation remains the planned stronger boundary and is what
    would make the premise hold regardless of the host or the account.
    """

    for surface in _managed_policy_surfaces(credential_directory, policy_roots):
        if surface.exists() or surface.is_symlink():
            raise ClaudeManagedPolicyPresent(
                "serving Claude subscription agents requires a host without "
                f"administrator-managed policy, but {surface} exists: this "
                "executor's containment rests on the invocation being tool-free, "
                "and managed policy can still start a child beside the "
                "credential directory whatever the invocation asks for"
            )
    subscription_type = _stored_subscription_type(credential_directory)
    if subscription_type not in PERSONAL_SUBSCRIPTION_TYPES:
        personal = ", ".join(sorted(PERSONAL_SUBSCRIPTION_TYPES))
        offered = subscription_type if subscription_type is not None else "no"
        raise ClaudeManagedPolicyPresent(
            "serving Claude subscription agents requires a personal "
            f"subscription credential ({personal}), but "
            f"{credential_directory / CREDENTIAL_RECORD_ENTRY} carries "
            f"{offered} subscription type: an account outside that set can be "
            "delivered administrator-managed settings when the CLI starts, "
            "which is after any policy scan can see them and still in time to "
            "run a hook for this invocation"
        )


@dataclass(frozen=True)
class ClaudeSubscriptionSettings:
    """The deployment values one Claude subscription executor may use.

    `search_path` is the serving host's own executable search path: the Claude
    CLI resolves its interpreter and its own child tools through it, and the
    launched process receives no other inherited environment. Because this
    executor asks the CLI to scrub every subprocess environment, that path must
    also resolve bubblewrap: measured on every conformant version, a CLI told to
    scrub without it refuses to start, so a deployment that could only fail at
    invocation time is refused here instead.
    """

    executable: Path
    credential_directory: Path
    search_path: str

    def __post_init__(self) -> None:
        executable = self.executable.resolve()
        credential_directory = self.credential_directory.resolve()
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "credential_directory", credential_directory)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("the Claude executable must be an executable file")
        if not credential_directory.is_dir():
            raise ValueError(
                "the Claude credential directory must be an existing directory"
            )
        if not self.search_path.strip():
            raise ValueError("the Claude executable search path must be nonempty")
        if shutil.which(_BUBBLEWRAP_EXECUTABLE, path=self.search_path) is None:
            raise ValueError(
                "the Claude executable search path must resolve "
                f"{_BUBBLEWRAP_EXECUTABLE}, because this executor asks the CLI to "
                "scrub every subprocess environment and the CLI refuses to start "
                "without it"
            )


def _credential_environment(
    settings: ClaudeSubscriptionSettings,
) -> tuple[tuple[str, str], ...]:
    """The complete environment every invocation of this CLI is handed.

    It is the same whatever the invocation may do, because it is the credential
    boundary plus the retry, prompt-history and subprocess hardening -- and none
    of those is a property of the operation's tool surface.
    """

    return (
        (_CREDENTIAL_DIRECTORY_VARIABLE, str(settings.credential_directory)),
        (_SEARCH_PATH_VARIABLE, settings.search_path),
        (_SKIP_PROMPT_HISTORY_VARIABLE, _SKIP_PROMPT_HISTORY),
        (_MAXIMUM_RETRIES_VARIABLE, _NO_RETRIES),
        (_SUBPROCESS_ENVIRONMENT_SCRUB_VARIABLE, _SCRUB_SUBPROCESS_ENVIRONMENT),
    )


@dataclass(frozen=True)
class ClaudeProcessCommand(AgentProcessCommand):
    """One Claude headless command plus the output schema its node declared.

    The schema is what the decode below narrows the answer against, and decode
    is handed the invocation rather than the request, so the document travels
    on the command. It is published bytes -- the same revision the output seam
    later judges -- so a command that may be durably retained carries nothing
    secret by carrying it.
    """

    declared_output_schema_bytes: bytes | None = field(default=None, kw_only=True)


def _declared_schema_of(command: AgentProcessCommand) -> SchemaAccepted | None:
    """The accepted schema this invocation's node declared, where there is one.

    A schema this profile cannot read is answered as none rather than refused:
    a published revision was vetted before it could be pinned, and an executor
    that started disagreeing about it would be a second voice over bytes the
    output seam already owns.
    """

    if not isinstance(command, ClaudeProcessCommand):
        return None
    document = command.declared_output_schema_bytes
    if document is None:
        return None
    schema = read_schema_document(document)
    return schema if isinstance(schema, SchemaAccepted) else None


def _stream_lines(standard_output: bytes) -> tuple[str, ...]:
    """Every line this call wrote that carries anything, as text.

    Decoding replaces what is not UTF-8 rather than refusing the frame: bytes a
    failing process wrote are the diagnosis somebody is looking for, and a
    transcript that vanished because one of them was malformed would be the
    silence this whole seam removes. What survives is still bounded and redacted
    before it is kept, by the transcript contract and nothing here.
    """

    return tuple(
        line
        for line in standard_output.decode("utf-8", "replace").splitlines()
        if line.strip()
    )


def _stream_entry(line: str) -> dict[str, object] | None:
    """This line read as one stream entry, or nothing where it is not one."""

    try:
        entry: object = json.loads(line)
    except json.JSONDecodeError:
        return None
    return entry if isinstance(entry, dict) else None


def _content_blocks(entry: dict[str, object]) -> tuple[object, ...]:
    """The content blocks this message line carries, or none."""

    message = entry.get(_MESSAGE_FIELD)
    if not isinstance(message, dict):
        return ()
    content = message.get(_CONTENT_FIELD)
    return tuple(content) if isinstance(content, list) else ()


def _token_count(usage: dict[str, object], field_name: str) -> int | None:
    """One nonnegative token count this usage record states, or nothing."""

    counted = usage.get(field_name, 0)
    if type(counted) is not int or counted < 0:
        return None
    return counted


def _usage_step(entry: dict[str, object]) -> Usage | None:
    """What this line says the attempt spent, where it says it completely.

    The final `result` line is the one this is read from: it carries the
    provider's own total, while a per-turn record would have to be added up here
    and this module would then be the second place that decides what an attempt
    cost.
    """

    usage = entry.get(_USAGE_FIELD)
    if not isinstance(usage, dict):
        return None
    counts = tuple(
        _token_count(usage, field_name)
        for field_name in (
            _INPUT_TOKENS_FIELD,
            _OUTPUT_TOKENS_FIELD,
            _CACHE_READ_TOKENS_FIELD,
            _CACHE_CREATION_TOKENS_FIELD,
        )
    )
    if any(count is None for count in counts):
        return None
    read, written, cached, created = counts
    return Usage(read or 0, written or 0, cached or 0, created or 0)


def _block_step(block: object, tool_names: dict[str, str]) -> TranscriptEvent | None:
    """One content block as the step it is, or nothing where it is not one.

    `tool_names` is how a result finds the door it answered: the provider names
    the tool on the call and refers back to it by id afterwards, so the call
    leaves its name here for the result to read. A result whose call this
    executor never saw keeps the id instead of borrowing another tool's name.
    """

    if not isinstance(block, dict):
        return None
    match block.get(_ENVELOPE_TYPE_FIELD):
        case value if value == _TEXT_BLOCK_TYPE:
            spoken = block.get(_TEXT_FIELD)
            return AssistantTurn(spoken) if isinstance(spoken, str) else None
        case value if value == _TOOL_USE_BLOCK_TYPE:
            name = block.get(_TOOL_NAME_FIELD)
            if not isinstance(name, str):
                return None
            called_id = block.get(_TOOL_USE_ID_FIELD)
            if isinstance(called_id, str):
                tool_names[called_id] = name
            return ToolCalled(name, _canonical_json(block.get(_TOOL_INPUT_FIELD)))
        case value if value == _TOOL_RESULT_BLOCK_TYPE:
            answered_id = block.get(_ANSWERED_TOOL_USE_ID_FIELD)
            if not isinstance(answered_id, str):
                return None
            answer = block.get(_CONTENT_FIELD)
            return ToolReturned(
                tool_names.get(answered_id, answered_id),
                answer if isinstance(answer, str) else _canonical_json(answer),
            )
        case _:
            return None


def _canonical_json(value: object) -> str:
    """These decoded bytes back as one exact line of JSON."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _line_steps(line: str, tool_names: dict[str, str]) -> tuple[TranscriptEvent, ...]:
    """The steps one stream line stands for, keeping whole what it does not.

    A line this executor cannot read as steps is kept as the provider's own
    output rather than dropped: the session header, a line shape a release
    added, and whatever a call that never produced a stream printed instead all
    arrive here, and each of them is evidence about an episode somebody is
    reading precisely because nothing explained it.
    """

    entry = _stream_entry(line)
    if entry is None:
        return (UnrecognisedProviderOutput(line),)
    match entry.get(_ENVELOPE_TYPE_FIELD):
        case value if value in {_ASSISTANT_LINE_TYPE, _USER_LINE_TYPE}:
            steps = tuple(
                step
                for step in (
                    _block_step(block, tool_names) for block in _content_blocks(entry)
                )
                if step is not None
            )
        case value if value == _RESULT_ENVELOPE_TYPE:
            spent = _usage_step(entry)
            steps = () if spent is None else (spent,)
        case _:
            steps = ()
    return steps if steps else (UnrecognisedProviderOutput(line),)


def _stream_transcript(lines: Sequence[str]) -> AttemptTranscript | None:
    """What this call did, as far as its own stream said so.

    Nothing where the call wrote no line at all -- an honest absence, which is
    what an executor whose process never started has to say.
    """

    tool_names: dict[str, str] = {}
    steps = tuple(step for line in lines for step in _line_steps(line, tool_names))
    return AttemptTranscript.of(steps) if steps else None


def _decoded_claude_answer(
    invocation: AgentProcessInvocation,
    completion: AgentProcessCompletion,
) -> AgentExecutionResult | AgentExecutionFailure:
    """Read one `claude -p --output-format stream-json` call back, or refuse it.

    The stream is the CLI's contract rather than one operation's, so every
    executor in this module reads it the same way and a release that changed it
    would move all of them at once. The answer is the last line: measured on
    2.1.221 (#694), a `--verbose` stream ends in a `result` line whose key set
    is exactly what `--output-format json` produced alone, so this reads the
    same envelope it always read and the lines before it are the steps that
    reached it.

    Every way through here carries a transcript, the refusals included. What a
    call wrote before it failed is the only account of an ending the exit code
    and an empty standard error explain nothing about (#733), and it is
    strictly more than the predecessor kept, which was nothing at all.

    Where the node declared an output schema, the answer is narrowed to the
    bytes that really are a value it admits (`declared_instance_in_answer`).
    Asking a model in prose for bare JSON made the conductor a coin flip: the
    same brief came back bare, fenced or introduced by a sentence, and only the
    first was decodable (#663). Narrowing is not acceptance -- an answer
    carrying no such value travels on unchanged, and the output seam refuses it
    by name as before.

    This CLI's own structured-output control is measured and deliberately not
    carried by any vector here. On 2.1.221 `--json-schema <object>` exists and
    really parses (an unparseable value is refused as "not valid JSON", a
    boolean schema as "must be a JSON object"), while `--output-schema` and
    `--structured-output` are unknown options. What keeps it out is what it is
    made of: the release fulfils it with a synthetic `StructuredOutput` TOOL
    the model must call, and delivers the value in a `structured_output` field
    beside `result` rather than in `result`. Every vector here passes
    `--tools=`, and the doors vector pre-approves door tools only, so whether
    that tool survives them -- and what `result` then carries -- is exactly
    what no unbilled probe can see. Wiring it unmeasured could turn an answer
    this module decodes today into no answer at all, so it waits on the same
    operator-gated billed call the rest of this module's unmeasured half does.
    """

    lines = _stream_lines(completion.standard_output)
    transcript = _stream_transcript(lines)
    if (
        completion.return_code != 0
        or len(completion.standard_output) > CLAUDE_SUBSCRIPTION_FRAME_BYTES
        or not lines
    ):
        return _unusable_provider_answer(transcript)
    envelope = _stream_entry(lines[-1])
    if envelope is None:
        return _unusable_provider_answer(transcript)
    result = envelope.get(_RESULT_FIELD)
    if (
        envelope.get(_ENVELOPE_TYPE_FIELD) != _RESULT_ENVELOPE_TYPE
        or envelope.get(_ERROR_FLAG_FIELD) is not False
        or not isinstance(result, str)
    ):
        return _unusable_provider_answer(transcript)
    output_bytes = result.encode("utf-8")
    if len(output_bytes) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
        return _unusable_provider_answer(transcript)
    schema = _declared_schema_of(invocation.command)
    declared = (
        None if schema is None else declared_instance_in_answer(output_bytes, schema)
    )
    return AgentExecutionResult(
        output_bytes if declared is None else declared, transcript
    )


@dataclass(frozen=True)
class ClaudeSubscriptionExecutor:
    """One bare headless `claude --print` invocation and its JSON envelope.

    The invocation is as close to text-in/text-out as subscription
    authentication allows, because the process is handed the operator's
    credential directory. It decides no working directory: the CLI writes into
    wherever it is started, so where that is belongs to the attempt that owns
    the process, not to this executor. Every flag and variable below exists in
    every conformant version and is measured there before it is admitted --
    against a workspace holding a `CLAUDE.md` and a `.claude/settings.json`
    whose `SessionStart` hook creates a file, and against the executable's own
    `--help` and strings, with a deliberately bogus flag as the control: it
    answers `error: unknown option`, so a call the whole vector survives really
    parsed all of it rather than ignoring what it no longer knows.

    What each switch stops is the observation below, and that whole list is
    recorded from the release it was first run against rather than from every
    conformant version. A single contained call shows what the vector leaves
    standing when the flags act together; it can attribute nothing to one of
    them, and neither the negative control that makes such an attribution mean
    anything nor the positive control beside it can be taken inside a call that
    contains everything. Each of those costs a further billed call against the
    operator's live credential directory. So does the credential-scrub
    equivalence recorded further below, which is of the same kind. What a
    rerun establishes for a new member is the other half: that this vector
    parses there, and that together these flags leave no hook, project prompt,
    skill, MCP server, tool or transcript behind.

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

    `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` is set as defence in depth. An earlier
    revision of this executor left it unset and claimed the variable was an
    opt-OUT that a tool-free call could not benefit from. That claim was wrong,
    and it is withdrawn: the "=0 to disable" diagnostics it rested on are what
    the CLI prints once the hardening is already active, while every conformant
    version reads the variable as the opt-IN that strips Anthropic and
    cloud-provider credentials from every subprocess environment, and refuses
    identically to start without bubblewrap when it is set. That set, the
    tool-free call answers exactly as it does unset is recorded from the release
    this invocation was first measured against. It is not this executor's
    containment guarantee -- it is what limits a child this invocation does not
    expect to have.

    The deployment contract this V1 is measured for, and the only one it is
    composed for: a personal subscription credential (`max` or `pro`), on a
    host carrying no administrator policy, serving a loopback deployment (see
    `atelier2.host`). The narrowing is not advice. Managed settings are
    delivered to a managed account when the CLI starts, which is after any scan
    this executor could run and still in time to bring a hook into the billed
    call, so `attest_no_managed_policy` refuses composition against a credential
    outside that set -- and against one that will not say which it is.

    Residual risk, deliberately not claimed as closed. The process still runs
    as the serving user and can read `CLAUDE_CONFIG_DIR`; tool-free, it spawns
    no descendant that could inherit it, but nothing in the operating system
    forbids one. The account attestation is a disk read of a record the CLI
    itself writes, taken once at composition, so it neither proves what the
    network will deliver nor survives an account converted while the server
    runs. The operator ruled on 2026-08-14 that OS-enforced isolation stays
    planned as its own slice but does not gate this local heartbeat; it is what
    would make the premise hold regardless of the host or the account.
    """

    settings: ClaudeSubscriptionSettings

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        binding = request.resolved_binding
        if binding.auth_profile.auth_mode is not AuthMode.SUBSCRIPTION:
            raise ClaudeSubscriptionAuthModeUnsupported(
                "the Claude subscription executor serves subscription profiles only"
            )
        settings = self.settings
        return ClaudeProcessCommand(
            (
                str(settings.executable),
                _PRINT_FLAG,
                _OUTPUT_FORMAT_FLAG,
                _STREAM_JSON_OUTPUT_FORMAT,
                _VERBOSE_FLAG,
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
            _credential_environment(settings),
            # The job travels through standard input so no prompt ever appears
            # in the process table of a host the operator shares.
            request.job_bytes,
            standard_output_frame_bytes=CLAUDE_SUBSCRIPTION_FRAME_BYTES,
            declared_output_schema_bytes=request.declared_output_schema_bytes,
        )

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        """The answer travels inside the process result; the invocation carries
        only the output schema it is narrowed against."""

        return _decoded_claude_answer(invocation, completion)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        """Nothing to take back: `CLAUDE_CONFIG_DIR` names the operator's own
        credential directory, and this executor copies it nowhere."""

        del command

    def close(self) -> None:
        return


@dataclass(frozen=True)
class ClaudeSubscriptionExecutorFactory:
    """The host-composed factory for one Claude subscription executor."""

    settings: ClaudeSubscriptionSettings

    @property
    def key(self) -> AgentExecutorKey:
        return CLAUDE_SUBSCRIPTION_EXECUTOR_KEY

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return CLAUDE_SUBSCRIPTION_OPERATIONAL_IDENTITY

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        """Only headless: every invocation this executor prepares is one print call.

        Interactive is the declared extra, and this executor has no seam for
        one -- the job travels on standard input, the process is given no
        terminal, and the call ends with the model's single answer. Declaring
        the extra it cannot serve is what the starter refuses a node's demand
        against, so the absence here is the guard, not an omission.
        """

        return frozenset({AgentExecutionCapability.HEADLESS})

    def open(self) -> ClaudeSubscriptionExecutor:
        return ClaudeSubscriptionExecutor(self.settings)


CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("anthropic"),
    AgentExecutorRevision("claude-subscription-tools/v1"),
)
# A second operation of the same CLI, not a later revision of the first. Its
# argument vector differs from the tool-free one in exactly one decision -- the
# tools this call may use -- and that decision is what an operational identity
# stands for, so every durable attempt record keeps saying which of the two ran.
# V2 for the same reason its sibling is: the vector asks for the transcript-
# bearing stream instead of a single envelope (#666), which is a different
# operation of the same CLI rather than a later revision of this one.
CLAUDE_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-workspace-tools-print-stream-json/v2"
)

# The exact built-in tools this operation offers, named once and used twice:
# `--tools` decides what the model can see, `--allowedTools` decides what it may
# use without asking. Both are needed and neither is a stand-in for the other.
# Measured on 2.1.233, the release this vector was first built against: with
# CLAUDE_CODE_SUBPROCESS_ENV_SCRUB set and no allowlist, the CLI forces the
# permission mode back to its default and says so -- "Declare allowedTools
# explicitly, or set CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=0 to opt out" -- and that
# announcement is gone once the allowlist is declared. So an explicit allowlist
# is the only grant this environment honours, and a `--permission-mode` would be
# accepted on the command line and then silently overridden. Naming the same set
# at `--tools` keeps every other built-in, the network-reaching ones included,
# out of the model's reach rather than visible and refused.
WORKSPACE_TOOLS = ("Bash", "Edit", "Glob", "Grep", "Read", "Write")
_WORKSPACE_TOOL_LIST = ",".join(WORKSPACE_TOOLS)
_TOOLS_FLAG = "--tools"
_ALLOWED_TOOLS_FLAG = "--allowedTools"
# Default when the node pins no budget, or the pinned budget names no turn
# bound. A pinned `maximum_assistant_turns` replaces this at launch. A
# tool-using answer spends one turn per tool result, so a call bounded at
# one turn could never finish an edit; sixteen is small enough that no
# unbounded subscription loop starts behind a node that never chose a budget.
_WORKSPACE_TOOLS_MAXIMUM_TURNS = "16"

# What the CLI says when it could not read an argument, measured on 2.1.233
# against a flag no release can know. It is the one thing that tells a vector
# this executable parsed whole from one it stopped inside -- and the attestation
# below does not take it on trust from that one measurement: it re-establishes
# the refusal on whatever executable a deployment actually serves.
_ARGUMENT_REFUSAL_MARKER = "unknown option"
_UNKNOWN_FLAG_CONTROL = "--atelier2-no-claude-code-knows-this"
# What the CLI says when every flag of the vector exists and parses, and the
# combination of them is still refused: measured on 2.1.221 (#694),
# `--output-format stream-json` without `--verbose` exits 1 saying it requires
# that flag. The unknown-option control cannot see this -- nothing here is an
# unknown option -- so a vector that lost `--verbose` would pass the attestation
# below and then fail on every real run. It is refused by its own name.
_OUTPUT_FORMAT_COMBINATION_MARKER = "requires --verbose"
# The probe never reaches a model, so this names none: it keeps the vector's
# shape exact while saying plainly that no billed call stands behind it.
_INVOCATION_PROBE_MODEL = "atelier2-invocation-probe"
# The measured answers are one short line each. The probe reads an external
# program's streams, so it holds them to the smallest bound those answers fit
# inside rather than to whatever the program decides to write.
_INVOCATION_PROBE_OUTPUT_BYTES = 16_384


def _workspace_tool_arguments(
    executable: Path,
    model: str,
    maximum_assistant_turns: int | None = None,
) -> tuple[str, ...]:
    """The exact argument vector one workspace-tool invocation is launched with."""

    return (
        str(executable),
        _PRINT_FLAG,
        _OUTPUT_FORMAT_FLAG,
        _STREAM_JSON_OUTPUT_FORMAT,
        _VERBOSE_FLAG,
        _MODEL_FLAG,
        model,
        _TOOLS_FLAG,
        _WORKSPACE_TOOL_LIST,
        _ALLOWED_TOOLS_FLAG,
        _WORKSPACE_TOOL_LIST,
        _NO_SETTING_SOURCES,
        _SAFE_MODE_FLAG,
        _STRICT_MCP_CONFIG_FLAG,
        _MCP_CONFIG_FLAG,
        _EMPTY_MCP_CONFIG,
        _DISABLE_SLASH_COMMANDS_FLAG,
        _NO_CHROME_FLAG,
        _NO_SESSION_PERSISTENCE_FLAG,
        _MAXIMUM_TURNS_FLAG,
        (
            str(maximum_assistant_turns)
            if maximum_assistant_turns is not None
            else _WORKSPACE_TOOLS_MAXIMUM_TURNS
        ),
    )


def _jobless_invocation_answer(
    settings: ClaudeSubscriptionSettings,
    arguments: tuple[str, ...],
    timeout_seconds: float,
) -> str:
    """Start this exact invocation with no job, and read back how it refused.

    Both streams are read, because what has to be told apart here is only said
    on the diagnostic one. The call is handed the deployment's real environment,
    unlike the version probe: what is being attested is the invocation form this
    executor will really launch, and an environment of its own would attest a
    different one.
    """

    with tempfile.TemporaryDirectory(
        prefix="atelier2-claude-invocation-"
    ) as probe_root:
        try:
            process = subprocess.Popen(
                arguments,
                cwd=probe_root,
                env=dict(_credential_environment(settings)),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise ClaudeExecutableUnsupported(
                f"the Claude executable could not start this executor's "
                f"invocation: {error}"
            ) from error
        try:
            return_code, answer, diagnostics = bounded_process_streams(
                process, timeout_seconds, _INVOCATION_PROBE_OUTPUT_BYTES
            )
        except OSError as error:
            raise ClaudeExecutableUnsupported(
                f"the Claude executable could not start this executor's "
                f"invocation: {error}"
            ) from error
    if return_code == 0:
        raise ClaudeExecutableUnsupported(
            "the Claude executable answered a jobless invocation successfully: "
            "this probe rests on a call with no job ending in a refusal, so a "
            "release that runs one instead has to be measured again before this "
            "executor may be composed against it"
        )
    return (answer + diagnostics).decode("utf-8", "replace")


def _attest_invocation_parses(
    settings: ClaudeSubscriptionSettings,
    arguments: tuple[str, ...],
    served_subject: str,
    timeout_seconds: float,
) -> None:
    """The shared half of every invocation attestation in this module.

    A version answer is not startability. A copy of this CLI has passed
    `--version` and then failed to spawn its real invocation at all, which is
    the observation this attestation exists for (#301, operator ruling of the
    night of 18.08.). So it launches the argument vector an executor really
    prepares -- every flag, the deployment's own environment -- and hands it no
    job: a CLI that read the whole vector reaches its own missing-input refusal,
    and a CLI that did not stops at the argument it could not read. Neither
    reaches a model, so the attestation is free and runs at every composition
    rather than at the first run that binds a node.

    The negative observation only means something if this executable can still
    make the positive one, so the control runs beside it: the same vector with
    one flag no release can know must be refused as an unknown option. Without
    that control, "said nothing about an unknown option" would also be what a
    release that stopped saying it looks like.
    """

    started = _jobless_invocation_answer(settings, arguments, timeout_seconds)
    if _ARGUMENT_REFUSAL_MARKER in started:
        raise ClaudeExecutableUnsupported(
            "the Claude executable refused an argument of this executor's "
            f"invocation: {started.strip()}. Serving {served_subject} "
            "needs every flag of that vector to exist and parse, because each "
            "one is a containment decision this executor states"
        )
    if _OUTPUT_FORMAT_COMBINATION_MARKER in started:
        raise ClaudeExecutableUnsupported(
            "the Claude executable refused this executor's output format for "
            f"want of a flag the vector does not carry: {started.strip()}. "
            f"Serving {served_subject} needs the whole vector to be accepted "
            "together, not only flag by flag -- every real call would otherwise "
            "die on a combination this probe had already called startable"
        )
    control = _jobless_invocation_answer(
        settings, (*arguments, _UNKNOWN_FLAG_CONTROL), timeout_seconds
    )
    if _ARGUMENT_REFUSAL_MARKER not in control:
        raise ClaudeExecutableUnsupported(
            "the Claude executable did not refuse a flag no release can know, "
            f"answering instead: {control.strip()}. The probe above reads a "
            "missing flag out of exactly that refusal, so an executable that "
            "never states one cannot be attested by it"
        )


def attest_workspace_tool_invocation(
    settings: ClaudeSubscriptionSettings,
    timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
) -> None:
    """Refuse an executable that cannot start the workspace-tool invocation."""

    _attest_invocation_parses(
        settings,
        _workspace_tool_arguments(settings.executable, _INVOCATION_PROBE_MODEL),
        "workspace-tool agents",
        timeout_seconds,
    )


@dataclass(frozen=True)
class ClaudeWorkspaceToolExecutor:
    """One headless `claude --print` call that may use tools where it stands.

    This is the tool-free executor's sibling and deliberately not its successor.
    The two differ in one decision, and a node keeps the tool-free one unless its
    durable binding asks for `HEADLESS_WITH_TOOLS`; the capability the factory
    below declares is what makes that ask the only way to reach this class.

    WHAT IT GRANTS. The invocation names the built-in tools it offers and
    pre-approves exactly the same set, so the process may read, search, write
    and run commands. Both halves are measured, not chosen: with the subprocess
    environment scrub this deployment sets, the CLI forces the permission mode
    back to its default and tells the caller to declare `allowedTools`
    explicitly, so an explicit allowlist is the only grant it honours here.

    WHAT IT KEEPS. Every other flag and the whole environment are the tool-free
    executor's, unchanged: no user or project settings, safe mode, an explicitly
    empty strict MCP configuration, no skills, no Chrome integration, no session
    persistence, no prompt history and no retries. Safe mode's own help states
    that built-in tools and permissions keep working while every customization
    stays disabled, which is what makes the pair coherent -- what this executor
    adds is the built-in tools it named, and nothing the workspace or the
    operator's configuration could bring with them.

    WHAT IT DOES NOT CLAIM. No operating-system isolation. The process runs as
    the serving user, and `Bash` and `Write` reach every path that user reaches
    -- including the credential directory this invocation hands it. The
    attempt's workspace is where the process is *started*, not a boundary it is
    held inside: the lease says in its own words that it claims nothing about
    operating-system isolation, and this executor claims nothing beyond it. A
    tool this vector did not name cannot be used; where a named tool may reach
    is the CLI's own business and no promise of this module's. The boundary that
    would make those claims is a separate slice and has its own owners (#242,
    #312).

    WHAT IS NOT MEASURED, said here rather than discovered later. The tool-free
    executor rests on a one-call conformance matrix against a real subscription
    answer. This one has no such call yet on any release: what is measured is
    that the vector starts and parses whole, and that the CLI's own hardening
    stops overriding the permission decision once the allowlist is declared.
    That a real answer then uses exactly these tools, that no customization
    returns with them, and what the envelope reports while they run, are the
    half a billed call has to establish -- one call, under the operator's gate,
    owned by #301. Until it is run, this executor is composed only where an
    operator armed it by name.
    """

    settings: ClaudeSubscriptionSettings

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        binding = request.resolved_binding
        if binding.auth_profile.auth_mode is not AuthMode.SUBSCRIPTION:
            raise ClaudeSubscriptionAuthModeUnsupported(
                "the Claude workspace-tool executor serves subscription profiles only"
            )
        settings = self.settings
        return ClaudeProcessCommand(
            _workspace_tool_arguments(
                settings.executable,
                binding.configuration.model,
                request.maximum_assistant_turns,
            ),
            _credential_environment(settings),
            # The job travels through standard input so no prompt ever appears
            # in the process table of a host the operator shares.
            request.job_bytes,
            standard_output_frame_bytes=CLAUDE_SUBSCRIPTION_FRAME_BYTES,
            declared_output_schema_bytes=request.declared_output_schema_bytes,
        )

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        """The answer travels inside the process result; the invocation carries
        only the output schema it is narrowed against.

        What the process wrote beside the answer stays in the workspace the
        attempt leased. This executor reads none of it: making a file durable is
        the artifact store's decision and its own slice (#352).
        """

        return _decoded_claude_answer(invocation, completion)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        """Nothing to take back: `CLAUDE_CONFIG_DIR` names the operator's own
        credential directory, and this executor copies it nowhere."""

        del command

    def close(self) -> None:
        return


@dataclass(frozen=True)
class ClaudeWorkspaceToolExecutorFactory:
    """The host-composed factory for one Claude workspace-tool executor."""

    settings: ClaudeSubscriptionSettings

    @property
    def key(self) -> AgentExecutorKey:
        return CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return CLAUDE_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY

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

    def open(self) -> ClaudeWorkspaceToolExecutor:
        return ClaudeWorkspaceToolExecutor(self.settings)


CLAUDE_ATELIER_DOORS_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("anthropic"),
    AgentExecutorRevision("claude-atelier-doors/v1"),
)
# A third operation of the same CLI, beside the tool-free and workspace-tool
# ones. Its distinguishing decision is the tool surface it opens -- the
# product's own MCP doors instead of built-ins -- and that decision is what an
# operational identity stands for, so every durable attempt record keeps saying
# which of the three ran. V2 for the same reason its two siblings are: the
# vector asks for the transcript-bearing stream instead of a single envelope
# (#666), which is a different operation rather than a later revision.
CLAUDE_ATELIER_DOORS_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-atelier-doors-print-stream-json/v2"
)

# Claude Code's allowlistable name for one MCP tool: `mcp__<server>__<tool>`.
# The grammar is this provider's and lives here; the server and tool names it
# is applied to arrive from their own typed owner through the composition root,
# never re-spelled in this module.
_MCP_TOOL_NAME_TEMPLATE = "mcp__{server}__{tool}"
# An atelier-doors episode is a small, fixed shape: read the brief, list the
# catalog (one door call plus reading its result), start a run (one), observe
# it (one), then a closing answer. Six tool-result turns cover that with
# headroom while staying small enough that no unbounded subscription loop hides
# behind it -- and small because each further turn could start one more billed
# child run: the door dedups only an exact repeated run identity, never
# distinct starts, so this bound is the per-episode ceiling on started
# children. A pinned node budget replaces it at launch, exactly as it does for
# the workspace-tool executor.
_ATELIER_DOORS_MAXIMUM_TURNS = "6"


@dataclass(frozen=True)
class ClaudeAtelierDoorsSettings:
    """The deployment values one Claude atelier-doors executor may use.

    `deployment` is the same validated Claude deployment the other two
    executors of this module use -- executable, credential directory, and the
    bubblewrap-bearing search path the subprocess scrub insists on. The three
    door fields arrive from the composition root, because each names a fact
    another layer owns: `door_server_name` and `door_tools` are the MCP door
    vocabulary (`atelier2.host.mcp_tools`), and `door_command` is the serving
    host's own way of launching its stdio MCP door against its own loopback
    address. This module owns only the Claude grammar those facts are spelled
    in. Whether the named service is loopback is the door child's own refusal
    (`atelier2.host.mcp_command`), the single owner of that trust rule.
    """

    deployment: ClaudeSubscriptionSettings
    door_server_name: str
    door_tools: tuple[str, ...]
    door_command: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.door_server_name.strip():
            raise ValueError("the atelier door server name must be nonempty")
        if not self.door_tools or any(not tool.strip() for tool in self.door_tools):
            raise ValueError(
                "the atelier door tools must be a nonempty list of nonempty names"
            )
        if not self.door_command or any(not part for part in self.door_command):
            raise ValueError(
                "the atelier door command must be a nonempty argument vector"
            )

    @property
    def allowed_door_tools(self) -> tuple[str, ...]:
        """The door tools under Claude Code's `mcp__<server>__<tool>` grammar."""

        return tuple(
            _MCP_TOOL_NAME_TEMPLATE.format(server=self.door_server_name, tool=tool)
            for tool in self.door_tools
        )

    def door_mcp_config(self) -> str:
        """The `--mcp-config` JSON naming exactly one server: the atelier door.

        Keyed by the door's own server name so the allowlisted
        `mcp__<server>__<tool>` names resolve to it. With `--strict-mcp-config`
        beside it, no user or project configuration can add a second server.
        """

        return json.dumps(
            {
                "mcpServers": {
                    self.door_server_name: {
                        "command": self.door_command[0],
                        "args": list(self.door_command[1:]),
                    }
                }
            },
            separators=(",", ":"),
        )


def _atelier_doors_arguments(
    settings: ClaudeAtelierDoorsSettings,
    model: str,
    maximum_assistant_turns: int | None = None,
) -> tuple[str, ...]:
    """The exact argument vector one atelier-doors invocation is launched with.

    `--safe-mode` is deliberately absent, by measurement rather than choice: on
    this repository's probe (24.08., spawn-detection against a fake MCP server,
    no model reached) safe mode prevented the `--mcp-config` server from being
    spawned at all, so a safe-mode call could never reach a door. What safe mode
    was carrying for the sibling executors -- no plugins, no skills, no hooks,
    no `CLAUDE.md` discovery -- must here be held by the surviving flags
    (`--setting-sources=`, `--disable-slash-commands`, strict single-server MCP
    config), and that they hold it alone is exactly what the billed conformance
    probe below has to establish before routine use.
    """

    return (
        str(settings.deployment.executable),
        _PRINT_FLAG,
        _OUTPUT_FORMAT_FLAG,
        _STREAM_JSON_OUTPUT_FORMAT,
        _VERBOSE_FLAG,
        _MODEL_FLAG,
        model,
        _NO_TOOLS,
        _ALLOWED_TOOLS_FLAG,
        ",".join(settings.allowed_door_tools),
        _NO_SETTING_SOURCES,
        _STRICT_MCP_CONFIG_FLAG,
        _MCP_CONFIG_FLAG,
        settings.door_mcp_config(),
        _DISABLE_SLASH_COMMANDS_FLAG,
        _NO_CHROME_FLAG,
        _NO_SESSION_PERSISTENCE_FLAG,
        _MAXIMUM_TURNS_FLAG,
        (
            str(maximum_assistant_turns)
            if maximum_assistant_turns is not None
            else _ATELIER_DOORS_MAXIMUM_TURNS
        ),
    )


def attest_atelier_doors_invocation(
    settings: ClaudeAtelierDoorsSettings,
    timeout_seconds: float = _PROBE_TIMEOUT_SECONDS,
) -> None:
    """Refuse an executable that cannot start the atelier-doors invocation."""

    _attest_invocation_parses(
        settings.deployment,
        _atelier_doors_arguments(settings, _INVOCATION_PROBE_MODEL),
        "atelier-doors agents",
        timeout_seconds,
    )


@dataclass(frozen=True)
class ClaudeAtelierDoorsExecutor:
    """One headless `claude --print` call that may operate the atelier's doors.

    The third sibling of this module, and the first executor in this repository
    whose tools reach the product's own API instead of the workspace: the
    invocation loads exactly one MCP server -- the serving host's own
    `atelier2 mcp` stdio door against its loopback address -- and pre-approves
    exactly the door tools the composition root named. The conductor (#7) is
    its first consumer: an agent that chooses, starts and observes catalog
    workflows through the same public doors the browser uses.

    WHAT IT GRANTS. No built-in tool: `--tools=` removes every one, measured on
    the tool-free executor to leave a shell request as printed imitation text.
    What remains reachable is the named MCP door server and, without asking,
    only the `--allowedTools` list -- the doors the composition root granted.
    The door process may see more tools than the allowlist pre-approves
    (narrowing is the allowlist's, not a narrower server's); a tool outside the
    list is not pre-approved and a headless call cannot interactively approve
    anything.

    WHAT IT KEEPS, AND THE ONE FLAG IT DROPS. `--setting-sources=`, a strict
    single-server MCP configuration, no slash commands, no Chrome, no session
    persistence, no prompt history, no retries -- all the sibling executors'
    containment, unchanged. `--safe-mode` is dropped by measurement: safe mode
    prevents any `--mcp-config` server from spawning, so it and a door cannot
    coexist (see `_atelier_doors_arguments`).

    WHAT IT SPAWNS, said plainly because the siblings say the opposite. This
    invocation EXPECTS a descendant: Claude Code launches the door command as a
    stdio subprocess. That child speaks only the loopback HTTP API and refuses
    any other address itself, and it needs no credential because the loopback
    API has none to offer. What keeps the operator's Anthropic credential out
    of the child is that the invocation's environment never carries a secret
    value in the first place -- `CLAUDE_CONFIG_DIR` names a directory and the
    credential stays a file on disk. `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`
    travels beside that as defense in depth: it was measured on the tool-free
    sibling, not on an MCP stdio child.

    WHAT THE SCRUB ITSELF LEAVES BEHIND, measured unbilled and named here
    rather than left silent (#656, #661). Before it can launch the door child
    under bubblewrap, this CLI's own `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`
    preparation creates roughly nineteen empty bind-mount targets in the
    invocation's own working directory wherever it does not already find one
    there -- dotfiles such as `.env`, package-manager markers such as
    `package.json`, `yarn.lock` and `.npmrc`, and the directories
    `.claude/commands`, `.claude/agents` and `node_modules/.bin`. None of them
    are read by anything this module decodes, and none are a write the model
    or a pinned project made. They are not tracked or swept by name: the
    working directory this executor is started in is one leased unit
    (`AgentAttemptWorkspaceLease`), and `LocalAgentAttemptWorkspaceOwner.release`
    retires that whole directory at the attempt's terminal cleanup regardless
    of what the CLI, the model or a pinned project left inside it -- so this
    residue falls with everything else in the lease rather than needing a name
    list this module would have to keep in step with a CLI release that stays
    free to widen it.

    WHAT IS NOT MEASURED, said here rather than discovered later. No billed
    call has been made under this vector on any release. What is measured is
    that the vector parses whole (the attestation above), and -- unbilled, by
    spawn-detection -- that the door server is reached without safe mode and
    not with it. That a real answer fires a door, that a built-in stays
    imitation-only beside a live MCP server, that a non-listed door and a
    second server stay unreachable, that no customization returns through
    safe mode's absence, that the subprocess-env scrub reaches the MCP server
    child's observed environment, and that the `--max-turns` bound holds
    across MCP door-call turns, are the half a billed call has to establish --
    one call, under the operator's gate, before routine use. Until it is run,
    this executor is composed only where an operator armed it by name.

    STRUCTURAL DEPENDENCY, named. The door is loopback-only, so this executor
    works only while attempts execute on the serving host itself. A runner
    cutover that moves attempts into containers breaks the door's
    reachability unless an authenticated machine credential (ADR 0009) lands
    first.
    """

    settings: ClaudeAtelierDoorsSettings

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        binding = request.resolved_binding
        if binding.auth_profile.auth_mode is not AuthMode.SUBSCRIPTION:
            raise ClaudeSubscriptionAuthModeUnsupported(
                "the Claude atelier-doors executor serves subscription profiles only"
            )
        settings = self.settings
        return ClaudeProcessCommand(
            _atelier_doors_arguments(
                settings,
                binding.configuration.model,
                request.maximum_assistant_turns,
            ),
            _credential_environment(settings.deployment),
            # The job travels through standard input so no prompt ever appears
            # in the process table of a host the operator shares.
            request.job_bytes,
            standard_output_frame_bytes=CLAUDE_SUBSCRIPTION_FRAME_BYTES,
            declared_output_schema_bytes=request.declared_output_schema_bytes,
        )

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        """The answer travels inside the process result; the invocation carries
        only the output schema it is narrowed against."""

        return _decoded_claude_answer(invocation, completion)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        """Nothing to take back: `CLAUDE_CONFIG_DIR` names the operator's own
        credential directory, and this executor copies it nowhere."""

        del command

    def close(self) -> None:
        return


@dataclass(frozen=True)
class ClaudeAtelierDoorsExecutorFactory:
    """The host-composed factory for one Claude atelier-doors executor."""

    settings: ClaudeAtelierDoorsSettings

    @property
    def key(self) -> AgentExecutorKey:
        return CLAUDE_ATELIER_DOORS_EXECUTOR_KEY

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return CLAUDE_ATELIER_DOORS_OPERATIONAL_IDENTITY

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        """Only headless-with-tools, exactly like the workspace sibling.

        The capability says a node asked for a tool-bearing call; WHICH tools
        is this executor's declared contract, selected by the binding's
        executor revision and never by the capability (see the capability's own
        docstring). Plain `HEADLESS` is missing so a text-only node can never
        be answered with an invocation that reaches the product's doors, and
        interactive is missing because there is no terminal here.
        """

        return frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS})

    def open(self) -> ClaudeAtelierDoorsExecutor:
        return ClaudeAtelierDoorsExecutor(self.settings)
