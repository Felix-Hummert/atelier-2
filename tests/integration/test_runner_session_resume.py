"""Resume: a prior lifetime's retained journal record replays without a second child.

The crash cuts here mirror the wire-coupling infrastructure `B1` extracted
(`test_runner_session_wire.py`) and the store idempotency `#15-A` already
proved: they inject a deterministic exit at an exact point in the candidate's
own code (never an external, racy `SIGKILL`) and, on the Core side, drop the
in-memory `CoreRunnerSession` and build a fresh one against the same durable
store -- exactly the two techniques the Plan-Gate pinned. `CandidateScenario`
and the Docker witness (`scripts/runner_candidate.sh`,
`tests/witness/runner_candidate_core.py`) are untouched; those crash legs
belong to `#15-B5`.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.runner_session_core import DbosRunnerSessionCore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.free_runner_executor import FreeRunnerAuthorizationResolver
from atelier2.adapters.runner_journal import RunnerJournal
from atelier2.application.run_runner_session import (
    CoreRunnerSession,
    RunnerSessionRefusal,
    encode_runner_prepare_payload,
)
from atelier2.contracts.agent_attempts import (
    RunnerEvidenceAcceptancePhase,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
    RunnerProviderResult,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.runner_manifests import (
    RunnerManifestV1,
    encode_runner_manifest,
    runner_manifest_id,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame, RunnerSessionMessage
from atelier2.contracts.runner_terminal_evidence_codec import (
    encode_runner_terminal_evidence_record,
)
from atelier2.runner.session import CandidateScenario
from tests.integration.test_agent_attempts import attempt_request, attempt_runtime
from tests.integration.test_runner_session_application import (
    _candidate_manifest,
    _ready_payload,
)
from tests.integration.test_runner_session_application import (
    _free_request as _application_free_request,
)
from tests.integration.test_runner_session_wire import (
    _STUBBED_PROCESS_LIMIT,
    _denied_identity_directory,
    _drive_core_session,
    _FakeRunnerSessionCore,
    _host_manifest,
)
from tests.integration.test_runner_session_wire import (
    _binding as _wire_binding,
)
from tests.integration.test_runner_session_wire import (
    _free_request as _wire_free_request,
)
from tests.scenarios.agents import agent_attempt_execution

# The candidate driver `_CANDIDATE_DRIVER` (test_runner_session_wire.py) runs
# `run_candidate_session` unmodified; this one additionally, and only for a
# named test process, monkeypatches the two no-op crash-cut seams
# `session.py` exposes for exactly this purpose. Nothing about production
# code changes: the seams are no-ops unless a test replaces them.
_RESUMABLE_CANDIDATE_DRIVER = """
import ctypes
import os
import socket
import sys
from pathlib import Path

_PR_SET_NO_NEW_PRIVS = 38

(
    fd, attempt_id, request_hash, generation_id, manifest_id, invocation_id,
    scenario_value, manifest_path, identity, journal_directory, process_limit,
    crash_point,
) = sys.argv[1:13]

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
session_module._pid_limit = lambda: int(process_limit)
if crash_point == "after-publish":
    session_module._after_terminal_evidence_published = lambda: os._exit(9)
elif crash_point == "after-ack-tombstone":
    session_module._after_ack_tombstone_published = lambda: os._exit(9)
elif crash_point:
    raise ValueError(f"unknown crash point {crash_point!r}")

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


def _spawn_resumable_candidate(
    candidate_side: socket.socket,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    scenario: CandidateScenario,
    manifest_path: Path,
    identity: Path,
    journal_directory: Path,
    crash_point: str = "",
) -> subprocess.Popen[bytes]:
    fd = candidate_side.fileno()
    return subprocess.Popen(
        (
            sys.executable,
            "-c",
            _RESUMABLE_CANDIDATE_DRIVER,
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
            crash_point,
        ),
        pass_fds=(fd,),
        close_fds=True,
    )


