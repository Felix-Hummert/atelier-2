from __future__ import annotations

import json
import os
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
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
    AgentProcessCompletion,
    AgentProcessInvocation,
)

CLAUDE_SUBSCRIPTION_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("anthropic"), AgentExecutorRevision("claude-subscription/v1")
)
CLAUDE_SUBSCRIPTION_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-print-json/v1"
)

# The largest raw frame one `claude -p --output-format json` call may write on
# standard output. The port owns the field; this number is this provider's,
# because it is this provider's wire format that produces the frame. It is
# deliberately larger than the durable output bound: the frame carries the
# durable result *inside* a JSON envelope, so a raw bound equal to
# MAXIMUM_AGENT_OUTPUT_BYTES_V2 would refuse answers the durable contract
# accepts. Derivation, so no durably acceptable result is ever refused as a
# frame:
#   * JSON string escaping expands one source byte to at most six frame bytes
#     (a C0 control byte becomes a six-character backslash-u escape), so a
#     durable-legal result occupies at most 6 * 49,152 = 294,912 frame bytes.
#   * The remaining 98,304 bytes are the envelope metadata allowance. One real
#     `claude -p --output-format json` answer (claude 2.1.221) was measured
#     carrying 1,328 bytes of metadata around its result, so the allowance is
#     about seventy times the observed envelope -- room for more models in
#     `modelUsage`, permission denials and future fields without ever bounding
#     an answer the durable contract would accept.
CLAUDE_SUBSCRIPTION_FRAME_BYTES = 8 * MAXIMUM_AGENT_OUTPUT_BYTES_V2

# Every Claude Code whose containment behaviour was measured end to end for
# this executor. A version bound would only prove that the flags were
# introduced, never that a later release still means by them what this executor
# claims, so conformance is an exact reviewed set rather than a floor.
CONFORMANT_CLAUDE_VERSIONS = frozenset({(2, 1, 221)})

_VERSION_FLAG = "--version"
_VERSION_PROBE_TIMEOUT_SECONDS = 30.0
# The measured answer is one short line. The probe reads an external program's
# streams, so it holds them to the smallest bound that answer fits inside
# rather than to whatever the program decides to write.
_VERSION_PROBE_OUTPUT_BYTES = 4_096

# Every directory this CLI treats as an administrator policy root and every
# entry it loads from one, read out of the 2.1.221 executable: the platform
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
# out of the 2.1.221 executable: a first-party OAuth session fetches managed
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
# Defence in depth for the child this invocation does not expect to have: set,
# the CLI strips Anthropic and cloud-provider credentials from every subprocess
# environment. Measured on 2.1.221 against the invocation below: with it the
# tool-free call answers exactly as without it, and on a search path where
# bubblewrap does not resolve the CLI refuses to start at all rather than
# running unscrubbed -- which is why `ClaudeSubscriptionSettings` proves
# bubblewrap is reachable before this deployment composes.
_SUBPROCESS_ENVIRONMENT_SCRUB_VARIABLE = "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB"
_SCRUB_SUBPROCESS_ENVIRONMENT = "1"
_BUBBLEWRAP_EXECUTABLE = "bwrap"

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


def _terminated_process_tree(process: subprocess.Popen[bytes]) -> None:
    """End the probe's whole session, then reap it and release its pipes.

    The probe runs in a session of its own, so one signal reaches whatever the
    external program started, not only the program itself. An already reaped
    probe is left alone: its identifier is free for the operating system to
    hand to somebody else, so signalling it could reach a stranger's session.
    """

    if process.returncode is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


def _bounded_answer(
    process: subprocess.Popen[bytes], deadline: float
) -> tuple[int, bytes]:
    """Read both streams of a probe under one byte bound and one deadline."""

    if process.stdout is None or process.stderr is None:
        raise ClaudeExecutableUnsupported("the version probe has no readable streams")
    answered = process.stdout.fileno()
    captured: dict[int, bytearray] = {}
    with selectors.DefaultSelector() as selector:
        for stream in (process.stdout, process.stderr):
            descriptor = stream.fileno()
            os.set_blocking(descriptor, False)
            captured[descriptor] = bytearray()
            selector.register(descriptor, selectors.EVENT_READ)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ClaudeExecutableUnsupported(
                    f"the Claude executable did not answer {_VERSION_FLAG} in time"
                )
            for ready, _ in selector.select(remaining):
                descriptor = ready.fd
                chunk = os.read(descriptor, _VERSION_PROBE_OUTPUT_BYTES)
                if not chunk:
                    selector.unregister(descriptor)
                    continue
                stream_bytes = captured[descriptor]
                stream_bytes += chunk
                if len(stream_bytes) > _VERSION_PROBE_OUTPUT_BYTES:
                    raise ClaudeExecutableUnsupported(
                        f"the Claude executable answered {_VERSION_FLAG} with more "
                        f"than {_VERSION_PROBE_OUTPUT_BYTES} bytes"
                    )
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ClaudeExecutableUnsupported(
            f"the Claude executable did not answer {_VERSION_FLAG} in time"
        )
    try:
        return_code = process.wait(timeout=remaining)
    except subprocess.TimeoutExpired as error:
        raise ClaudeExecutableUnsupported(
            f"the Claude executable did not answer {_VERSION_FLAG} in time"
        ) from error
    return return_code, bytes(captured[answered])


def read_claude_version(
    executable: Path, timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS
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
            return_code, answer = _bounded_answer(
                process, time.monotonic() + timeout_seconds
            )
        except OSError as error:
            raise ClaudeExecutableUnsupported(
                f"the Claude executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
        finally:
            _terminated_process_tree(process)
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
    executable: Path, timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS
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
            "against that exact release, and a later one can change a control they "
            "depend on. Admitting a new version needs the one-call conformance "
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
    also resolve bubblewrap: measured on 2.1.221, a CLI told to scrub without
    it refuses to start, so a deployment that could only fail at invocation
    time is refused here instead.
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
        if shutil.which(_BUBBLEWRAP_EXECUTABLE, path=self.search_path) is None:
            raise ValueError(
                "the Claude executable search path must resolve "
                f"{_BUBBLEWRAP_EXECUTABLE}, because this executor asks the CLI to "
                "scrub every subprocess environment and the CLI refuses to start "
                "without it"
            )


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

    `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1` is set as defence in depth. An earlier
    revision of this executor left it unset and claimed the variable was an
    opt-OUT that a tool-free call could not benefit from. That claim was wrong,
    and it is withdrawn: the "=0 to disable" diagnostics it rested on are what
    the CLI prints once the hardening is already active, while 2.1.221 reads
    the variable as the opt-IN that strips Anthropic and cloud-provider
    credentials from every subprocess environment. Measured against this exact
    invocation on 2.1.221: set, the tool-free call answers exactly as it does
    unset. It is not this executor's containment guarantee -- it is what limits
    a child this invocation does not expect to have.

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
                (
                    _SUBPROCESS_ENVIRONMENT_SCRUB_VARIABLE,
                    _SCRUB_SUBPROCESS_ENVIRONMENT,
                ),
            ),
            # The job travels through standard input so no prompt ever appears
            # in the process table of a host the operator shares.
            request.job_bytes,
            standard_output_frame_bytes=CLAUDE_SUBSCRIPTION_FRAME_BYTES,
        )

    def decode_process_completion(
        self, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        if completion.return_code != 0:
            return _UNUSABLE_PROVIDER_ANSWER
        if len(completion.standard_output) > CLAUDE_SUBSCRIPTION_FRAME_BYTES:
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
