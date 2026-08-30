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
import ssl
import subprocess
import sys
import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from atelier2.adapters.claude_subscription import CLAUDE_SUBSCRIPTION_EXECUTOR_KEY
from atelier2.adapters.free_runner_executor import (
    FreeRunnerHoldJob,
    FreeRunnerPrintJob,
    encode_free_runner_job,
    free_runner_auth_reference,
)
from atelier2.adapters.runner_cli_pins import runner_executor_cli_pin
from atelier2.adapters.runner_core_transport import (
    RunnerPeerPin,
    accept_and_drive_session,
    accept_pinned_runner,
    bind_session_listener,
    drive_until_released_or_dropped,
)
from atelier2.adapters.runner_journal import RunnerJournal
from atelier2.adapters.runner_tls import (
    CORE_DNS_NAME,
    core_uri_for_certificate,
    pin_tls_13,
    runner_uri_for_invocation,
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
    RunnerProviderResult,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutionResult,
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
    CANDIDATE_JOURNAL_BYTES,
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
from atelier2.runner.session import CandidateScenario, RunnerFrameChannel, _status_field
from tests.scenarios.transcripts import largest_attempt_transcript

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

# Every candidate driver below inherits one already-connected channel as a
# descriptor rather than a Core address it could dial again, so its channel
# source hands that channel over once and then refuses. A test whose Core
# drops the connection therefore ends the candidate right there, instead of
# leaving the session's reconnect loop waiting out the manifest's whole
# attempt span for a Core that no test is going to bring back;
# `test_runner_session_reconnect.py` is where a source that really reconnects
# lives.
_ONE_CONNECTION_SOURCE = """
_connections = [channel]


def _connect_to_core(seconds_left):
    del seconds_left
    if not _connections:
        raise RuntimeError("this candidate was handed exactly one connection")
    return _connections.pop()
"""

# Runs `run_candidate_session` in a fresh interpreter with its own
# `PR_SET_NO_NEW_PRIVS` and a stubbed cgroup pid limit reading (see
# `_STUBBED_PROCESS_LIMIT` above). The child's Landlock surface arrives
# through the manifest `_host_manifest` attests, exactly as it does in the
# deployed image. Nothing about the session's own logic is touched.
_CANDIDATE_DRIVER = (
    """
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
channel = socket.socket(fileno=int(fd))
"""
    + _ONE_CONNECTION_SOURCE
    + """
run_candidate_session(
    _connect_to_core,
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
)


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
        self.acknowledged_tombstone: RunnerTerminalEvidenceAckTombstone | None = None

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
        decoded = decode_runner_terminal_evidence_record(tombstone)
        if not isinstance(decoded, RunnerTerminalEvidenceAckTombstone):
            raise TypeError("runner-terminal-record-corrupt")
        if decoded.binding != binding or decoded.evidence_hash != evidence_hash:
            raise ValueError("runner ACK tombstone differs")
        self.acknowledged += 1
        self.acknowledged_tombstone = decoded

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


_WIRE_MANIFEST_TIMINGS = {
    "total_attempt_milliseconds": 5_000,
    "terminate_grace_milliseconds": 200,
    "reap_deadline_milliseconds": 2_000,
}
"""Long enough for a real handshake, child and reap on a loaded host; short
enough that a test which drives one of those endings wrong fails rather than
hangs. A test with its own timing subject overrides exactly what it needs."""


def _prepared_session(
    tmp_path: Path,
    scenario: CandidateScenario = CandidateScenario.SUCCESS,
    job_bytes: bytes | None = None,
    executor: tuple[str, str] | None = None,
    **timings: int,
) -> _PreparedSession:
    invocation = RunnerInvocationId("B" * 43)
    manifest = _host_manifest(**{**_WIRE_MANIFEST_TIMINGS, **timings})
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


def _largest_originable_terminal_envelope(
    prepared: _PreparedSession,
) -> RunnerTerminalEvidenceEnvelope:
    """The biggest terminal fact a real Runner can hand over on this session.

    Every width is the real one: the session pins its generation and invocation
    tokens to 43 base64url characters, the provider answer is as wide as the V2
    output bound allows and the transcript fills the document bound. A provider
    *failure* carrying the same transcript would encode 38 bytes larger and is
    equally journal-legal, but `runner/session.py` still refuses to publish a
    failure transcript, so it is not a record this path can originate today.
    """

    return RunnerTerminalEvidenceEnvelope(
        prepared.binding,
        prepared.invocation,
        RunnerProviderResult(
            AgentExecutionResult(
                b"x" * MAXIMUM_AGENT_OUTPUT_BYTES_V2, largest_attempt_transcript()
            ),
        ),
    )


def test_both_wire_ends_derive_the_free_runner_auth_reference_from_one_owner() -> None:
    """Core's encode side (`_prepared_session` above) and the Runner's resolve
    side (`runner.session`) must never grow two derivations of the same
    reference -- they share the one function `adapters.free_runner_executor`
    owns."""
    from atelier2.runner import session as runner_session

    assert runner_session.free_runner_auth_reference is free_runner_auth_reference


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


# ---------------------------------------------------------------------------
# `atelier2.adapters.runner_core_transport` -- the production Core-side
# transport owner, pinned here over a genuine loopback TLS connection rather
# than the plain `socket.socketpair()` the tests above use. These are the
# transport's own accept/reconnect bound and peer fence, plus one full
# end-to-end run of the real candidate session driven through it; the wire
# protocol itself is already pinned above.
# ---------------------------------------------------------------------------


def _self_signed_ca() -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wire-test-ca")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _private_key_pem(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )


def _leaf_identity(
    ca_key: rsa.RSAPrivateKey,
    ca: x509.Certificate,
    key: rsa.RSAPrivateKey,
    *,
    dns_name: str | None,
    uri: str,
    eku: x509.ObjectIdentifier,
) -> bytes:
    """One leaf certificate PEM under `ca`, for the already-generated `key`."""
    names: list[x509.GeneralName] = [] if dns_name is None else [x509.DNSName(dns_name)]
    names.append(x509.UniformResourceIdentifier(uri))
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wire-test-leaf")])
        )
        .issuer_name(ca.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(minutes=1))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return certificate.public_bytes(serialization.Encoding.PEM)


@dataclass
class _LoopbackAuthority:
    """A throwaway CA plus one Core server identity under it, for a real TLS
    loopback accept -- the same shape a real issuer's mTLS handoff carries."""

    ca_key: rsa.RSAPrivateKey
    ca: x509.Certificate
    ca_pem: bytes
    ca_path: Path
    core_context: ssl.SSLContext


def _loopback_authority(tmp_path: Path) -> _LoopbackAuthority:
    ca_key, ca = _self_signed_ca()
    ca_pem = ca.public_bytes(serialization.Encoding.PEM)
    ca_path = tmp_path / "ca.crt"
    ca_path.write_bytes(ca_pem)
    core_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    core_uri = core_uri_for_certificate(core_key.public_key())
    core_cert_path = tmp_path / "core.crt"
    core_cert_path.write_bytes(
        _leaf_identity(
            ca_key,
            ca,
            core_key,
            dns_name=CORE_DNS_NAME,
            uri=core_uri,
            eku=ExtendedKeyUsageOID.SERVER_AUTH,
        )
    )
    core_key_path = tmp_path / "core.key"
    core_key_path.write_bytes(_private_key_pem(core_key))
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    pin_tls_13(context)
    context.load_cert_chain(core_cert_path, core_key_path)
    context.load_verify_locations(cafile=ca_path)
    context.verify_mode = ssl.CERT_REQUIRED
    return _LoopbackAuthority(ca_key, ca, ca_pem, ca_path, context)


def _runner_identity(
    tmp_path: Path, authority: _LoopbackAuthority, uri: str, name: str = "runner"
) -> tuple[Path, Path, bytes]:
    """A Runner client identity under `authority`, as (cert path, key path, leaf DER)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_pem = _leaf_identity(
        authority.ca_key,
        authority.ca,
        key,
        dns_name=None,
        uri=uri,
        eku=ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    cert_path = tmp_path / f"{name}.crt"
    cert_path.write_bytes(leaf_pem)
    key_path = tmp_path / f"{name}.key"
    key_path.write_bytes(_private_key_pem(key))
    leaf_der = x509.load_pem_x509_certificate(leaf_pem).public_bytes(
        serialization.Encoding.DER
    )
    return cert_path, key_path, leaf_der


def _client_tls_context(
    authority: _LoopbackAuthority, cert_path: Path, key_path: Path
) -> ssl.SSLContext:
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH, cafile=authority.ca_path
    )
    pin_tls_13(context)
    context.load_cert_chain(cert_path, key_path)
    return context


_TLS_CANDIDATE_DRIVER = (
    """
import ctypes
import socket
import ssl
import sys
from pathlib import Path

_PR_SET_NO_NEW_PRIVS = 38

(
    fd, attempt_id, request_hash, generation_id, manifest_id, invocation_id,
    scenario_value, manifest_path, identity, journal_directory, process_limit,
    workspace_directory, runner_cert, runner_key, ca_cert, server_hostname,
) = sys.argv[1:17]

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

session_module._pid_limit = lambda: int(process_limit)
session_module._runner_workspace_directory = lambda: Path(workspace_directory)

context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_cert)
context.minimum_version = ssl.TLSVersion.TLSv1_3
context.maximum_version = ssl.TLSVersion.TLSv1_3
context.load_cert_chain(runner_cert, runner_key)
channel = context.wrap_socket(
    socket.socket(fileno=int(fd)), server_hostname=server_hostname
)
"""
    + _ONE_CONNECTION_SOURCE
    + """
run_candidate_session(
    _connect_to_core,
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
)


def _spawn_tls_candidate_session(
    raw_client: socket.socket,
    prepared: _PreparedSession,
    scenario: CandidateScenario,
    runner_cert_path: Path,
    runner_key_path: Path,
    ca_path: Path,
    server_hostname: str,
) -> subprocess.Popen[bytes]:
    fd = raw_client.fileno()
    return subprocess.Popen(
        (
            sys.executable,
            "-c",
            _TLS_CANDIDATE_DRIVER,
            str(fd),
            prepared.binding.attempt_id.value,
            prepared.binding.request_hash.value,
            prepared.binding.generation_id.value,
            prepared.binding.manifest_id.value,
            prepared.invocation.value,
            scenario.value,
            str(prepared.manifest_path),
            str(prepared.identity),
            str(prepared.journal_directory),
            str(_STUBBED_PROCESS_LIMIT),
            str(prepared.workspace_directory),
            str(runner_cert_path),
            str(runner_key_path),
            str(ca_path),
            server_hostname,
        ),
        pass_fds=(fd,),
        close_fds=True,
    )


def _run_tls_candidate_to_released(tmp_path: Path, prepared: _PreparedSession) -> int:
    authority = _loopback_authority(tmp_path)
    runner_uri = runner_uri_for_invocation(prepared.binding, prepared.invocation)
    runner_cert_path, runner_key_path, runner_leaf_der = _runner_identity(
        tmp_path, authority, runner_uri
    )
    server = bind_session_listener(0, accept_timeout_seconds=10.0)
    port = server.getsockname()[1]
    pin = RunnerPeerPin(
        authority.ca_pem, runner_uri, ExtendedKeyUsageOID.CLIENT_AUTH, runner_leaf_der
    )

    with server:
        raw_client = socket.create_connection(("127.0.0.1", port), timeout=10)
        candidate = _spawn_tls_candidate_session(
            raw_client,
            prepared,
            CandidateScenario.SUCCESS,
            runner_cert_path,
            runner_key_path,
            authority.ca_path,
            CORE_DNS_NAME,
        )
        raw_client.close()
        try:
            accept_and_drive_session(
                server,
                authority.core_context,
                pin,
                lambda: prepared.core_session,
                1,
            )
            return candidate.wait(timeout=10)
        except Exception:
            try:
                candidate.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
            raise
        finally:
            if candidate.poll() is None:
                candidate.kill()
            candidate.wait(timeout=10)


def test_runner_core_transport_drives_a_real_candidate_session_to_released_over_tls(
    tmp_path: Path,
) -> None:
    """The production transport (`accept_and_drive_session`), not a hand-rolled
    stand-in, drives the real Runner subprocess from INVOCATION_OFFER to
    RELEASED over a genuine TLS loopback connection -- the transport the
    witness Core (`tests/witness/runner_candidate_core.py`) is now the first
    caller of."""
    prepared = _prepared_session(tmp_path)
    returncode = _run_tls_candidate_to_released(tmp_path, prepared)

    assert returncode == 0
    assert prepared.core.armed == 1
    assert prepared.core.committed == 1
    assert prepared.core.acknowledged == 1


@pytest.mark.proves("what-may-be-stored-can-be-delivered")
def test_runner_core_transport_delivers_the_largest_originable_record_over_tls(
    tmp_path: Path,
) -> None:
    """What the journal admits, the session hands over. The Runner publishes its
    widest terminal fact, and the production transport carries it the whole way
    -- real subprocess, real TLS, real `CoreRunnerSession` -- through commit,
    ACK tombstone, RELEASE and journal removal. A transport bound below the
    record bound stops this at `runner-session-oversized` instead."""

    prepared = _prepared_session(tmp_path)
    envelope = _largest_originable_terminal_envelope(prepared)
    RunnerJournal(prepared.journal_directory).publish(envelope, CANDIDATE_JOURNAL_BYTES)

    returncode = _run_tls_candidate_to_released(tmp_path, prepared)

    evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
    assert returncode == 0
    assert prepared.core.armed == 1
    assert prepared.core.committed == 1
    assert prepared.core.committed_envelope == envelope
    assert prepared.core.acknowledged == 1
    assert prepared.core.acknowledged_tombstone == RunnerTerminalEvidenceAckTombstone(
        envelope.binding,
        envelope.invocation_id,
        evidence_hash,
    )
    assert not (prepared.journal_directory / "terminal-record").exists()


def test_runner_core_transport_raises_when_no_runner_connects_within_the_accept_bound() -> (
    None
):
    server = bind_session_listener(0, accept_timeout_seconds=0.05)
    with (
        server,
        pytest.raises(RuntimeError, match="accept bound"),
    ):
        accept_and_drive_session(
            server,
            ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER),
            RunnerPeerPin(
                b"", "urn:test:unreachable", ExtendedKeyUsageOID.CLIENT_AUTH, b""
            ),
            lambda: pytest.fail("no connection should ever have been accepted"),
            1,
        )