_RESUME_MANIFEST_TIMINGS = {
    "total_attempt_milliseconds": 4_000,
    "terminate_grace_milliseconds": 100,
    "reap_deadline_milliseconds": 1_000,
}
"""Short enough that a wrongly relaunched CANCEL-hold child misses every test
timeout, long enough for the real reap in the crashing first lifetime."""


@dataclass
class _ResumableFixture:
    binding: RunnerGenerationBinding
    invocation: RunnerInvocationId
    manifest_path: Path
    identity: Path
    journal_directory: Path


def _resumable_fixture(tmp_path: Path) -> _ResumableFixture:
    invocation = RunnerInvocationId("B" * 43)
    manifest = _host_manifest(**_RESUME_MANIFEST_TIMINGS)
    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(encode_runner_manifest(manifest))
    identity = _denied_identity_directory(tmp_path)
    journal_directory = tmp_path / "journal"
    request = _wire_free_request()
    binding = _wire_binding(request.request_hash, runner_manifest_id(manifest))
    return _ResumableFixture(
        binding, invocation, manifest_path, identity, journal_directory
    )


def _fresh_core_session(
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    core: _FakeRunnerSessionCore,
) -> CoreRunnerSession:
    request = _wire_free_request()
    manifest = _host_manifest(**_RESUME_MANIFEST_TIMINGS)
    reference = FreeRunnerAuthorizationResolver().reference_for(
        request.resolved_binding.auth_profile
    )
    return CoreRunnerSession(
        binding,
        core,
        encode_runner_prepare_payload(request, reference),
        manifest,
        reference,
        invocation,
    )


def test_resume_delivers_retained_evidence_without_a_second_child(
    tmp_path: Path,
) -> None:
    """Crash cut C3: exit right after journaling evidence, before TERMINAL_AVAILABLE.

    The candidate's journal already holds a fixed cancellation fact when the
    resumed lifetime starts; it must replay the full OFFER-to-RELEASED
    handshake using that fact instead of racing a second, minutes-long
    candidate child against it.
    """
    fixture = _resumable_fixture(tmp_path)
    first_core = _FakeRunnerSessionCore()
    first_session = _fresh_core_session(fixture.binding, fixture.invocation, first_core)
    core_side, candidate_side = socket.socketpair()
    with core_side:
        candidate = _spawn_resumable_candidate(
            candidate_side,
            fixture.binding,
            fixture.invocation,
            CandidateScenario.CANCEL,
            fixture.manifest_path,
            fixture.identity,
            fixture.journal_directory,
            crash_point="after-publish",
        )
        candidate_side.close()
        try:
            with pytest.raises(ConnectionError):
                _drive_core_session(core_side, first_session, CandidateScenario.CANCEL)
            assert candidate.wait(timeout=10) != 0
        finally:
            candidate.kill()
            candidate.wait(timeout=10)

    retained = RunnerJournal(fixture.journal_directory).readback(fixture.binding)
    assert isinstance(retained, RunnerTerminalEvidenceEnvelope)

    second_core = _FakeRunnerSessionCore()
    second_session = _fresh_core_session(
        fixture.binding, fixture.invocation, second_core
    )
    core_side, candidate_side = socket.socketpair()
    with core_side:
        candidate = _spawn_resumable_candidate(
            candidate_side,
            fixture.binding,
            fixture.invocation,
            CandidateScenario.CANCEL,
            fixture.manifest_path,
            fixture.identity,
            fixture.journal_directory,
        )
        candidate_side.close()
        try:
            # SUCCESS drives no CANCEL frame; a wrongly relaunched CANCEL-hold
            # child would ignore SIGTERM and never finish inside this bound.
            _drive_core_session(core_side, second_session, CandidateScenario.SUCCESS)
            returncode = candidate.wait(timeout=2)
        finally:
            candidate.kill()
            candidate.wait(timeout=10)

    assert returncode == 0
    assert (second_core.armed, second_core.committed, second_core.acknowledged) == (
        1,
        1,
        1,
    )
    assert not (fixture.journal_directory / "terminal-record").exists()


