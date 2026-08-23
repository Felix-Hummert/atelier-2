"""The extracted candidate session paired with the real Core session, on the wire.

Both ends run real production code — real `encode_runner_session_frame` /
`decode_runner_session_frame`, the real `_CoreFrameFence`, and the real
`CoreRunnerSession` state machine — connected by one real `socket.socketpair()`
half each. Only the disposable candidate process and its journal volume are
faked: a plain subprocess stands in for the Docker container the launcher
would otherwise start, and `tmp_path` stands in for its mounted journal.

The candidate side runs `atelier2.runner.session.run_candidate_session`
unmodified in that subprocess, a fresh interpreter rather than a fork of this
one — pytest-xdist workers are multi-threaded, and `os.fork()` there risks the
child deadlocking on a lock some other thread held at fork time. The
subprocess is also where the one host-security precondition the READY
measurement requires, `PR_SET_NO_NEW_PRIVS`, gets set — never on the pytest
worker that runs every other test.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath

import pytest

from atelier2.adapters.claude_subscription import CLAUDE_SUBSCRIPTION_EXECUTOR_KEY
from atelier2.adapters.free_runner_executor import (
    FreeRunnerHoldJob,
    FreeRunnerPrintJob,
    encode_free_runner_job,
)
from atelier2.application.run_runner_session import (
    CoreRunnerSession,
    RunnerSessionRefusal,
    encode_runner_prepare_payload,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    AgentAttemptReplacement,
    CancelAgentAttemptRequest,
    RunId,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
    RunnerProviderFailure,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runner_manifests import (
    RunnerManifestV1,
    RunnerPathGrant,
    RunnerPathRight,
    candidate_runner_manifest,
    encode_runner_manifest,
    runner_manifest_id,
)
from atelier2.contracts.runner_session_codec import (
    decode_runner_session_frame,
    encode_runner_session_frame,
    runner_session_body_length,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame, RunnerSessionMessage
from atelier2.contracts.runner_terminal_evidence_codec import (
    decode_runner_terminal_evidence_record,
)
from atelier2.contracts.runs import WorkflowRevisionHash
from atelier2.runner.authorization import free_runner_auth_reference
from atelier2.runner.executors import runner_executor_cli_pin
from atelier2.runner.session import CandidateScenario, RunnerFrameChannel, _status_field

# A cgroup pids controller isn't delegated the same way (or readable the same
# way) on every host this test runs on: this sandbox, the GitHub-hosted CI
# runner, and the Docker witness each expose /sys/fs/cgroup differently, and
# `_pid_limit`'s own real fallback chain can legitimately find nothing
# numeric on some of them. The manifest and the live READY measurement must
# still agree, so both sides use this one fixed stand-in instead of the real
# per-host reading. Production's real `_pid_limit` is untouched and stays proven by the
# Docker witness (`scripts/runner_candidate.sh`), which runs on its own
# attested cgroup.
_STUBBED_PROCESS_LIMIT = 4096

# Runs `run_candidate_session` in a fresh interpreter with its own
# `PR_SET_NO_NEW_PRIVS` and a stubbed cgroup pid limit reading (see
# `_STUBBED_PROCESS_LIMIT` above). The child's Landlock surface arrives
# through the manifest `_host_manifest` attests, exactly as it does in the
# deployed image. Nothing about the session's own logic is touched.
_CANDIDATE_DRIVER = """
import ctypes
import socket
import sys
from pathlib import Path

_PR_SET_NO_NEW_PRIVS = 38

(
    fd, attempt_id, request_hash, generation_id, manifest_id, invocation_id,
    scenario_value, manifest_path, identity, journal_directory, process_limit,
    workspace_directory, landlock_abi,
) = sys.argv[1:14]

from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.runner import session as session_module
from atelier2.runner.session import CandidateScenario, run_candidate_session

libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS) failed")

# Declared witness data, not a stubbed subject: an empty value leaves the real
# kernel reading in place, and only a test that wants to drive the
# no-child-boundary refusal names one.
if landlock_abi:
    session_module.landlock_kernel_abi = lambda: int(landlock_abi)