def test_runner_core_transport_accept_pinned_runner_refuses_a_leaf_foreign_to_the_pin(
    tmp_path: Path,
) -> None:
    """The pinned-leaf fence: a certificate that passes the CA/SAN/EKU check
    (same claimed identity) but is not byte-for-byte the pinned leaf is still
    refused -- the fingerprint half of the fence `validate_peer_certificate`
    alone cannot provide."""
    authority = _loopback_authority(tmp_path)
    uri = "urn:atelier2:runner:v1:peer-fence-test"
    _pinned_cert_path, _pinned_key_path, pinned_leaf_der = _runner_identity(
        tmp_path, authority, uri, name="pinned"
    )
    presenting_cert_path, presenting_key_path, _presenting_leaf_der = _runner_identity(
        tmp_path, authority, uri, name="presenting"
    )
    client_context = _client_tls_context(
        authority, presenting_cert_path, presenting_key_path
    )
    server = bind_session_listener(0, accept_timeout_seconds=5.0)
    port = server.getsockname()[1]
    pin = RunnerPeerPin(
        authority.ca_pem, uri, ExtendedKeyUsageOID.CLIENT_AUTH, pinned_leaf_der
    )

    def _connect() -> None:
        with (
            socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
            client_context.wrap_socket(raw, server_hostname=CORE_DNS_NAME),
        ):
            pass

    with server:
        connector = threading.Thread(target=_connect)
        connector.start()
        try:
            with pytest.raises(RuntimeError, match="Runner peer leaf differs"):
                accept_pinned_runner(server, authority.core_context, pin)
        finally:
            connector.join(timeout=5)