def test_resume_completes_from_a_retained_ack_tombstone(tmp_path: Path) -> None:
    """Crash cut C5: exit right after ACK_TOMBSTONE, before RELEASE.

    The candidate's journal already dropped its envelope for the tombstone by
    the time the resumed lifetime starts; it has nothing left to resend as a
    fresh `TERMINAL_RECORD` and instead re-offers the retained tombstone,
    which Core confirms against the durable attempt instead of committing it
    as new evidence.
    """
    fixture = _resumable_fixture(tmp_path)
    first_core = _FakeRunnerSessionCore()
    first_session = _fresh_core_session(fixture.binding, fixture.invocation, first_core)
    core_side, candidate_side = socket.socketpair()
    with core_side:
        candidate = _spawn_resumable_candidate(
            candidate_side,
            fixture.binding,
            fixture.invocation,
            CandidateScenario.SUCCESS,
            fixture.manifest_path,
            fixture.identity,
            fixture.journal_directory,
            crash_point="after-ack-tombstone",
        )
        candidate_side.close()
        try:
            with pytest.raises(ConnectionError):
                _drive_core_session(core_side, first_session, CandidateScenario.SUCCESS)
            assert candidate.wait(timeout=10) != 0
        finally:
            candidate.kill()
            candidate.wait(timeout=10)

    retained = RunnerJournal(fixture.journal_directory).readback(fixture.binding)
    assert isinstance(retained, RunnerTerminalEvidenceAckTombstone)

    second_core = _FakeRunnerSessionCore()
    second_session = _fresh_core_session(
        fixture.binding, fixture.invocation, second_core
    )
    core_side, candidate_side = socket.socketpair()
    with core_side:
        candidate = _spawn_resumable_candidate(
            candidate_side,
            fixture.binding,
            fixture.invocation,
            CandidateScenario.SUCCESS,
            fixture.manifest_path,
            fixture.identity,
            fixture.journal_directory,
        )
        candidate_side.close()
        try:
            _drive_core_session(core_side, second_session, CandidateScenario.SUCCESS)
            returncode = candidate.wait(timeout=10)
        finally:
            candidate.kill()
            candidate.wait(timeout=10)

    assert returncode == 0
    assert (second_core.armed, second_core.committed, second_core.acknowledged) == (
        1,
        1,
        1,
    )
    assert not (fixture.journal_directory / "terminal-record").exists()


# --- Store-backed cuts: a fresh CoreRunnerSession against the same durable
# store, driven by hand-built frames rather than a live candidate process.
# `arm_runner_invocation`, `commit_runner_terminal_evidence`, and
# `mark_runner_evidence_acknowledged` are the store's own proven idempotent
# owners (#15-A); these cuts prove `CoreRunnerSession`/`DbosRunnerSessionCore`
# reach them correctly across a real Core restart.


@dataclass
class _StoreFixture:
    runtime: DbosRuntime
    store: DbosAgentAttemptStore
    execution: AgentAttemptExecution
    binding: RunnerGenerationBinding
    invocation: RunnerInvocationId
    manifest: RunnerManifestV1
    reference: str
    prepare_payload: tuple[bytes, ...]


def _store_fixture(tmp_path: Path, run_name: str) -> _StoreFixture:
    runtime = attempt_runtime(tmp_path)
    runtime.initialize_storage()
    request = attempt_request(runtime, run_name)
    execution = agent_attempt_execution(request)
    store = DbosAgentAttemptStore(runtime.engine)
    store.prepare(execution)
    binding = RunnerGenerationBinding(
        execution.attempt_id,
        execution.request.request_hash,
        RunnerGenerationId("A" * 43),
        RunnerManifestId.of(b"resume-manifest-v1"),
    )
    store.bind_runner_generation(execution, binding)
    free_request = _application_free_request()
    reference = FreeRunnerAuthorizationResolver().reference_for(
        free_request.resolved_binding.auth_profile
    )
    return _StoreFixture(
        runtime,
        store,
        execution,
        binding,
        RunnerInvocationId("B" * 43),
        _candidate_manifest(),
        reference,
        encode_runner_prepare_payload(free_request, reference),
    )


