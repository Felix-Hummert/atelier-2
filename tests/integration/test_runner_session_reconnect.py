"""A Core death while a provider child runs costs neither the child nor its result.

Both ends are real: the candidate is `run_candidate_session` in its own
interpreter, driving a real provider child, and the Core side is the real
`CoreRunnerSession` behind the production transport loop
(`drive_until_released_or_dropped`). Only the durable store is faked, exactly
as `test_runner_session_wire.py` does -- the subject here is what a candidate
does when the connection under it dies, not what Core writes.

Core dying is modelled the way it really happens: the drive loop's own
`on_started` hook raises, so the Core side stops mid-exchange and its socket
closes with frames still owed. Nothing is signalled to the candidate, which is
the point -- it only ever sees the connection go away.

The candidate here is the one that really dials Core again, so its channel
source is a loopback connect rather than the single inherited descriptor every
other candidate driver is handed.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from atelier2.adapters.free_runner_executor import (
    FreeRunnerHoldJob,
    encode_free_runner_job,
)
from atelier2.adapters.runner_core_transport import (
    RunnerSessionAdvancer,
    drive_until_released_or_dropped,
)
from atelier2.contracts.agent_attempts import RunnerProviderResult
from atelier2.contracts.runner_sessions import RunnerSessionFrame
from atelier2.runner.session import CandidateScenario
from tests.integration.test_runner_session_wire import (
    _STUBBED_PROCESS_LIMIT,
    _prepared_session,
    _PreparedSession,
)

# Runs `run_candidate_session` the way the deployed image does -- one channel
# per connect, dialled fresh -- and records every provider child the session
# really starts. The ledger is what makes "the child survived" a fact instead
# of an inference: a candidate that reaped its child and launched a second one
# would deliver evidence that looks the same and write a second line here.
_RECONNECTING_CANDIDATE_DRIVER = """
import ctypes
import socket
import sys
from pathlib import Path

_PR_SET_NO_NEW_PRIVS = 38

(
    core_port, attempt_id, request_hash, generation_id, manifest_id,
    invocation_id, scenario_value, manifest_path, identity, journal_directory,
    process_limit, workspace_directory, child_ledger,
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

session_module._pid_limit = lambda: int(process_limit)
session_module._runner_workspace_directory = lambda: Path(workspace_directory)

_start_runner_child = session_module.start_runner_child


def _recording_start_runner_child(*arguments, **keywords):
    child = _start_runner_child(*arguments, **keywords)
    with open(child_ledger, "a", encoding="ascii") as ledger:
        print(child.pid, file=ledger)
    return child


session_module.start_runner_child = _recording_start_runner_child


def _connect_to_core():
    return socket.create_connection(("127.0.0.1", int(core_port)), 5)


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

# Bounds every blocking wait in this module: a candidate that never connects,
# never reconnects, or never gives up must fail these tests loudly rather than
# hang the suite. It is far above every span the manifests below declare.
_WITNESS_PATIENCE_SECONDS = 30.0


class _CoreDied(Exception):
    """The Core process is gone the moment the candidate reports its child."""


def _die_at_started(session: RunnerSessionAdvancer) -> RunnerSessionFrame | None:
    del session
    raise _CoreDied


def _spawn_reconnecting_candidate(
    prepared: _PreparedSession, core_port: int, child_ledger: Path
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        (
            sys.executable,
            "-c",
            _RECONNECTING_CANDIDATE_DRIVER,
            str(core_port),
            prepared.binding.attempt_id.value,
            prepared.binding.request_hash.value,
            prepared.binding.generation_id.value,
            prepared.binding.manifest_id.value,
            prepared.invocation.value,
            CandidateScenario.SUCCESS.value,
            str(prepared.manifest_path),
            str(prepared.identity),
            str(prepared.journal_directory),
            str(_STUBBED_PROCESS_LIMIT),
            str(prepared.workspace_directory),
            str(child_ledger),
        ),
        close_fds=True,
    )


def _launched_child_pids(child_ledger: Path) -> tuple[int, ...]:
    """Every provider process the candidate really started, in order."""
    if not child_ledger.is_file():
        return ()
    return tuple(int(line) for line in child_ledger.read_text(encoding="ascii").split())


def _process_is_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _hold_job(seconds: float) -> bytes:
    return encode_free_runner_job(FreeRunnerHoldJob(seconds))


def test_a_core_death_while_the_child_runs_still_delivers_that_childs_evidence(
    tmp_path: Path,
) -> None:
    """The whole wish of `#529`, end to end.

    Core dies with the provider child mid-flight and comes back on a second
    connection. The candidate must not have reaped that child: it replays the
    handshake from OFFER, waits the same child out, and hands Core the real
    result it paid for -- one arm, one commit, one acknowledgement, and
    exactly one provider process for the whole invocation.
    """
    prepared = _prepared_session(
        tmp_path, job_bytes=_hold_job(0.5), total_attempt_milliseconds=8_000
    )
    child_ledger = tmp_path / "started-children"

    with socket.create_server(("127.0.0.1", 0)) as listener:
        listener.settimeout(_WITNESS_PATIENCE_SECONDS)
        candidate = _spawn_reconnecting_candidate(
            prepared, listener.getsockname()[1], child_ledger
        )
        try:
            dying, _peer = listener.accept()
            with dying, pytest.raises(_CoreDied):
                drive_until_released_or_dropped(
                    dying, prepared.core_session, _die_at_started
                )

            resumed, _peer = listener.accept()
            with resumed:
                released = drive_until_released_or_dropped(
                    resumed, prepared.core_session
                )
            returncode = candidate.wait(timeout=_WITNESS_PATIENCE_SECONDS)
        finally:
            candidate.kill()
            candidate.wait(timeout=_WITNESS_PATIENCE_SECONDS)

    assert released
    assert returncode == 0
    assert len(_launched_child_pids(child_ledger)) == 1
    assert (
        prepared.core.armed,
        prepared.core.committed,
        prepared.core.acknowledged,
    ) == (
        1,
        1,
        1,
    )
    committed = prepared.core.committed_envelope
    assert committed is not None
    assert isinstance(committed.evidence, RunnerProviderResult)
    assert not (prepared.journal_directory / "terminal-record").exists()


def test_a_core_that_never_returns_ends_the_invocation_with_nothing_left_running(
    tmp_path: Path,
) -> None:
    """The other half: patience is bounded, and giving up leaves no residue.

    The listener closes with the dying Core, so every reconnect this candidate
    tries is refused. Within the attempt span its manifest declares it must
    stop trying and end -- reaping the child it was holding, which ignores
    SIGTERM on purpose, so only a real escalation to KILL can make this pass.
    """
    prepared = _prepared_session(
        tmp_path, job_bytes=_hold_job(5.0), total_attempt_milliseconds=2_000
    )
    child_ledger = tmp_path / "started-children"

    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(_WITNESS_PATIENCE_SECONDS)
    candidate = _spawn_reconnecting_candidate(
        prepared, listener.getsockname()[1], child_ledger
    )
    try:
        with listener:
            dying, _peer = listener.accept()
            with dying, pytest.raises(_CoreDied):
                drive_until_released_or_dropped(
                    dying, prepared.core_session, _die_at_started
                )
        returncode = candidate.wait(timeout=_WITNESS_PATIENCE_SECONDS)
    finally:
        candidate.kill()
        candidate.wait(timeout=_WITNESS_PATIENCE_SECONDS)

    assert returncode != 0
    launched = _launched_child_pids(child_ledger)
    assert len(launched) == 1
    assert not _process_is_alive(launched[0])
    assert not (prepared.journal_directory / "terminal-record").exists()