def test_runner_core_transport_accepts_a_second_connection_after_the_first_drops(
    tmp_path: Path,
) -> None:
    """`accept_and_drive_session`'s own reconnect bound: a first connection
    that drops before RELEASED does not fail the call so long as a second,
    equally pinned connection reaches RELEASED -- and the session factory
    runs exactly once, against the first connection, never rebuilt for the
    reconnect. A minimal fake session stands in here (structurally satisfying
    `RunnerSessionAdvancer`) to pin the transport's own mechanics apart from
    the real `CoreRunnerSession` protocol state machine, which the end-to-end
    test above already exercises."""
    authority = _loopback_authority(tmp_path)
    uri = "urn:atelier2:runner:v1:reconnect-test"
    runner_cert_path, runner_key_path, runner_leaf_der = _runner_identity(
        tmp_path, authority, uri
    )
    client_context = _client_tls_context(authority, runner_cert_path, runner_key_path)
    server = bind_session_listener(0, accept_timeout_seconds=5.0)
    port = server.getsockname()[1]
    pin = RunnerPeerPin(
        authority.ca_pem, uri, ExtendedKeyUsageOID.CLIENT_AUTH, runner_leaf_der
    )
    binding = _binding(AgentExecutionRequestHash("a" * 64), RunnerManifestId("b" * 64))
    invocation = RunnerInvocationId("B" * 43)

    def _connect_and_drop() -> None:
        with (
            socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
            client_context.wrap_socket(raw, server_hostname=CORE_DNS_NAME),
        ):
            pass

    def _connect_and_release() -> None:
        with (
            socket.create_connection(("127.0.0.1", port), timeout=5) as raw,
            client_context.wrap_socket(
                raw, server_hostname=CORE_DNS_NAME
            ) as connection,
        ):
            connection.sendall(
                encode_runner_session_frame(
                    RunnerSessionFrame(
                        RunnerSessionMessage.RELEASED,
                        1,
                        binding,
                        invocation,
                        (b"released",),
                    )
                )
            )

    build_count = 0

    def _session_factory() -> _FakeSessionAdvancer:
        nonlocal build_count
        build_count += 1
        return _FakeSessionAdvancer()

    errors: list[BaseException] = []

    def _run_server() -> None:
        try:
            accept_and_drive_session(
                server, authority.core_context, pin, _session_factory, 2
            )
        except BaseException as error:  # noqa: BLE001 -- surfaced to the test thread
            errors.append(error)

    with server:
        server_thread = threading.Thread(target=_run_server)
        server_thread.start()
        try:
            dropper = threading.Thread(target=_connect_and_drop)
            dropper.start()
            dropper.join(timeout=5)
            releaser = threading.Thread(target=_connect_and_release)
            releaser.start()
            releaser.join(timeout=5)
        finally:
            server_thread.join(timeout=5)

    assert not errors
    assert build_count == 1


