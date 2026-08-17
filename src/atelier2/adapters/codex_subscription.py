"""One headless `codex exec` subscription executor.

Invocation semantics and the version set are measured against codex-cli
0.147.0. `codex exec` has no prompt-file flag, so the job travels on standard
input -- the CLI reads its instructions there when the prompt argument is `-`
-- and never on the argument vector, where any account on the host could read
it. The child environment is only `HOME`, `CODEX_HOME` and `PATH`, and `HOME`
is containment rather than convenience: without it the CLI resolves the
invoking account's own profile.

The agent's answer is taken from `--output-last-message`, the file the CLI
documents as carrying the last message. Standard output is framed but not
parsed: this executor was not permitted a billed `codex exec` call, so the
`--json` event stream is unmeasured, and decoding an unmeasured envelope would
be inventing a provider format.

Because that answer arrives beside the process rather than inside it, decoding
takes the invocation it prepared: the runtime opens one executor per registry
key and hands that same object to every attempt, so an executor correlating
through its own state would let overlapping attempts decode each other's
answers into durable results. This executor therefore holds none. The answer is
asked for under a bare name, so the CLI writes it into the directory the attempt
leased, and the workspace owner removes it with everything else the attempt
left behind. One directory regime, not two.

Flags alone do not bound what the CLI discovers or what it may do. `codex
doctor --json` reports the config layer, credential home and MCP servers a
composed profile resolves, so the host attests that exact profile with this
executor's own environment. `codex sandbox` runs one command under the CLI's
own Linux sandbox, so a sandboxed binding is refused unless that sandbox can
actually start here: measured on a host without the namespaces bubblewrap
needs, it exits nonzero with `bwrap: loopback: Failed RTM_NEWADDR`, and a
sandbox that cannot start is not a sandbox.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from enum import StrEnum
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

CODEX_SUBSCRIPTION_EXECUTOR_KEY = AgentExecutorKey(
    ProviderId("openai"), AgentExecutorRevision("codex-subscription/v1")
)
CODEX_SUBSCRIPTION_OPERATIONAL_IDENTITY = AgentExecutorOperationalIdentity(
    "headless-exec-last-message/v1"
)

# The same portable ceiling as the process port. `codex exec` writes progress
# to standard output while the durable answer goes to the last-message file, so
# this executor frames the stream it does not read rather than inventing a
# tighter allowance for an envelope it was not permitted to measure.
CODEX_SUBSCRIPTION_FRAME_BYTES = MAXIMUM_AGENT_PROCESS_STANDARD_OUTPUT_BYTES

CONFORMANT_CODEX_VERSIONS = frozenset({(0, 147, 0)})


class CodexSandboxMode(StrEnum):
    """The sandbox policies `codex exec -s` documents on codex-cli 0.147.0."""

    READ_ONLY = "read-only"
    WORKSPACE_WRITE = "workspace-write"
    DANGER_FULL_ACCESS = "danger-full-access"


_VERSION_FLAG = "--version"
_VERSION_PROBE_TIMEOUT_SECONDS = 30.0
_VERSION_PROBE_OUTPUT_BYTES = 4_096

_EXEC_COMMAND = "exec"
# Measured against codex-cli 0.147.0 `--help`: each of these names an ambient
# surface a single headless attempt must not reach. They are containment, not
# preference. `--ignore-user-config` keeps the operator's own `config.toml`,
# and the per-project trust it records, out of the child while auth still
# resolves through `CODEX_HOME`.
_IGNORE_USER_CONFIG_FLAG = "--ignore-user-config"
_IGNORE_RULES_FLAG = "--ignore-rules"
_SKIP_GIT_REPOSITORY_CHECK_FLAG = "--skip-git-repo-check"
_EPHEMERAL_FLAG = "--ephemeral"
_COLOR_FLAG = "--color"
_NEVER = "never"
_MODEL_FLAG = "--model"
_SANDBOX_FLAG = "--sandbox"
_LAST_MESSAGE_FLAG = "--output-last-message"
# `codex exec` reads its instructions from standard input when the prompt
# argument is `-`. A prompt given as an argument would additionally append
# piped input as a `<stdin>` block, so the job is passed one way only.
_PROMPT_FROM_STANDARD_INPUT = "-"

_DOCTOR_COMMAND = "doctor"
_DOCTOR_JSON_FLAG = "--json"
_DOCTOR_OUTPUT_BYTES = 1_048_576
_CHECKS_FIELD = "checks"
_CONFIGURATION_CHECK = "config.load"
_DETAILS_FIELD = "details"
_STATUS_FIELD = "status"
_OK = "ok"
_CREDENTIAL_HOME_DETAIL = "CODEX_HOME"
_USER_CONFIGURATION_DETAIL = "config.toml"
_MISSING = "missing"
_MCP_SERVER_DETAIL = "mcp servers"
_NO_MCP_SERVERS = "0"

_SANDBOX_COMMAND = "sandbox"
_SANDBOX_PROBE_ARGUMENTS = ("--", "/bin/true")

_PROBE_DIRECTORY_PREFIX = "atelier2-codex-probe-"
_ANSWER_FILE_NAME = "last-message"

_HOME_VARIABLE = "HOME"
_CREDENTIAL_DIRECTORY_VARIABLE = "CODEX_HOME"
_SEARCH_PATH_VARIABLE = "PATH"

_UNUSABLE_PROVIDER_ANSWER = AgentExecutionFailure(
    AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
)


class CodexSubscriptionAuthModeUnsupported(ValueError):
    """A published configuration bound a non-subscription profile to this executor."""


class CodexExecutableUnsupported(ValueError):
    """The named executable is not a Codex CLI this executor was measured against."""


class CodexContainmentUnattested(ValueError):
    """The composed profile discovers, or fails to contain, what it must not."""


def _parsed_version(reported: str) -> tuple[int, int, int] | None:
    """Read `codex-cli 0.147.0` or `0.147.0` as the version."""

    tokens = reported.strip().split()
    if not tokens:
        return None
    leading = tokens[1] if len(tokens) > 1 and not tokens[0][0].isdigit() else tokens[0]
    parts = leading.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def read_codex_version(
    executable: Path,
    search_path: str,
    timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS,
) -> tuple[int, int, int]:
    """Ask one executable which Codex it is. Runs it with `--version`.

    The probe carries the served child's own executable search path and
    nothing else. Measured on this host, the installed `codex` is a
    `#!/usr/bin/env node` shim: a probe with no `PATH` cannot start it at all,
    so an empty environment would report every real install as unsupported.
    """

    with tempfile.TemporaryDirectory(prefix="atelier2-codex-version-") as probe_root:
        try:
            process = subprocess.Popen(
                (str(executable), _VERSION_FLAG),
                cwd=probe_root,
                env={_SEARCH_PATH_VARIABLE: search_path},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise CodexExecutableUnsupported(
                f"the Codex executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
        try:
            return_code, answer = bounded_process_answer(
                process, timeout_seconds, _VERSION_PROBE_OUTPUT_BYTES
            )
        except OSError as error:
            raise CodexExecutableUnsupported(
                f"the Codex executable did not answer {_VERSION_FLAG}: {error}"
            ) from error
    if return_code != 0:
        raise CodexExecutableUnsupported(
            f"the Codex executable refused {_VERSION_FLAG} with exit code {return_code}"
        )
    version = _parsed_version(answer.decode("utf-8", "replace"))
    if version is None:
        raise CodexExecutableUnsupported(
            f"the Codex executable did not report a version at {_VERSION_FLAG}"
        )
    return version


def verify_codex_capability(
    executable: Path,
    search_path: str,
    timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS,
) -> tuple[int, int, int]:
    """Refuse an executable outside the reviewed conformance set."""

    version = read_codex_version(executable, search_path, timeout_seconds)
    if version not in CONFORMANT_CODEX_VERSIONS:
        reported = ".".join(str(part) for part in version)
        conformant = ", ".join(
            ".".join(str(part) for part in candidate)
            for candidate in sorted(CONFORMANT_CODEX_VERSIONS)
        )
        raise CodexExecutableUnsupported(
            f"serving Codex subscription agents requires Codex {conformant}, "
            f"not {reported}: this executor's invocation semantics were measured "
            "against that exact release"
        )
    return version


@dataclass(frozen=True)
class CodexSubscriptionSettings:
    executable: Path
    credential_directory: Path
    search_path: str
    sandbox: CodexSandboxMode

    def __post_init__(self) -> None:
        executable = self.executable.resolve()
        credential_directory = self.credential_directory.resolve()
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "credential_directory", credential_directory)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ValueError("the Codex executable must be an executable file")
        if not credential_directory.is_dir():
            raise ValueError(
                "the Codex credential directory must be an existing directory"
            )
        if not self.search_path.strip():
            raise ValueError("the Codex executable search path must be nonempty")
        if not isinstance(self.sandbox, CodexSandboxMode):
            raise TypeError("the Codex sandbox mode must be a measured policy")


def _child_environment(
    settings: CodexSubscriptionSettings,
) -> tuple[tuple[str, str], ...]:
    """The complete environment a launched Codex inherits, and nothing else."""

    return (
        (_HOME_VARIABLE, str(settings.credential_directory)),
        (_CREDENTIAL_DIRECTORY_VARIABLE, str(settings.credential_directory)),
        (_SEARCH_PATH_VARIABLE, settings.search_path),
    )


def _probe(
    settings: CodexSubscriptionSettings,
    arguments: tuple[str, ...],
    timeout_seconds: float,
    output_bytes: int,
    environment_overrides: dict[str, str],
) -> tuple[int, bytes]:
    """Run one non-billed CLI subcommand with the environment a job would get."""

    # A probe attests the profile a job would get -- its home, its config layer
    # and its MCP servers -- never a directory, so it runs in one of its own
    # rather than in an attempt's lease, which does not exist yet at composition.
    with tempfile.TemporaryDirectory(prefix=_PROBE_DIRECTORY_PREFIX) as probe_root:
        try:
            process = subprocess.Popen(
                (str(settings.executable), *arguments),
                cwd=probe_root,
                env=dict(_child_environment(settings)) | environment_overrides,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            raise CodexContainmentUnattested(
                f"the Codex executable did not answer {arguments[0]}: {error}"
            ) from error
        try:
            return bounded_process_answer(process, timeout_seconds, output_bytes)
        except OSError as error:
            raise CodexContainmentUnattested(
                f"the Codex executable did not answer {arguments[0]}: {error}"
            ) from error


def _uncontained_surfaces(
    settings: CodexSubscriptionSettings, reported: dict[str, object]
) -> tuple[str, ...]:
    """Name every way the reported profile is not the one this executor granted."""

    checks = reported.get(_CHECKS_FIELD)
    if not isinstance(checks, dict):
        return (f"{_CHECKS_FIELD} missing",)
    configuration = checks.get(_CONFIGURATION_CHECK)
    if not isinstance(configuration, dict):
        return (f"{_CONFIGURATION_CHECK} missing",)
    details = configuration.get(_DETAILS_FIELD)
    if not isinstance(details, dict):
        return (f"{_CONFIGURATION_CHECK}.{_DETAILS_FIELD} missing",)

    uncontained: list[str] = []
    if configuration.get(_STATUS_FIELD) != _OK:
        uncontained.append(f"{_CONFIGURATION_CHECK}={configuration.get(_STATUS_FIELD)}")
    if details.get(_CREDENTIAL_HOME_DETAIL) != str(settings.credential_directory):
        uncontained.append(
            f"{_CREDENTIAL_HOME_DETAIL}={details.get(_CREDENTIAL_HOME_DETAIL)}"
        )
    # A contained profile reports its user config as present-but-missing; a
    # bare path means the CLI resolved a config.toml it would load.
    user_configuration = details.get(_USER_CONFIGURATION_DETAIL)
    if not (
        isinstance(user_configuration, list)
        and len(user_configuration) == 2
        and user_configuration[1] == _MISSING
    ):
        uncontained.append(_USER_CONFIGURATION_DETAIL)
    mcp_servers = details.get(_MCP_SERVER_DETAIL)
    if mcp_servers not in (None, _NO_MCP_SERVERS):
        uncontained.append(f"{_MCP_SERVER_DETAIL}={mcp_servers}")
    return tuple(uncontained)


def attest_codex_containment(
    settings: CodexSubscriptionSettings,
    timeout_seconds: float = _VERSION_PROBE_TIMEOUT_SECONDS,
    environment_overrides: dict[str, str] | None = None,
) -> None:
    """Refuse to serve unless the composed profile is contained and enforceable.

    Neither probe requests a completion, so attesting costs no subscription.
    """

    overrides = environment_overrides or {}
    # The report is read rather than its exit code: measured on codex-cli
    # 0.147.0, `doctor` exits nonzero whenever any check fails, and the ones
    # that fail on a contained deployment are auth and network reachability --
    # neither of which is containment. Gating on the exit code would refuse to
    # serve a perfectly contained profile on an offline host, so this reads the
    # configuration check it actually depends on and lets a missing credential
    # fail the attempt loudly instead.
    _return_code, answer = _probe(
        settings,
        (_DOCTOR_COMMAND, _DOCTOR_JSON_FLAG),
        timeout_seconds,
        _DOCTOR_OUTPUT_BYTES,
        overrides,
    )
    try:
        reported: object = json.loads(answer)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CodexContainmentUnattested(
            f"the Codex executable did not report {_DOCTOR_COMMAND} as JSON"
        ) from error
    if not isinstance(reported, dict):
        raise CodexContainmentUnattested(
            f"the Codex executable did not report {_DOCTOR_COMMAND} as an object"
        )
    uncontained = _uncontained_surfaces(settings, reported)
    if uncontained:
        raise CodexContainmentUnattested(
            "serving Codex subscription agents requires a profile that resolves "
            "this executor's own credential home, loads no user config.toml and "
            f"configures no MCP server; this deployment reports "
            f"{', '.join(uncontained)}"
        )

    if settings.sandbox is CodexSandboxMode.DANGER_FULL_ACCESS:
        return
    sandbox_return_code, _sandbox_answer = _probe(
        settings,
        (_SANDBOX_COMMAND, *_SANDBOX_PROBE_ARGUMENTS),
        timeout_seconds,
        _DOCTOR_OUTPUT_BYTES,
        overrides,
    )
    if sandbox_return_code != 0:
        raise CodexContainmentUnattested(
            f"serving Codex agents under the {settings.sandbox.value} sandbox "
            f"requires a host where {_SANDBOX_COMMAND} can start; it exited with "
            f"code {sandbox_return_code} here, and a sandbox that cannot start "
            "does not contain anything"
        )


def _answer_file_of(invocation: AgentProcessInvocation) -> Path:
    """Name the answer file inside the directory this attempt leased.

    The command asks for the answer under a bare name, so the CLI writes it
    into the directory it is started in -- the attempt's own lease. There is no
    second private directory to create, hand over or remove: the lease is that
    directory, and the workspace owner removes it with everything in it.
    """

    return invocation.lease.working_directory / _ANSWER_FILE_NAME


@dataclass(frozen=True)
class CodexSubscriptionExecutor:
    settings: CodexSubscriptionSettings

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        binding = request.resolved_binding
        if binding.auth_profile.auth_mode is not AuthMode.SUBSCRIPTION:
            raise CodexSubscriptionAuthModeUnsupported(
                "the Codex subscription executor serves subscription profiles only"
            )
        settings = self.settings
        return AgentProcessCommand(
            (
                str(settings.executable),
                _EXEC_COMMAND,
                _IGNORE_USER_CONFIG_FLAG,
                _IGNORE_RULES_FLAG,
                _SKIP_GIT_REPOSITORY_CHECK_FLAG,
                _EPHEMERAL_FLAG,
                _COLOR_FLAG,
                _NEVER,
                _MODEL_FLAG,
                binding.configuration.model,
                _SANDBOX_FLAG,
                settings.sandbox.value,
                _LAST_MESSAGE_FLAG,
                _ANSWER_FILE_NAME,
                _PROMPT_FROM_STANDARD_INPUT,
            ),
            _child_environment(settings),
            request.job_bytes,
            standard_output_frame_bytes=CODEX_SUBSCRIPTION_FRAME_BYTES,
        )

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        """Read the answer this exact invocation's lease holds."""

        try:
            answer = _answer_file_of(invocation).read_bytes()
        except OSError:
            answer = None
        if completion.return_code != 0:
            return _UNUSABLE_PROVIDER_ANSWER
        if len(completion.standard_output) > CODEX_SUBSCRIPTION_FRAME_BYTES:
            return _UNUSABLE_PROVIDER_ANSWER
        if answer is None or len(answer) > MAXIMUM_AGENT_OUTPUT_BYTES_V2:
            return _UNUSABLE_PROVIDER_ANSWER
        return AgentExecutionResult(answer)

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        """Nothing to take back: this executor copies no credential anywhere.

        `CODEX_HOME` names the operator's own credential directory rather than a
        copy made for one invocation, and the answer lives in the attempt's
        leased directory, which the workspace owner takes away. Removing
        anything here would remove the lease itself.
        """

        del command

    def close(self) -> None:
        """The lease owns every byte this executor produced, so it owns nothing."""


@dataclass(frozen=True)
class CodexSubscriptionExecutorFactory:
    settings: CodexSubscriptionSettings

    @property
    def key(self) -> AgentExecutorKey:
        return CODEX_SUBSCRIPTION_EXECUTOR_KEY

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return CODEX_SUBSCRIPTION_OPERATIONAL_IDENTITY

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        return frozenset({AgentExecutionCapability.HEADLESS})

    def open(self) -> CodexSubscriptionExecutor:
        return CodexSubscriptionExecutor(self.settings)
