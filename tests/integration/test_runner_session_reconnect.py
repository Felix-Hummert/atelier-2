"""A Core death while a provider child runs costs neither the child nor its result.

Both ends are real. The candidate is `run_candidate_session` in its own
interpreter, supervising a real child through the real launcher and its real
attested Landlock grants. The Core side is the production `CoreRunnerSession`
over a real durable attempt in a real store, driven by the production transport
loop (`drive_until_released_or_dropped`), accepting one Runner connection per
Core lifetime -- the shape `adapters/dbos/runtime.py` gives a Serve whose
`maximum_connection_attempts` is one.

Core dying is modelled as a Serve process death, not a dropped socket: the
drive loop's own `on_started` hook raises, the connection closes with frames
still owed, and then the runtime itself is closed and opened again over the
same files. The second lifetime therefore starts *cold* -- no cached session,
no remembered sequence -- and what has to recognize the Runner's replay is the
durable attempt's own idempotency. That is the production shape, and a stronger
claim than a warm cache replaying itself.

Nothing here waits on a guess. The child blocks until this test releases it, so
"the child was still running" is observed rather than inferred; the released
bytes are the test's own, so the evidence Core commits is traceable to that one
child; and the Runner's clock is injected, so the proof about a span running
out spends no wall clock at all. Every timeout below is a deadlock ceiling.

The fixed free-runner candidate program's own two behaviours are not the
subject here and stay proven by `test_runner_session_wire.py`; this witness
substitutes the program the child runs, never the launcher that confines it.
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.runner_session_core import DbosRunnerSessionCore
from atelier2.adapters.dbos.runtime import DbosRuntime
from atelier2.adapters.free_runner_executor import FreeRunnerHoldJob
from atelier2.adapters.runner_cli_pins import runner_executor_cli_pin
from atelier2.adapters.runner_core_transport import (
    RunnerSessionAdvancer,
    drive_until_released_or_dropped,
)
from atelier2.application.run_runner_session import (
    CoreRunnerSession,
    encode_runner_prepare_payload,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptState,
    RunnerEvidenceAcceptancePhase,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerProviderResult,
    RunnerTerminalEvidenceEnvelope,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.executions import AgentAttemptExecution
from atelier2.contracts.runner_manifests import (
    RunnerManifestV1,
    encode_runner_manifest,
    runner_manifest_id,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame
from atelier2.contracts.runner_terminal_evidence_codec import (
    decode_runner_terminal_evidence_record,
)
from atelier2.runner.session import InvocationSpanExhausted
from tests.integration.test_runner_session_wire import (
    _STUBBED_PROCESS_LIMIT,
    _denied_identity_directory,
    _host_manifest,
)
from tests.scenarios.runners import (
    free_runner_core_runtime,
    prepared_free_runner_attempt,
)

# The child this witness runs instead of the fixed candidate program: it waits
# for a file only this test writes, then answers with exactly what that file
# says. Waiting on cue is what makes "the child was still alive" an
# observation; answering with the test's own bytes is what ties the evidence
# Core commits to this one child rather than to any child. It ignores SIGTERM
# for the same reason `FreeRunnerHoldJob` does -- only a real escalation to
# KILL can end it, so a reap that stopped at TERM would fail rather than pass.
_GATED_CHILD_PROGRAM = (
    "import signal, sys, time\n"
    "from pathlib import Path\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "release = Path(sys.argv[1])\n"
    "while not release.is_file():\n"
    "    time.sleep(0.01)\n"
    "sys.stdout.write(release.read_text(encoding='ascii'))\n"
)

# Runs `run_candidate_session` the way the deployed image does -- dialling Core
# afresh for every connection -- while recording each provider child the
# session really starts. The ledger is what makes "the child survived" a fact
# instead of an inference: a candidate that reaped its child and launched a
# second one would deliver evidence that looks the same and write a second
# line here.
_RECONNECTING_CANDIDATE_DRIVER = """
import ctypes
import socket
import sys
from pathlib import Path

_PR_SET_NO_NEW_PRIVS = 38

(
    core_port, attempt_id, request_hash, generation_id, manifest_id,
    invocation_id, manifest_path, identity, journal_directory, process_limit,
    workspace_directory, child_ledger, child_program, release_file, clock_kind,
) = sys.argv[1:16]

from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
)
from atelier2.contracts.agents import AgentExecutionRequestHash
from atelier2.runner import session as session_module
from atelier2.runner.session import (
    REAL_TIME,
    CandidateScenario,
    run_candidate_session,
)

libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), "prctl(NO_NEW_PRIVS) failed")

session_module._pid_limit = lambda: int(process_limit)
session_module._runner_workspace_directory = lambda: Path(workspace_directory)

_start_runner_child = session_module.start_runner_child