def test_runner_core_transport_drive_writes_the_on_started_hooks_frame() -> None:
    """The `on_started` hook's one write-back: STARTED triggers it, and its
    returned frame -- not the fake session's own `accept` response, which is
    always withheld below -- is exactly what reaches the wire."""
    binding = _binding(AgentExecutionRequestHash("a" * 64), RunnerManifestId("c" * 64))
    invocation = RunnerInvocationId("C" * 43)
    triggered = RunnerSessionFrame(
        RunnerSessionMessage.CANCEL,
        1,
        binding,
        invocation,
        (b"run", b"cmd", (0).to_bytes(8, "big"), b"NONE"),
    )
    driver, connection = socket.socketpair()
    errors: list[BaseException] = []

    def _drive() -> None:
        try:
            drive_until_released_or_dropped(
                connection, _FakeSessionAdvancer(), lambda session: triggered
            )
        except BaseException as error:  # noqa: BLE001 -- surfaced to the test thread
            errors.append(error)

    with driver, connection:
        thread = threading.Thread(target=_drive)
        thread.start()
        try:
            driver.sendall(
                encode_runner_session_frame(
                    RunnerSessionFrame(
                        RunnerSessionMessage.STARTED,
                        1,
                        binding,
                        invocation,
                        ((1).to_bytes(8, "big"),),
                    )
                )
            )
            written = _read_frame(driver)
            assert written == triggered

            driver.sendall(
                encode_runner_session_frame(
                    RunnerSessionFrame(
                        RunnerSessionMessage.RELEASED,
                        2,
                        binding,
                        invocation,
                        (b"released",),
                    )
                )
            )
        finally:
            thread.join(timeout=5)

    assert not errors


