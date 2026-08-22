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
from dataclasses import replace
from pathlib import Path

import pytest

from atelier2.adapters.free_runner_executor import FreeRunnerAuthorizationResolver
from atelier2.application.run_runner_session import (
    CoreRunnerSession,
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
from atelier2.runner.session import (
    CandidateScenario,
    RunnerFrameChannel,
    _pid_limit,
    _status_field,
)

# Runs `run_candidate_session` in a fresh interpreter, with its own
# `PR_SET_NO_NEW_PRIVS` and a Landlock allowlist widened to reach this test
# interpreter's own install prefix (the production allowlist names the
# deployed candidate image's layout instead — the same accommodation
# `test_runner_child.py` already makes for the identical reason). Nothing
# about the session's own logic is touched.
_CANDIDATE_DRIVER = """
import ctypes
import socket
import sys
from pathlib import Path

_PR_SET_NO_NEW_PRIVS = 38

(
    fd, attempt_id, request_hash, generation_id, manifest_id, invocation_id,
    scenario_value, manifest_path, identity, journal_directory,
) = sys.argv[1:11]

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


def _interpreter_reachable_allowlist():
    return tuple(
        path
        for path in (
            Path("/usr"), Path("/lib"), Path("/lib64"), Path("/proc"), Path("/dev"),
            Path(sys.prefix), Path(sys.base_prefix),
        )
        if path.exists()
    )


session_module.child_allowlist = _interpreter_reachable_allowlist
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
        envelope = decode_runner_terminal_evidence_record(record)
        if not isinstance(envelope, RunnerTerminalEvidenceEnvelope):
            raise TypeError("runner-terminal-record-corrupt")
        return RunnerTerminalEvidenceHash.for_envelope(envelope)

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


def _free_request() -> AgentExecutionRequestV2:
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
        b"Return the one candidate result.",
    )


def _host_manifest(**timings: int) -> RunnerManifestV1:
    """A candidate manifest declaring exactly what this host will measure.

    `run_candidate_session`'s READY step re-measures uid, gid, capabilities,
    and the cgroup pid limit live; a manifest built from anything else would
    make `require_ready_matches_manifest` refuse a healthy session.
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
        ),
        effective_uid=os.getuid(),
        effective_gid=os.getgid(),
        effective_capabilities=_status_field("CapEff").lower(),
        process_limit=_pid_limit(),
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


def _spawn_candidate_session(
    candidate_side: socket.socket,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    scenario: CandidateScenario,
    manifest_path: Path,
    identity: Path,
    journal_directory: Path,
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
        ),
        pass_fds=(fd,),
        close_fds=True,
    )


@pytest.mark.parametrize(
    "scenario", (CandidateScenario.SUCCESS, CandidateScenario.CANCEL)
)
def test_runner_session_wire_completes_offer_to_released(
    tmp_path: Path, scenario: CandidateScenario
) -> None:
    invocation = RunnerInvocationId("B" * 43)
    manifest = _host_manifest(
        total_attempt_milliseconds=5_000,
        terminate_grace_milliseconds=200,
        reap_deadline_milliseconds=2_000,
    )
    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(encode_runner_manifest(manifest))
    identity = _denied_identity_directory(tmp_path)
    journal_directory = tmp_path / "journal"
    request = _free_request()
    reference = FreeRunnerAuthorizationResolver().reference_for(
        request.resolved_binding.auth_profile
    )
    binding = _binding(request.request_hash, runner_manifest_id(manifest))
    core = _FakeRunnerSessionCore()
    core_session = CoreRunnerSession(
        binding,
        core,
        encode_runner_prepare_payload(request, reference),
        manifest,
        reference,
        invocation,
    )
    core_side, candidate_side = socket.socketpair()

    with core_side:
        candidate = _spawn_candidate_session(
            candidate_side,
            binding,
            invocation,
            scenario,
            manifest_path,
            identity,
            journal_directory,
        )
        candidate_side.close()
        try:
            _drive_core_session(core_side, core_session, scenario)
            returncode = candidate.wait(timeout=10)
        finally:
            # A Core-side failure must not strand the candidate subprocess
            # blocked on a read that will now never arrive.
            candidate.kill()
            candidate.wait(timeout=10)

    assert returncode == 0
    assert core.armed == 1
    assert core.committed == 1
    assert core.acknowledged == 1
    assert core.cancelled == (1 if scenario is CandidateScenario.CANCEL else 0)
    assert not (journal_directory / "terminal-record").exists()