def _session_for(
    fixture: _StoreFixture, invocation: RunnerInvocationId
) -> CoreRunnerSession:
    return CoreRunnerSession(
        fixture.binding,
        DbosRunnerSessionCore(fixture.execution, fixture.store, "resume-cut-cancel"),
        fixture.prepare_payload,
        fixture.manifest,
        fixture.reference,
        invocation,
    )


def _frame(
    fixture: _StoreFixture,
    invocation: RunnerInvocationId,
    message: RunnerSessionMessage,
    sequence: int,
    payload: tuple[bytes, ...] = (),
) -> RunnerSessionFrame:
    return RunnerSessionFrame(message, sequence, fixture.binding, invocation, payload)


def _drive_through_started(
    fixture: _StoreFixture, session: CoreRunnerSession, invocation: RunnerInvocationId
) -> None:
    session.accept(
        _frame(fixture, invocation, RunnerSessionMessage.INVOCATION_OFFER, 1)
    )
    session.accept(
        _frame(fixture, invocation, RunnerSessionMessage.READY, 2, _ready_payload())
    )
    session.accept(
        _frame(fixture, invocation, RunnerSessionMessage.STARTED, 3, (b"\x00" * 8,))
    )


def _drive_through_released(
    fixture: _StoreFixture,
    session: CoreRunnerSession,
    invocation: RunnerInvocationId,
    envelope: RunnerTerminalEvidenceEnvelope,
    evidence_hash: RunnerTerminalEvidenceHash,
) -> RunnerSessionFrame:
    session.accept(
        _frame(
            fixture,
            invocation,
            RunnerSessionMessage.TERMINAL_AVAILABLE,
            4,
            (evidence_hash.value.encode("ascii"),),
        )
    )
    ack = session.accept_terminal_record(
        _frame(
            fixture,
            invocation,
            RunnerSessionMessage.TERMINAL_RECORD,
            5,
            (encode_runner_terminal_evidence_record(envelope),),
        )
    )
    tombstone = RunnerTerminalEvidenceAckTombstone(
        fixture.binding, invocation, evidence_hash
    )
    session.accept(
        _frame(
            fixture,
            invocation,
            RunnerSessionMessage.ACK_TOMBSTONE,
            6,
            (encode_runner_terminal_evidence_record(tombstone),),
        )
    )
    session.accept(
        _frame(
            fixture,
            invocation,
            RunnerSessionMessage.RELEASED,
            7,
            (evidence_hash.value.encode("ascii"),),
        )
    )
    return ack


def test_resume_after_a_core_crash_before_started_re_arms_idempotently(
    tmp_path: Path,
) -> None:
    """Crash cut C6: Core dies right after STARTED, the runner invocation is live.

    A fresh `CoreRunnerSession` replays the full handshake from OFFER with
    the same invocation; the durable store's own idempotent `arm` recognizes
    the retry instead of refusing it as a foreign second invocation.
    """
    fixture = _store_fixture(tmp_path, "runner-resume/c6")
    try:
        first = _session_for(fixture, fixture.invocation)
        _drive_through_started(fixture, first, fixture.invocation)
        # Core dies here -- drop `first` without ever driving it further.

        second = _session_for(fixture, fixture.invocation)
        _drive_through_started(fixture, second, fixture.invocation)
        envelope = RunnerTerminalEvidenceEnvelope(
            fixture.binding,
            fixture.invocation,
            RunnerProviderResult(AgentExecutionResult(b"resume-c6-result")),
        )
        evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
        ack = _drive_through_released(
            fixture, second, fixture.invocation, envelope, evidence_hash
        )

        assert ack.message is RunnerSessionMessage.ACK
        durable = fixture.store.load(fixture.execution.attempt_id)
        assert durable.runner_invocation_id == fixture.invocation
        assert (
            durable.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
        )
        assert durable.runner_terminal_evidence_hash == evidence_hash
    finally:
        fixture.runtime.close()