def test_runner_core_transport_drive_writes_nothing_when_on_started_returns_none() -> (
    None
):
    """The `on_started is not None and ... is not None` guard: STARTED still
    reaches the hook, but a `None` answer writes nothing back for it."""
    binding = _binding(AgentExecutionRequestHash("a" * 64), RunnerManifestId("d" * 64))
    invocation = RunnerInvocationId("D" * 43)
    driver, connection = socket.socketpair()
    errors: list[BaseException] = []

    def _drive() -> None:
        try:
            drive_until_released_or_dropped(
                connection, _FakeSessionAdvancer(), lambda session: None
            )
        except BaseException as error:  # noqa: BLE001 -- surfaced to the test thread
            errors.append(error)

    with driver, connection:
        thread = threading.Thread(target=_drive)
        thread.start()
        try:
            driver.sendall(
                encode_runner_session_frame(
                    RunnerSessionFrame(
                        RunnerSessionMessage.STARTED,
                        1,
                        binding,
                        invocation,
                        ((1).to_bytes(8, "big"),),
                    )
                )
            )
            driver.settimeout(0.2)
            with pytest.raises(TimeoutError):
                driver.recv(1)
            driver.settimeout(None)

            driver.sendall(
                encode_runner_session_frame(
                    RunnerSessionFrame(
                        RunnerSessionMessage.RELEASED,
                        2,
                        binding,
                        invocation,
                        (b"released",),
                    )
                )
            )
        finally:
            thread.join(timeout=5)

    assert not errors


class _FakeSessionAdvancer:
    """Answers nothing and is never asked to cancel -- see
    `test_runner_core_transport_accepts_a_second_connection_after_the_first_drops`."""

    def accept(self, frame: RunnerSessionFrame) -> RunnerSessionFrame | None:
        return None

    def accept_terminal_record(
        self, frame: RunnerSessionFrame
    ) -> RunnerSessionFrame | None:
        return None

    def cancel(self) -> RunnerSessionFrame:
        raise NotImplementedError
