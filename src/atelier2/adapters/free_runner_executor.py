from __future__ import annotations

import json
import signal
import sys
import time
from dataclasses import dataclass

from atelier2.contracts.agent_attempts import (
    MAXIMUM_RUNNER_STANDARD_ERROR_BYTES,
    AgentAttemptFailureCode,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AuthMode,
    AuthProfileRevision,
    AuthReference,
    ProviderId,
)
from atelier2.contracts.runner_sessions import MAXIMUM_RUNNER_A_TEXT_BYTES
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentExecutorKey,
    AgentExecutorV2,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)


class FreeRunnerJobRefused(ValueError):
    """The fixed candidate program refuses any job document but its two."""


@dataclass(frozen=True)
class FreeRunnerPrintJob:
    """Print `text` and exit zero -- the candidate's completed-attempt leg."""

    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("free runner print job text must be nonempty")


@dataclass(frozen=True)
class FreeRunnerHoldJob:
    """Sleep `hold_seconds` while ignoring SIGTERM -- the candidate's cancel leg.

    Ignoring SIGTERM is deliberate, not incidental: it is what makes
    `reap_cancelled_runner_child` reach `REAPED_AFTER_KILL`, the exact
    physical observation the ADR's cancel proof asserts on (`#301-B1` review
    note). A hold job that answered SIGTERM would silently make that proof
    untestable rather than fail it loudly.
    """

    hold_seconds: float

    def __post_init__(self) -> None:
        if type(self.hold_seconds) not in (int, float) or self.hold_seconds <= 0:
            raise ValueError("free runner hold job seconds must be a positive number")


FreeRunnerJobDocument = FreeRunnerPrintJob | FreeRunnerHoldJob

_JOB_KIND_FIELD = "kind"
_PRINT_KIND = "print"
_HOLD_KIND = "hold"
_PRINT_TEXT_FIELD = "text"
_HOLD_SECONDS_FIELD = "hold_seconds"


def encode_free_runner_job(document: FreeRunnerJobDocument) -> bytes:
    """The one wire form for `job_bytes`: never argv, never interpreted program
    text (ADR 0009 S1) -- both the runner-side executor that builds this and
    the fixed candidate program that reads it agree on nothing else."""
    if isinstance(document, FreeRunnerPrintJob):
        body: dict[str, object] = {
            _JOB_KIND_FIELD: _PRINT_KIND,
            _PRINT_TEXT_FIELD: document.text,
        }
    elif isinstance(document, FreeRunnerHoldJob):
        body = {
            _JOB_KIND_FIELD: _HOLD_KIND,
            _HOLD_SECONDS_FIELD: document.hold_seconds,
        }
    else:
        raise TypeError("free runner job document must be Print or Hold")
    return json.dumps(body, sort_keys=True).encode("utf-8")


def decode_free_runner_job(data: bytes) -> FreeRunnerJobDocument:
    """The candidate program's own decode: refuse anything but its two documents."""
    try:
        body = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FreeRunnerJobRefused("free-runner-job-refused") from error
    if not isinstance(body, dict):
        raise FreeRunnerJobRefused("free-runner-job-refused")
    kind = body.get(_JOB_KIND_FIELD)
    try:
        if kind == _PRINT_KIND:
            text = body.get(_PRINT_TEXT_FIELD)
            if not isinstance(text, str):
                raise FreeRunnerJobRefused("free-runner-job-refused")
            return FreeRunnerPrintJob(text)
        if kind == _HOLD_KIND:
            hold_seconds = body.get(_HOLD_SECONDS_FIELD)
            if not isinstance(hold_seconds, (int, float)) or isinstance(
                hold_seconds, bool
            ):
                raise FreeRunnerJobRefused("free-runner-job-refused")
            return FreeRunnerHoldJob(hold_seconds)
    except ValueError as error:
        raise FreeRunnerJobRefused("free-runner-job-refused") from error
    raise FreeRunnerJobRefused("free-runner-job-refused")


def run_free_runner_job() -> int:
    """The fixed candidate program's whole body: read stdin, decode, act, refuse.

    `_FREE_RUNNER_PROGRAM_SOURCE` below runs exactly this function and carries
    no job bytes of its own; every job document arrives only over the stdin
    this reads, never as an argument or as code the interpreter was handed.
    """
    try:
        document = decode_free_runner_job(sys.stdin.buffer.read())
    except FreeRunnerJobRefused as error:
        print(str(error), file=sys.stderr)
        return 1
    if isinstance(document, FreeRunnerPrintJob):
        print(document.text)
        return 0
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(document.hold_seconds)
    return 0


_FREE_RUNNER_PROGRAM_SOURCE = (
    "import sys\n"
    "from atelier2.adapters.free_runner_executor import run_free_runner_job\n"
    "raise SystemExit(run_free_runner_job())\n"
)

_UNUSABLE_FREE_RUNNER_ANSWER = AgentExecutionFailure(
    AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
)

# `prepare_process` below deliberately declares its stdout frame bound as
# `MAXIMUM_AGENT_OUTPUT_BYTES_V2` rather than importing this bound directly,
# so the runner session can reuse that one number as the shared read bound
# for both this candidate's stdout and stderr (see the comment on
# `standard_output_frame_bytes` there). If either bound's owner ever moves
# it, this assertion breaks the import loudly instead of silently widening
# or narrowing what stderr may carry.
assert MAXIMUM_AGENT_OUTPUT_BYTES_V2 == MAXIMUM_RUNNER_STANDARD_ERROR_BYTES, (
    "the fake-free candidate's declared output bound and the runner's "
    "standard-error bound have drifted apart"
)