def _gated_start_runner_child(arguments, *rest, **keywords):
    # Declared witness data: the program is this test's, the launcher that
    # confines it is production's and untouched -- same attested grants, same
    # session, same descriptor discipline.
    del arguments
    child = _start_runner_child(
        (sys.executable, "-c", child_program, release_file), *rest, **keywords
    )
    with open(child_ledger, "a", encoding="ascii") as ledger:
        print(child.pid, file=ledger)
    return child


session_module.start_runner_child = _gated_start_runner_child


# A clock whose only cost is the arithmetic: waiting spends the invocation's
# span without spending the wall clock, so a proof about what this container
# does when that span runs out takes no longer than the exchange before it.
class _SpentClock:
    def __init__(self):
        self._now = 0.0

    def monotonic(self):
        return self._now

    def sleep(self, seconds):
        self._now += seconds


def _connect_to_core(seconds_left):
    return socket.create_connection(("127.0.0.1", int(core_port)), seconds_left)


run_candidate_session(
    _connect_to_core,
    RunnerGenerationBinding(
        AgentAttemptId(attempt_id),
        AgentExecutionRequestHash(request_hash),
        RunnerGenerationId(generation_id),
        RunnerManifestId(manifest_id),
    ),
    RunnerInvocationId(invocation_id),
    CandidateScenario.SUCCESS,
    Path(manifest_path),
    Path(identity),
    Path(journal_directory),
    clock=REAL_TIME if clock_kind == "real" else _SpentClock(),
)
"""

# Bounds every blocking wait in this module. Nothing here is meant to reach it:
# a candidate that never connects, never reconnects, or never gives up must
# fail these tests loudly rather than hang the suite.
_DEADLOCK_CEILING_SECONDS = 60.0

# The span the attested manifest declares. Neither test spends it: the first
# never runs out of it, and the second spends it on a clock the candidate owns.
# It is large enough that a loaded host cannot make it the reason a proof fails.
_SPAN_MILLISECONDS = 30_000

# The one writable surface the attested manifest grants the child
# (`test_runner_session_wire._host_child_grants`). Putting the gate file here
# is what keeps this witness from widening a single grant to make itself work.
_HOST_SCRATCH = "/tmp"

_CANDIDATE_INVOCATION = RunnerInvocationId("B" * 43)
_CANDIDATE_GENERATION = RunnerGenerationId("A" * 43)


class _CoreDied(Exception):
    """This Core process is gone the moment the candidate reports its child."""


@dataclass
class _RecordingCore:
    """The production Core session, plus the terminal record it was handed.

    A recorder, never a stand-in: every frame still reaches the real
    `CoreRunnerSession` over the real durable store. It keeps only the one
    protocol frame whose content these proofs turn on -- the evidence bytes
    the Runner delivered -- which no durable field carries back verbatim.
    """

    session: CoreRunnerSession
    terminal_records: list[bytes] = field(default_factory=list)

    def accept(self, frame: RunnerSessionFrame) -> RunnerSessionFrame | None:
        return self.session.accept(frame)

    def accept_terminal_record(
        self, frame: RunnerSessionFrame
    ) -> RunnerSessionFrame | None:
        self.terminal_records.append(frame.payload[0])
        return self.session.accept_terminal_record(frame)

    def cancel(self) -> RunnerSessionFrame:
        return self.session.cancel()


@dataclass
class _ReconnectFixture:
    """One durable free-runner attempt and the Core lifetimes that serve it."""

    core_root: Path
    runtime: DbosRuntime
    store: DbosAgentAttemptStore
    execution: AgentAttemptExecution
    binding: RunnerGenerationBinding
    manifest: RunnerManifestV1
    manifest_path: Path
    identity: Path
    journal_directory: Path
    workspace_directory: Path
    child_ledger: Path
    release_file: Path
    prepare_payload: tuple[bytes, ...]
    auth_reference: str

    def core_session(self) -> _RecordingCore:
        """The session one Core lifetime drives, cold, over the durable attempt.

        Called once per lifetime and never carried across one: a restarted
        Serve keeps nothing of the session its predecessor held, which is
        exactly the recovery this file exists to prove.
        """
        return _RecordingCore(
            CoreRunnerSession(
                self.binding,
                DbosRunnerSessionCore(
                    self.execution, self.store, "runner-reconnect-cancel"
                ),
                self.prepare_payload,
                self.manifest,
                self.auth_reference,
                _CANDIDATE_INVOCATION,
                runner_executor_cli_pin(self.manifest),
            )
        )

    def restart_core(self) -> None:
        """Close this Core's runtime and open a new one over the same files.

        That is what a Serve restart leaves behind: the durable attempt, its
        lease material and the Runner's journal volume all survive, and every
        in-memory session does not.
        """
        self.runtime.close()
        self.runtime = free_runner_core_runtime(self.core_root)
        self.store = DbosAgentAttemptStore(self.runtime.engine)


@contextmanager
def _reconnect_fixture(tmp_path: Path) -> Iterator[_ReconnectFixture]:
    manifest = _host_manifest(total_attempt_milliseconds=_SPAN_MILLISECONDS)
    manifest_path = tmp_path / "manifest"
    manifest_path.write_bytes(encode_runner_manifest(manifest))
    workspace_directory = tmp_path / "runner-workspace"
    workspace_directory.mkdir()
    core_root = tmp_path / "core"
    prepared = prepared_free_runner_attempt(
        core_root,
        "runner-reconnect/one",
        FreeRunnerHoldJob(manifest.total_attempt_milliseconds / 1000),
    )
    gate_root = Path(tempfile.mkdtemp(dir=_HOST_SCRATCH))
    fixture = _ReconnectFixture(
        core_root,
        prepared.runtime,
        prepared.store,
        prepared.execution,
        RunnerGenerationBinding(
            prepared.execution.attempt_id,
            prepared.execution.request.request_hash,
            _CANDIDATE_GENERATION,
            runner_manifest_id(manifest),
        ),
        manifest,
        manifest_path,
        _denied_identity_directory(tmp_path),
        tmp_path / "journal",
        workspace_directory,
        tmp_path / "started-children",
        gate_root / "release",
        encode_runner_prepare_payload(
            prepared.execution.request, prepared.auth_reference
        ),
        prepared.auth_reference,
    )
    # Built before the store is touched at all, so a durable step that refuses
    # still leaves this process's one runtime binding released below.
    try:
        fixture.store.prepare(fixture.execution)
        fixture.store.bind_runner_generation(fixture.execution, fixture.binding)
        yield fixture
    finally:
        shutil.rmtree(gate_root, ignore_errors=True)
        fixture.runtime.close()


def _spawn_candidate(
    fixture: _ReconnectFixture, core_port: int, clock_kind: str
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        (
            sys.executable,
            "-c",
            _RECONNECTING_CANDIDATE_DRIVER,
            str(core_port),
            fixture.binding.attempt_id.value,
            fixture.binding.request_hash.value,
            fixture.binding.generation_id.value,
            fixture.binding.manifest_id.value,
            _CANDIDATE_INVOCATION.value,
            str(fixture.manifest_path),
            str(fixture.identity),
            str(fixture.journal_directory),
            str(_STUBBED_PROCESS_LIMIT),
            str(fixture.workspace_directory),
            str(fixture.child_ledger),
            _GATED_CHILD_PROGRAM,
            str(fixture.release_file),
            clock_kind,
        ),
        # Captured rather than inherited so a proof can say *how* the
        # invocation ended, not merely that it did: this session declares one
        # named ending for a span that ran out, and an ending that arrived by
        # some other accident would otherwise look identical here.
        stderr=subprocess.PIPE,
        close_fds=True,
    )


def _serve_one_connection(
    listener: socket.socket,
    core: RunnerSessionAdvancer,
    on_started: Callable[[RunnerSessionAdvancer], RunnerSessionFrame | None]
    | None = None,
) -> bool:
    """One Core lifetime's whole session-listening life: accept exactly one
    Runner and drive it, the shape `accept_and_drive_session` gives a Serve
    whose `maximum_connection_attempts` is one. The listener outlives the
    lifetime because the port a deployment serves on outlives it too."""
    connection, _peer = listener.accept()
    with connection:
        return drive_until_released_or_dropped(connection, core, on_started)


def _launched_child_pids(child_ledger: Path) -> tuple[int, ...]:
    """Every provider process this invocation really started, in order."""
    if not child_ledger.is_file():
        return ()
    return tuple(int(line) for line in child_ledger.read_text(encoding="ascii").split())


def _child_is_running(pid: int) -> bool:
    """Whether that process exists and has not already finished.

    A child that exited but has not been reaped still answers a signal probe,
    so its state field is what tells "still working" from "already done" --
    which is the whole difference these proofs turn on.
    """
    try:
        status = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    except FileNotFoundError:
        return False
    return status.rpartition(")")[2].split()[0] not in {"Z", "X"}


def _die_once_the_child_is_reported(
    fixture: _ReconnectFixture, child_was_running: list[bool]
) -> Callable[[RunnerSessionAdvancer], RunnerSessionFrame | None]:
    """End this Core lifetime at STARTED, recording what it saw as it went."""

    def _hook(session: RunnerSessionAdvancer) -> RunnerSessionFrame | None:
        del session
        launched = _launched_child_pids(fixture.child_ledger)
        child_was_running.append(len(launched) == 1 and _child_is_running(launched[0]))
        raise _CoreDied

    return _hook


def test_a_core_restart_while_the_child_runs_delivers_that_childs_own_evidence(
    tmp_path: Path,
) -> None:
    """The whole wish of `#529`, against a Core that really restarted.

    The first Core process dies with the provider child mid-flight. The second
    starts cold over the same durable attempt and accepts the one Runner that
    reconnects. That Runner must still be holding its child: it replays the
    handshake from OFFER, waits the same child out, and hands the new Core the
    result it paid for. Exactly one provider process for the invocation, one
    accepted invocation, one committed record, one acknowledgement -- and the
    bytes Core commits are the ones this test released into that child.
    """
    # A JSON document, because the node this attempt belongs to declares an
    # output schema and the commit holds the child's answer against it: bytes
    # that could never satisfy it would end the attempt FAILED and prove
    # nothing about which child produced them.
    released = b'{"answer": "the child that outlived its Core"}'

    with _reconnect_fixture(tmp_path) as fixture:
        child_was_running: list[bool] = []
        with socket.create_server(("127.0.0.1", 0)) as listener:
            listener.settimeout(_DEADLOCK_CEILING_SECONDS)
            candidate = _spawn_candidate(
                fixture, listener.getsockname()[1], clock_kind="real"
            )
            try:
                dying = fixture.core_session()
                with pytest.raises(_CoreDied):
                    _serve_one_connection(
                        listener,
                        dying,
                        _die_once_the_child_is_reported(fixture, child_was_running),
                    )

                fixture.restart_core()
                survivors = _launched_child_pids(fixture.child_ledger)
                held_across_the_restart = len(survivors) == 1 and _child_is_running(
                    survivors[0]
                )
                fixture.release_file.write_bytes(released)
                resumed = fixture.core_session()
                reached_released = _serve_one_connection(listener, resumed)
                candidate.communicate(timeout=_DEADLOCK_CEILING_SECONDS)
                returncode = candidate.returncode
            finally:
                candidate.kill()
                candidate.wait(timeout=_DEADLOCK_CEILING_SECONDS)

        assert child_was_running == [True]
        assert held_across_the_restart
        assert reached_released
        assert returncode == 0
        assert _launched_child_pids(fixture.child_ledger) == survivors

        assert dying.terminal_records == []
        assert len(resumed.terminal_records) == 1
        record = decode_runner_terminal_evidence_record(resumed.terminal_records[0])
        assert isinstance(record, RunnerTerminalEvidenceEnvelope)
        assert record.evidence == RunnerProviderResult(AgentExecutionResult(released))

        durable = fixture.store.load(fixture.execution.attempt_id)
        assert durable.runner_invocation_id == _CANDIDATE_INVOCATION
        assert durable.attempt_ordinal == 1
        assert durable.state is AgentAttemptState.SUCCEEDED
        assert (
            durable.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.ACKNOWLEDGED
        )
        assert not (fixture.journal_directory / "terminal-record").exists()


def test_a_core_that_never_returns_ends_the_invocation_with_nothing_left_running(
    tmp_path: Path,
) -> None:
    """The other half: patience is bounded, and giving up leaves no residue.

    The listener closes with the dying Core, so every reconnect is refused.
    Within the span its manifest declares -- spent here on a clock the
    candidate owns, never on this suite's wall clock -- it must stop trying
    and end: reaping the child it was holding, which ignores SIGTERM on
    purpose, so only a real escalation to KILL passes. The durable attempt is
    left exactly as armed as it was, with no evidence invented for it.
    """
    with _reconnect_fixture(tmp_path) as fixture:
        child_was_running: list[bool] = []
        listener = socket.create_server(("127.0.0.1", 0))
        listener.settimeout(_DEADLOCK_CEILING_SECONDS)
        candidate = _spawn_candidate(
            fixture, listener.getsockname()[1], clock_kind="spent"
        )
        try:
            with listener, pytest.raises(_CoreDied):
                _serve_one_connection(
                    listener,
                    fixture.core_session(),
                    _die_once_the_child_is_reported(fixture, child_was_running),
                )
            _stdout, stderr = candidate.communicate(timeout=_DEADLOCK_CEILING_SECONDS)
            returncode = candidate.returncode
        finally:
            candidate.kill()
            candidate.wait(timeout=_DEADLOCK_CEILING_SECONDS)

        assert child_was_running == [True]
        assert returncode != 0
        assert InvocationSpanExhausted.__name__ in stderr.decode("utf-8", "replace")
        launched = _launched_child_pids(fixture.child_ledger)
        assert len(launched) == 1
        assert not _child_is_running(launched[0])
        assert not (fixture.journal_directory / "terminal-record").exists()

        durable = fixture.store.load(fixture.execution.attempt_id)
        assert durable.runner_invocation_id == _CANDIDATE_INVOCATION
        assert (
            durable.runner_evidence_acceptance_phase
            is RunnerEvidenceAcceptancePhase.NONE
        )