def test_resume_after_a_core_crash_between_commit_and_ack_recommits_idempotently(
    tmp_path: Path,
) -> None:
    """Crash cut C4: Core dies right after committing, before answering ACK."""
    fixture = _store_fixture(tmp_path, "runner-resume/c4")
    try:
        envelope = RunnerTerminalEvidenceEnvelope(
            fixture.binding,
            fixture.invocation,
            RunnerProviderResult(AgentExecutionResult(b"resume-c4-result")),
        )
        evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)

        first = _session_for(fixture, fixture.invocation)
        _drive_through_started(fixture, first, fixture.invocation)
        first.accept(
            _frame(
                fixture,
                fixture.invocation,
                RunnerSessionMessage.TERMINAL_AVAILABLE,
                4,
                (evidence_hash.value.encode("ascii"),),
            )
        )
        # The commit durably lands here; Core dies before the ACK it computed
        # ever reaches the wire -- the response is simply discarded.
        first.accept_terminal_record(
            _frame(
                fixture,
                fixture.invocation,
                RunnerSessionMessage.TERMINAL_RECORD,
                5,
                (encode_runner_terminal_evidence_record(envelope),),
            )
        )
        after_commit = fixture.store.load(fixture.execution.attempt_id)
        assert (
            after_commit.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.CORE_COMMITTED
        )

        second = _session_for(fixture, fixture.invocation)
        _drive_through_started(fixture, second, fixture.invocation)
        ack = _drive_through_released(
            fixture, second, fixture.invocation, envelope, evidence_hash
        )

        assert ack.message is RunnerSessionMessage.ACK
        durable = fixture.store.load(fixture.execution.attempt_id)
        assert (
            durable.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
        )
        assert durable.runner_terminal_evidence_hash == evidence_hash
    finally:
        fixture.runtime.close()


def test_a_retry_after_full_release_refuses_instead_of_repeating_the_work(
    tmp_path: Path,
) -> None:
    """A closed attempt offered again (a new invocation, nothing retained) refuses.

    Once RELEASED, the durable attempt already carries its one accepted
    invocation; a candidate that starts over from a blank journal (its own
    record long since released) mints a fresh invocation and Core's own
    durable arm refuses it -- closed and named, no residue, no re-placement
    of the underlying work.
    """
    fixture = _store_fixture(tmp_path, "runner-resume/finalize-retry")
    try:
        envelope = RunnerTerminalEvidenceEnvelope(
            fixture.binding,
            fixture.invocation,
            RunnerProviderResult(AgentExecutionResult(b"resume-finalize-result")),
        )
        evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)

        first = _session_for(fixture, fixture.invocation)
        _drive_through_started(fixture, first, fixture.invocation)
        _drive_through_released(
            fixture, first, fixture.invocation, envelope, evidence_hash
        )
        before_retry = fixture.store.load(fixture.execution.attempt_id)
        assert (
            before_retry.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
        )

        foreign_invocation = RunnerInvocationId("C" * 43)
        retry = _session_for(fixture, foreign_invocation)
        retry.accept(
            _frame(
                fixture, foreign_invocation, RunnerSessionMessage.INVOCATION_OFFER, 1
            )
        )

        with pytest.raises(RunnerSessionRefusal, match="runner-arm-conflict"):
            retry.accept(
                _frame(
                    fixture,
                    foreign_invocation,
                    RunnerSessionMessage.READY,
                    2,
                    _ready_payload(),
                )
            )

        after_retry = fixture.store.load(fixture.execution.attempt_id)
        assert after_retry == before_retry
    finally:
        fixture.runtime.close()