class FreeRunnerCandidateExecutor:
    """The one real runner-side executor for the fake-free candidate program.

    `_CoreRefusingFreeRunnerExecutor` below is the Serve-side fence: nothing
    reachable from Core may ever run a live fake-free process. This is the
    other half -- the one thing the isolated Runner container is actually
    allowed to execute, reached only through
    `atelier2.runner.executors.select_runner_executor`, never through the
    Core-facing factory below.
    """

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        return AgentProcessCommand(
            (sys.executable, "-c", _FREE_RUNNER_PROGRAM_SOURCE),
            standard_input=request.job_bytes,
            # Deliberately the durable result bound, not a wider provider
            # frame: this candidate never needs more, and using the same
            # number lets the runner session reuse one bounded read for both
            # this stream and the standard-error bound it enforces alongside
            # it (`MAXIMUM_RUNNER_STANDARD_ERROR_BYTES`, which is this same
            # 49_152 by construction).
            standard_output_frame_bytes=MAXIMUM_AGENT_OUTPUT_BYTES_V2,
        )

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        """The answer travels inside the process result; the workspace lease
        `invocation` carries is not consulted -- this candidate program never
        reads or writes it."""
        del invocation
        if completion.return_code != 0:
            return _UNUSABLE_FREE_RUNNER_ANSWER
        return AgentExecutionResult(completion.standard_output.strip())

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        """Nothing to take back: this executor hands the child no credential
        channel at all."""
        del command

    def close(self) -> None:
        return


class FreeRunnerExecutorFactory:
    """The Core catalogue entry for work that only the isolated Runner executes."""

    @property
    def key(self) -> AgentExecutorKey:
        return AgentExecutorKey(
            ProviderId("fake-free"), AgentExecutorRevision("fake-free/v1")
        )

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return AgentExecutorOperationalIdentity("free-runner-candidate")

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        return frozenset((AgentExecutionCapability.HEADLESS,))

    def open(self) -> AgentExecutorV2:
        return _CoreRefusingFreeRunnerExecutor()


class _CoreRefusingFreeRunnerExecutor:
    """Protect the candidate fence if an in-Core process path reaches this key."""

    def prepare_process(self, request: AgentExecutionRequestV2) -> AgentProcessCommand:
        del request
        raise RuntimeError("fake-free execution belongs to the Runner candidate")

    def decode_process_completion(
        self, invocation: AgentProcessInvocation, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        del invocation, completion
        raise RuntimeError("fake-free execution belongs to the Runner candidate")

    def release_credential_channel(self, command: AgentProcessCommand) -> None:
        del command

    def close(self) -> None:
        return


def refuse_unbound_runner_a_request(request: AgentExecutionRequestV2) -> None:
    """Refuse A-unbound or non-candidate request fields before generation bind."""
    if request.declared_output_schema_bytes is not None:
        raise ValueError("runner-a-output-schema-unbound")
    if request.maximum_assistant_turns is not None:
        raise ValueError("runner-a-turn-limit-unbound")
    if not 1 <= request.round_ordinal <= 2**64 - 1:
        raise ValueError("runner-a-round-out-of-range")
    factory = FreeRunnerExecutorFactory()
    configuration = request.resolved_binding.configuration
    auth = request.resolved_binding.auth_profile
    texts = (
        request.run_id.value,
        request.node_id,
        request.resolved_binding.role.value,
        configuration.model,
        configuration.executor_revision.value,
        configuration.requested_capability.value,
        auth.profile_id,
        auth.provider_id.value,
        auth.auth_mode.value,
        request.executor_operational_identity.value,
    )
    for text_value in texts:
        encoded = text_value.encode("utf-8")
        if (
            encoded.decode("utf-8") != text_value
            or not 1 <= len(encoded) <= MAXIMUM_RUNNER_A_TEXT_BYTES
        ):
            raise ValueError("runner-a-text-oversized")
    if (
        configuration.executor_revision != factory.key.executor_revision
        or request.executor_operational_identity != factory.operational_identity
        or auth.provider_id != factory.key.provider_id
        or auth.auth_mode is not AuthMode.API_KEY
        or configuration.requested_capability not in factory.declared_capabilities
    ):
        raise ValueError("runner-a-executor-unavailable")


@dataclass(frozen=True)
class FreeRunnerAuthorization:
    """The fake-free executor receives no credential material."""


def free_runner_auth_reference(profile: AuthProfileRevision) -> AuthReference:
    """The one deterministic, secret-free reference for a fake-free profile.

    Both Core's encode side (the disposable `#301` witness process today; a
    real Core composition later) and the Runner's resolve side
    (`runner.session`) call this module directly, so the reference either
    end computes for the same `AuthProfileRevision` can never drift into two
    owners.
    """
    return AuthReference(
        f"urn:atelier2:fake-free-auth:v1:{profile.revision_hash.value}"
    )


def resolve_free_runner_authorization(
    profile: AuthProfileRevision, reference: AuthReference
) -> FreeRunnerAuthorization:
    """Confirm `reference` is exactly this profile's own derived reference."""
    if (
        profile.provider_id.value != "fake-free"
        or profile.auth_mode is not AuthMode.API_KEY
        or reference != free_runner_auth_reference(profile)
    ):
        raise ValueError("auth-profile-unresolvable")
    return FreeRunnerAuthorization()