session_module._pid_limit = lambda: int(process_limit)
session_module._runner_workspace_directory = lambda: Path(workspace_directory)
run_candidate_session(
    socket.socket(fileno=int(fd)),
    RunnerGenerationBinding(
        AgentAttemptId(attempt_id),
        AgentExecutionRequestHash(request_hash),
        RunnerGenerationId(generation_id),
        RunnerManifestId(manifest_id),
    ),
    RunnerInvocationId(invocation_id),
    CandidateScenario(scenario_value),
    Path(manifest_path),
    Path(identity),
    Path(journal_directory),
)
"""


class _FakeRunnerSessionCore:
    """The durable-store port `run_runner_session.CoreRunnerSession` delegates to.

    A fake here stands in for the Core-side SQLite attempt store: that
    persistence is exercised elsewhere (test_runner_session_application.py,
    the DBOS store tests). This wire test's subject is the frame sequencing
    between the two real session state machines, not durable-store recovery.
    """

    def __init__(self) -> None:
        self.armed = 0
        self.committed = 0
        self.acknowledged = 0
        self.cancelled = 0
        self.committed_envelope: RunnerTerminalEvidenceEnvelope | None = None

    def arm(
        self, binding: RunnerGenerationBinding, invocation: RunnerInvocationId
    ) -> None:
        del binding, invocation
        self.armed += 1

    def commit_terminal_record(
        self, binding: RunnerGenerationBinding, record: bytes
    ) -> RunnerTerminalEvidenceHash:
        del binding
        self.committed += 1
        decoded = decode_runner_terminal_evidence_record(record)
        # A resumed candidate whose journal already tombstoned this evidence
        # has no envelope left to resend -- see
        # `DbosRunnerSessionCore._require_already_acknowledged` for the real
        # adapter's durable-store confirmation this fake stands in for.
        if isinstance(decoded, RunnerTerminalEvidenceAckTombstone):
            return decoded.evidence_hash
        if not isinstance(decoded, RunnerTerminalEvidenceEnvelope):
            raise TypeError("runner-terminal-record-corrupt")
        self.committed_envelope = decoded
        return RunnerTerminalEvidenceHash.for_envelope(decoded)

    def acknowledge(
        self,
        binding: RunnerGenerationBinding,
        evidence_hash: RunnerTerminalEvidenceHash,
        tombstone: bytes,
    ) -> None:
        del binding, evidence_hash, tombstone
        self.acknowledged += 1

    def cancel(self) -> CancelAgentAttemptRequest:
        self.cancelled += 1
        return CancelAgentAttemptRequest(
            RunId("runner-session-wire/one"),
            AgentAttemptId("a" * 64),
            "runner-session-wire-cancel",
            2,
            AgentAttemptReplacement.NONE,
        )


def _binding(
    request_hash: AgentExecutionRequestHash, manifest_id: RunnerManifestId
) -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        AgentAttemptId("a" * 64),
        request_hash,
        RunnerGenerationId("A" * 43),
        manifest_id,
    )


_PRINT_JOB_BYTES = encode_free_runner_job(FreeRunnerPrintJob("runner candidate"))


def _free_request(job_bytes: bytes = _PRINT_JOB_BYTES) -> AgentExecutionRequestV2:
    auth = AuthProfileRevision(
        "candidate", 1, ProviderId("fake-free"), AuthMode.API_KEY
    )
    configuration = AgentConfigurationRevision(
        "free",
        auth.revision_hash,
        AgentExecutorRevision("fake-free/v1"),
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    run_id = RunId("runner-session-wire/one")
    workflow = WorkflowRevisionHash("a" * 64)
    node_id = "execute"
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, workflow, node_id, 1),
        run_id,
        workflow,
        node_id,
        ResolvedAgentBinding(AgentRole("runner"), configuration, auth),
        AgentExecutorOperationalIdentity("free-runner-candidate"),
        job_bytes,
    )


# The one writable surface a session driven on this host may name, and the
# read-only path standing in for the credential directory ADR 0009 sec. 2 binds
# read-only. The deployed image attests its own paths, which do not exist here;
# the rights are the same either way, which is what these tests are about.
_HOST_SCRATCH = PurePosixPath("/tmp")
_HOST_CREDENTIALS = PurePosixPath("/etc")


def _host_child_grants() -> tuple[RunnerPathGrant, ...]:
    """The child surface this test interpreter really needs, as one manifest fact.

    The fixed candidate program (`free_runner_executor.run_free_runner_job`)
    imports atelier2 only after Landlock restricts it, so this interpreter's
    install prefix and an editable install's real source tree (this dev tree's
    own `src/`, outside both prefixes) have to be named the same way `/usr`
    already covers a packaged install — production's own layout. Attesting it
    through the manifest is what the deployed image does too, so this drives
    the real path instead of replacing the session's own function.
    """
    import atelier2

    installed_source_root = Path(atelier2.__file__).resolve().parent.parent
    executable = {
        PurePosixPath(path)
        for path in (
            Path("/usr"),
            Path("/lib"),
            Path("/lib64"),
            Path(sys.prefix),
            Path(sys.base_prefix),
            installed_source_root,
        )
        if path.exists()
    } - {_HOST_SCRATCH, _HOST_CREDENTIALS}
    # `/proc` and `/dev` are data this child reads, and the credential stand-in
    # is data it must never run -- the same shape the deployed image attests.
    read_only = {
        PurePosixPath(path)
        for path in (Path("/proc"), Path("/dev"), Path(_HOST_CREDENTIALS))
        if path.exists()
    } - executable
    return tuple(
        sorted(
            (
                *(
                    RunnerPathGrant(path, RunnerPathRight.READ_AND_EXECUTE)
                    for path in executable
                ),
                *(
                    RunnerPathGrant(path, RunnerPathRight.READ_ONLY)
                    for path in read_only
                ),
                RunnerPathGrant(_HOST_SCRATCH, RunnerPathRight.READ_WRITE),
            ),
            key=lambda grant: grant.path.as_posix(),
        )
    )


def _host_manifest(**timings: int) -> RunnerManifestV1:
    """A candidate manifest declaring exactly what this host will measure.

    `run_candidate_session`'s READY step re-measures uid, gid, and
    capabilities live; a manifest built from anything else would make
    `require_ready_matches_manifest` refuse a healthy session. The process
    limit instead uses the fixed `_STUBBED_PROCESS_LIMIT` both sides agree
    on — see the comment above `_STUBBED_PROCESS_LIMIT`.
    """
    return replace(
        candidate_runner_manifest(
            source_commit="a" * 40,
            image_digest="sha256:" + "b" * 64,
            required_landlock_abi=1,
            executor_revision="fake-free/v1",
            executor_operational_identity="free-runner-candidate",
            provider_id="fake-free",
            auth_mode="api_key",
            requested_capability="headless",
            provider_credential_directory=_HOST_CREDENTIALS,
            child_path_grants=_host_child_grants(),
        ),
        effective_uid=os.getuid(),
        effective_gid=os.getgid(),
        effective_capabilities=_status_field("CapEff").lower(),
        process_limit=_STUBBED_PROCESS_LIMIT,
        **timings,
    )


def _denied_identity_directory(tmp_path: Path) -> Path:
    """A directory this process can read but not write — the READY attestation
    requires that the mounted identity volume refuses a write probe."""
    identity = tmp_path / "identity"
    identity.mkdir()
    identity.chmod(0o500)
    return identity


def _read_frame(channel: RunnerFrameChannel) -> RunnerSessionFrame:
    prefix = _read_exact(channel, 4)
    length = runner_session_body_length(prefix)
    return decode_runner_session_frame(prefix + _read_exact(channel, length))


def _read_exact(channel: RunnerFrameChannel, length: int) -> bytes:
    chunks: list[bytes] = []
    remaining = length
    while remaining:
        chunk = channel.recv(remaining)
        if not chunk:
            raise ConnectionError("candidate closed the wire test session")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_frame(channel: RunnerFrameChannel, frame: RunnerSessionFrame) -> None:
    channel.sendall(encode_runner_session_frame(frame))


def _drive_core_session(
    channel: RunnerFrameChannel,
    session: CoreRunnerSession,
    scenario: CandidateScenario,
) -> None:
    """Play the Core side exactly as the witness Core process does."""
    while True:
        frame = _read_frame(channel)
        response = (
            session.accept_terminal_record(frame)
            if frame.message is RunnerSessionMessage.TERMINAL_RECORD
            else session.accept(frame)
        )
        if response is not None:
            _write_frame(channel, response)
        if (
            scenario is CandidateScenario.CANCEL
            and frame.message is RunnerSessionMessage.STARTED
        ):
            _write_frame(channel, session.cancel())
        if frame.message is RunnerSessionMessage.RELEASED:
            return


def _drive_core_session_with_crossing_cancel(
    channel: RunnerFrameChannel, session: CoreRunnerSession
) -> None:
    """Play the Core side for a cancel that crosses the candidate's own success.

    Core deliberately holds the runner's already-arrived TERMINAL_AVAILABLE
    frame unaccepted -- accepting it first would leave the session's legal
    cancel window and refuse the cancel with no frame ever reaching the wire.
    Issuing the cancel while still in that window, then writing it before the
    READBACK response `accept` produces, reproduces the crossing deterministically:
    whatever the candidate wrote to its own outbound stream already happened,
    unobserved by either side's control flow, so CANCEL is guaranteed to reach
    the candidate as the very next frame after its TERMINAL_AVAILABLE.
    """
    while True:
        frame = _read_frame(channel)
        if frame.message is RunnerSessionMessage.TERMINAL_AVAILABLE:
            _write_frame(channel, session.cancel())
            response = session.accept(frame)
        else:
            response = (
                session.accept_terminal_record(frame)
                if frame.message is RunnerSessionMessage.TERMINAL_RECORD
                else session.accept(frame)
            )
        if response is not None:
            _write_frame(channel, response)
        if frame.message is RunnerSessionMessage.RELEASED:
            return


def _spawn_candidate_session(
    candidate_side: socket.socket,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    scenario: CandidateScenario,
    manifest_path: Path,
    identity: Path,
    journal_directory: Path,
    workspace_directory: Path,
    search_path: str | None = None,
    landlock_abi: str = "",
) -> subprocess.Popen[bytes]:
    fd = candidate_side.fileno()
    return subprocess.Popen(
        (
            sys.executable,
            "-c",
            _CANDIDATE_DRIVER,
            str(fd),
            binding.attempt_id.value,
            binding.request_hash.value,
            binding.generation_id.value,
            binding.manifest_id.value,
            invocation.value,
            scenario.value,
            str(manifest_path),
            str(identity),
            str(journal_directory),
            str(_STUBBED_PROCESS_LIMIT),
            str(workspace_directory),
            landlock_abi,
        ),
        env=None if search_path is None else {**os.environ, "PATH": search_path},
        pass_fds=(fd,),
        close_fds=True,
    )


@dataclass
class _PreparedSession:
    binding: RunnerGenerationBinding
    invocation: RunnerInvocationId
    manifest_path: Path
    identity: Path
    journal_directory: Path
    workspace_directory: Path
    core: _FakeRunnerSessionCore
    core_session: CoreRunnerSession


# The executor revision whose toolchain this repository pins but a bare test
# host need not carry -- the one pair that lets a wire test drive a real
# pre-start toolchain refusal without inventing a fake registry entry.
_CLAUDE_EXECUTOR = (
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.provider_id.value,
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision.value,
)


def _prepared_session(
    tmp_path: Path,
    scenario: CandidateScenario = CandidateScenario.SUCCESS,
    job_bytes: bytes | None = None,
    executor: tuple[str, str] | None = None,
) -> _PreparedSession:
    invocation = RunnerInvocationId("B" * 43)
    manifest = _host_manifest(
        total_attempt_milliseconds=5_000,
        terminate_grace_milliseconds=200,
        reap_deadline_milliseconds=2_000,
    )
    if executor is not None:
        manifest = replace(
            manifest, provider_id=executor[0], executor_revision=executor[1]
        )
    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(encode_runner_manifest(manifest))
    identity = _denied_identity_directory(tmp_path)
    journal_directory = tmp_path / "journal"
    workspace_directory = tmp_path / "workspace"
    workspace_directory.mkdir()
    resolved_job_bytes = (
        job_bytes
        if job_bytes is not None
        else (
            encode_free_runner_job(
                FreeRunnerHoldJob(manifest.total_attempt_milliseconds / 1000)
            )
            if scenario is CandidateScenario.CANCEL
            else _PRINT_JOB_BYTES
        )
    )
    request = _free_request(resolved_job_bytes)
    reference = free_runner_auth_reference(request.resolved_binding.auth_profile).value
    binding = _binding(request.request_hash, runner_manifest_id(manifest))
    core = _FakeRunnerSessionCore()
    core_session = CoreRunnerSession(
        binding,
        core,
        encode_runner_prepare_payload(request, reference),
        manifest,
        reference,
        invocation,
        runner_executor_cli_pin(manifest),
    )
    return _PreparedSession(
        binding,
        invocation,
        manifest_path,
        identity,
        journal_directory,
        workspace_directory,
        core,
        core_session,
    )


@pytest.mark.parametrize(
    ("executor", "empty_search_path", "landlock_abi", "expected"),
    (
        pytest.param(
            _CLAUDE_EXECUTOR,
            True,
            "",
            "runner-provider-cli-absent",
            id="the-pinned-provider-cli-is-not-installed",
        ),
        pytest.param(
            None,
            False,
            "0",
            "runner-child-boundary-unavailable",
            id="the-kernel-cannot-confine-a-child",
        ),
    ),
)
def test_runner_session_wire_names_a_pre_start_refusal_to_core(
    tmp_path: Path,
    executor: tuple[str, str] | None,
    empty_search_path: bool,
    landlock_abi: str,
    expected: str,
) -> None:
    """A Runner that cannot attest itself tells Core why, then dies.

    Driven end to end over the real socket. Neither case can be reached by
    accident on a healthy host, so each is made true deliberately and in the
    same shape it would occur: an emptied search path really does leave the
    pinned Claude executable unresolvable, and a kernel reporting no Landlock
    ABI really does leave this Runner unable to confine a child. Core must
    learn the exact code from a REFUSE frame -- not infer something from a
    dropped connection -- and must arm nothing.
    """
    prepared = _prepared_session(tmp_path, executor=executor)
    core_side, candidate_side = socket.socketpair()

    with core_side:
        candidate = _spawn_candidate_session(
            candidate_side,
            prepared.binding,
            prepared.invocation,
            CandidateScenario.SUCCESS,
            prepared.manifest_path,
            prepared.identity,
            prepared.journal_directory,
            prepared.workspace_directory,
            search_path=(
                str(tmp_path / "an-empty-search-path") if empty_search_path else None
            ),
            landlock_abi=landlock_abi,
        )
        candidate_side.close()
        try:
            prepare = prepared.core_session.accept(_read_frame(core_side))
            assert prepare is not None
            _write_frame(core_side, prepare)
            refusal = _read_frame(core_side)

            assert refusal.message is RunnerSessionMessage.REFUSE
            with pytest.raises(RunnerSessionRefusal, match=expected):
                prepared.core_session.accept(refusal)
        finally:
            candidate.wait(timeout=30)
    assert candidate.returncode != 0
    assert prepared.core.armed == 0


@pytest.mark.parametrize(
    "scenario", (CandidateScenario.SUCCESS, CandidateScenario.CANCEL)
)
def test_runner_session_wire_completes_offer_to_released(
    tmp_path: Path, scenario: CandidateScenario
) -> None:
    prepared = _prepared_session(tmp_path, scenario)
    core_side, candidate_side = socket.socketpair()

    with core_side:
        candidate = _spawn_candidate_session(
            candidate_side,
            prepared.binding,
            prepared.invocation,
            scenario,
            prepared.manifest_path,
            prepared.identity,
            prepared.journal_directory,
            prepared.workspace_directory,
        )
        candidate_side.close()
        try:
            _drive_core_session(core_side, prepared.core_session, scenario)
            returncode = candidate.wait(timeout=10)
        finally:
            # A Core-side failure must not strand the candidate subprocess
            # blocked on a read that will now never arrive.
            candidate.kill()
            candidate.wait(timeout=10)

    assert returncode == 0
    assert prepared.core.armed == 1
    assert prepared.core.committed == 1
    assert prepared.core.acknowledged == 1
    assert prepared.core.cancelled == (1 if scenario is CandidateScenario.CANCEL else 0)
    assert not (prepared.journal_directory / "terminal-record").exists()


def test_runner_session_wire_publishes_a_provider_failure_for_a_refused_job_document(
    tmp_path: Path,
) -> None:
    """A child that refuses its stdin ends the session in typed failure evidence.

    No scenario this candidate ever declares sends a malformed job document --
    the fixed program's own refusal (nonzero exit, a stderr line) is what B-1
    wired `RunnerProviderFailure`/`ProcessExitSignature` for, replacing what
    was an unconditional `RuntimeError` that discarded stderr entirely. This
    pins that envelope as this session's actual observable outcome, not a
    thrown exception, and that the exit code and stderr it carries are the
    child's own.
    """
    prepared = _prepared_session(tmp_path, job_bytes=b"not-a-free-runner-job-document")
    core_side, candidate_side = socket.socketpair()

    with core_side:
        candidate = _spawn_candidate_session(
            candidate_side,
            prepared.binding,
            prepared.invocation,
            CandidateScenario.SUCCESS,
            prepared.manifest_path,
            prepared.identity,
            prepared.journal_directory,
            prepared.workspace_directory,
        )
        candidate_side.close()
        try:
            _drive_core_session(
                core_side, prepared.core_session, CandidateScenario.SUCCESS
            )
            returncode = candidate.wait(timeout=10)
        finally:
            candidate.kill()
            candidate.wait(timeout=10)

    assert returncode == 0
    assert prepared.core.committed == 1
    envelope = prepared.core.committed_envelope
    assert envelope is not None
    assert isinstance(envelope.evidence, RunnerProviderFailure)
    assert envelope.evidence.exit_signature.return_code == 1
    assert b"free-runner-job-refused" in envelope.evidence.exit_signature.standard_error


def test_runner_session_wire_declines_a_cancel_that_crosses_terminal_available(
    tmp_path: Path,
) -> None:
    """A CANCEL that reaches the candidate after its own TERMINAL_AVAILABLE.

    The candidate always completes CandidateScenario.SUCCESS; only Core's
    driving loop decides, deterministically, that its cancel crosses the
    candidate's already-sent TERMINAL_AVAILABLE (see
    `_drive_core_session_with_crossing_cancel`). Success still wins: the
    candidate's REFUSE closes the race instead of desynchronizing the
    stream, and the readback flow completes with the evidence the candidate
    already published to its journal before offering it.
    """
    prepared = _prepared_session(tmp_path)
    core_side, candidate_side = socket.socketpair()

    with core_side:
        candidate = _spawn_candidate_session(
            candidate_side,
            prepared.binding,
            prepared.invocation,
            CandidateScenario.SUCCESS,
            prepared.manifest_path,
            prepared.identity,
            prepared.journal_directory,
            prepared.workspace_directory,
        )
        candidate_side.close()
        try:
            _drive_core_session_with_crossing_cancel(core_side, prepared.core_session)
            returncode = candidate.wait(timeout=10)
        finally:
            candidate.kill()
            candidate.wait(timeout=10)

    assert returncode == 0
    assert prepared.core.armed == 1
    assert prepared.core.committed == 1
    assert prepared.core.acknowledged == 1
    assert prepared.core.cancelled == 1
    assert not (prepared.journal_directory / "terminal-record").exists()
