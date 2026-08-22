"""The candidate Runner's one-shot session state machine, off the wire.

Everything here drives OFFER through RELEASED against a narrow byte channel —
no socket, TLS, or DNS import crosses this boundary. `__main__` owns the
socket connect, the TLS handshake, and the peer-certificate fence; once that
authenticated channel exists, this module owns every frame that flows across
it, the child process it supervises, and the journal it publishes to.
"""

from __future__ import annotations

import errno
import hashlib
import os
import struct
import subprocess
import sys
import time
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from atelier2.adapters.free_runner_executor import (
    FreeRunnerAuthorizationResolver,
    refuse_unbound_runner_a_request,
)
from atelier2.adapters.runner_child import (
    REQUIRED_LANDLOCK_ABI,
    LandlockUnavailable,
    landlock_kernel_abi,
    reap_cancelled_runner_child,
    start_runner_child,
)
from atelier2.adapters.runner_journal import RunnerJournal
from atelier2.application.run_runner_session import (
    CROSSING_CANCEL_REFUSAL_CODE,
    decode_runner_prepare_payload,
    require_matching_evidence_hash,
    require_ready_matches_manifest,
)
from atelier2.contracts.agent_attempts import (
    RunnerCancellation,
    RunnerCancellationObservation,
    RunnerGenerationBinding,
    RunnerInvocationId,
    RunnerProviderResult,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import AgentExecutionResult
from atelier2.contracts.runner_manifests import (
    RunnerManifestV1,
    decode_runner_manifest,
    runner_manifest_id,
)
from atelier2.contracts.runner_session_codec import (
    PREPARE_AUTH_REFERENCE_FIELD,
    decode_runner_session_frame,
    encode_runner_session_frame,
    runner_session_body_length,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame, RunnerSessionMessage
from atelier2.contracts.runner_terminal_evidence_codec import (
    encode_runner_terminal_evidence_record,
)

_CONTROL_POLL_SECONDS = 0.05


class CandidateScenario(StrEnum):
    """The session ending the witness Core declares in the public bootstrap.

    The A candidate exists to prove two endings — a bounded child result and a
    cancel that reaps the child — so the child it launches is a declared,
    closed candidate fact rather than an implicit test key.
    """

    SUCCESS = "success"
    CANCEL = "cancel"


class RunnerFrameChannel(Protocol):
    """The narrow byte boundary the candidate session drives.

    Anything that can recv, sendall, and settimeout satisfies it — a real TLS
    socket in production, a plain `socket.socketpair()` half in a test. The
    session state machine never imports socket or ssl itself.
    """

    def recv(self, buffer_size: int, /) -> bytes: ...

    def sendall(self, data: bytes, /) -> None: ...

    def settimeout(self, value: float | None, /) -> None: ...


class _CoreFrameFence:
    """Read Core frames without losing bytes and refuse any foreign frame.

    Bytes already received survive a control-poll timeout, so a CANCEL arriving
    while the poll fires mid-frame is completed on the next poll instead of
    desynchronizing the stream. Every inbound frame must carry the session's
    exact binding, invocation, and next Core sequence — the fence Core holds
    against the runner, held symmetrically against Core.
    """

    def __init__(
        self,
        channel: RunnerFrameChannel,
        binding: RunnerGenerationBinding,
        invocation: RunnerInvocationId,
    ) -> None:
        self._channel = channel
        self._binding = binding
        self._invocation = invocation
        self._buffer = bytearray()
        self._next_core_sequence = 1

    def read_frame(self, timeout: float | None = None) -> RunnerSessionFrame:
        self._channel.settimeout(timeout)
        try:
            self._buffer_exactly(4)
            frame_length = 4 + runner_session_body_length(bytes(self._buffer[:4]))
            self._buffer_exactly(frame_length)
        finally:
            self._channel.settimeout(None)
        wire = bytes(self._buffer[:frame_length])
        del self._buffer[:frame_length]
        frame = decode_runner_session_frame(wire)
        if frame.binding != self._binding or frame.invocation_id != self._invocation:
            raise RuntimeError("runner-session-binding-mismatch")
        if frame.sequence != self._next_core_sequence:
            raise RuntimeError("runner-session-sequence-mismatch")
        self._next_core_sequence += 1
        return frame

    def _buffer_exactly(self, length: int) -> None:
        while len(self._buffer) < length:
            piece = self._channel.recv(length - len(self._buffer))
            if not piece:
                raise ConnectionError("Core closed the candidate session")
            self._buffer.extend(piece)


def _write_frame(channel: RunnerFrameChannel, frame: RunnerSessionFrame) -> None:
    channel.sendall(encode_runner_session_frame(frame))


def _frame(
    message: RunnerSessionMessage,
    sequence: int,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    payload: tuple[bytes, ...] = (),
) -> RunnerSessionFrame:
    return RunnerSessionFrame(message, sequence, binding, invocation, payload)


def _child_command(
    scenario: CandidateScenario, manifest: RunnerManifestV1
) -> tuple[str, ...]:
    if scenario is CandidateScenario.CANCEL:
        hold_seconds = manifest.total_attempt_milliseconds / 1000
        hold = (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            f"time.sleep({hold_seconds})"
        )
        return (sys.executable, "-c", hold)
    return (
        sys.executable,
        "-c",
        "import json; print(json.dumps('runner candidate'))",
    )


def _reap_child(
    child: subprocess.Popen[bytes], manifest: RunnerManifestV1
) -> RunnerCancellationObservation:
    return reap_cancelled_runner_child(
        child,
        manifest.terminate_grace_milliseconds / 1000,
        manifest.reap_deadline_milliseconds / 1000,
    )


def _control_or_child_exit(
    fence: _CoreFrameFence,
    child: subprocess.Popen[bytes],
    manifest: RunnerManifestV1,
) -> RunnerSessionFrame | None:
    deadline = time.monotonic() + manifest.total_attempt_milliseconds / 1000
    while child.poll() is None and time.monotonic() < deadline:
        try:
            return fence.read_frame(timeout=_CONTROL_POLL_SECONDS)
        except TimeoutError:
            continue
    return None


def _status_field(name: str) -> str:
    for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
        key, separator, value = line.partition(":")
        if separator and key == name:
            return value.strip()
    raise RuntimeError(f"runner status {name} is missing")


def _pid_limit() -> int:
    candidates = [Path("/sys/fs/cgroup/pids.max"), Path("/sys/fs/cgroup/pids/pids.max")]
    cgroup = Path("/proc/self/cgroup")
    if cgroup.is_file():
        for line in cgroup.read_text(encoding="ascii").splitlines():
            relative = line.split(":", 2)[-1].lstrip("/")
            if relative:
                candidates.append(Path("/sys/fs/cgroup") / relative / "pids.max")
    for path in candidates:
        if path.is_file():
            raw = path.read_text(encoding="ascii").strip()
            if raw != "max":
                return int(raw)
    raise RuntimeError("runner pid limit is unreadable")


def _read_only_root() -> bool:
    probe = Path("/.runner-root-write-probe")
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC)
    except OSError as error:
        return error.errno in {errno.EROFS, errno.EACCES, errno.EPERM}
    os.close(descriptor)
    probe.unlink()
    return False


def _identity_mount_denied(identity: Path) -> bool:
    probe = identity / ".write-probe"
    try:
        descriptor = os.open(
            probe,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        )
    except OSError as error:
        return error.errno in {errno.EROFS, errno.EACCES, errno.EPERM}
    os.close(descriptor)
    probe.unlink()
    return False


def child_allowlist() -> tuple[Path, ...]:
    paths = [Path("/usr"), Path("/lib"), Path("/proc"), Path("/dev")]
    for extra in (Path("/lib64"), Path("/workspace")):
        if extra.exists():
            paths.append(extra)
    return tuple(paths)


def _measured_ready_payload(
    manifest: RunnerManifestV1, auth_reference: str, identity: Path
) -> tuple[bytes, ...]:
    if landlock_kernel_abi() < REQUIRED_LANDLOCK_ABI:
        raise LandlockUnavailable("runner-child-boundary-unavailable")
    payload = (
        manifest.executor_revision.encode("utf-8"),
        manifest.executor_operational_identity.encode("utf-8"),
        struct.pack(">Q", os.getuid()),
        struct.pack(">Q", os.getgid()),
        _status_field("CapEff").lower().encode("ascii"),
        b"1" if _status_field("NoNewPrivs") == "1" else b"0",
        b"1" if _read_only_root() else b"0",
        struct.pack(">Q", _pid_limit()),
        struct.pack(">Q", REQUIRED_LANDLOCK_ABI),
        b"DENIED" if _identity_mount_denied(identity) else b"WRITABLE",
        hashlib.sha256(auth_reference.encode("ascii")).hexdigest().encode("ascii"),
    )
    require_ready_matches_manifest(payload, manifest, auth_reference)
    return payload


def _decline_crossing_cancel(
    channel: RunnerFrameChannel,
    fence: _CoreFrameFence,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    sequence: int,
    evidence_hash: RunnerTerminalEvidenceHash,
) -> tuple[RunnerSessionFrame, int]:
    """Answer a CANCEL that crossed the already-sent TERMINAL_AVAILABLE.

    This invocation's one evidence envelope is already published to the
    journal before it is ever offered, so a cancellation racing that offer
    cannot change the outcome. REFUSE closes the race by naming it instead of
    silently dropping Core's frame or killing the session; the caller then
    keeps waiting for the READBACK that was always coming.
    """
    sequence += 1
    _write_frame(
        channel,
        _frame(
            RunnerSessionMessage.REFUSE,
            sequence,
            binding,
            invocation,
            (
                CROSSING_CANCEL_REFUSAL_CODE.encode("ascii"),
                evidence_hash.value.encode("ascii"),
            ),
        ),
    )
    return fence.read_frame(), sequence


# The exact message `RunnerJournal.readback` raises when nothing is retained
# yet for a binding -- the one legal "first lifetime" case a resumed
# candidate must tell apart from real journal corruption (a foreign binding,
# or unreadable bytes), which stays a loud failure.
_JOURNAL_RECORD_MISSING = "runner-terminal-record-missing"


def _retained_terminal_record(
    journal: RunnerJournal, binding: RunnerGenerationBinding
) -> RunnerTerminalEvidenceEnvelope | RunnerTerminalEvidenceAckTombstone | None:
    """What a prior lifetime already fixed for this exact binding, if anything.

    A crashed candidate may have already published -- or even had Core
    acknowledge -- its one terminal fact before dying. Resume always replays
    the full OFFER-to-RELEASED handshake (no new message, no phase-derived
    session); this is only what lets it reuse a prior lifetime's fact instead
    of racing a second child against it.
    """
    try:
        return journal.readback(binding)
    except ValueError as error:
        if str(error) != _JOURNAL_RECORD_MISSING:
            raise
        return None


def _after_terminal_evidence_published() -> None:
    """No-op in production; a deterministic pytest crash-cut seam only.

    A resume test replaces this with `os._exit` to model a candidate dying
    right after journaling its terminal fact but before telling Core
    (`TERMINAL_AVAILABLE`) -- crash cut C3. This is never the shared
    Docker-witness `CandidateScenario` surface B5 owns.
    """


def _after_ack_tombstone_published() -> None:
    """No-op in production; the deterministic pytest crash-cut counterpart.

    A resume test replaces this with `os._exit` to model a candidate dying
    right after telling Core its evidence was acknowledged (`ACK_TOMBSTONE`)
    but before `RELEASE` arrives -- crash cut C5.
    """


def run_candidate_session(
    channel: RunnerFrameChannel,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    scenario: CandidateScenario,
    manifest_path: Path,
    identity: Path,
    journal_directory: Path,
) -> None:
    """Drive one authenticated candidate session from OFFER to RELEASED.

    `channel` is already connected and peer-verified by the caller; this
    function never resolves a name, opens a socket, or negotiates TLS. A
    prior lifetime's retained journal record, if any, decides whether this
    call launches a real child or replays the fact that lifetime already
    fixed -- see `_retained_terminal_record`.
    """
    fence = _CoreFrameFence(channel, binding, invocation)
    journal = RunnerJournal(journal_directory)
    retained = _retained_terminal_record(journal, binding)
    sequence = 1
    _write_frame(
        channel,
        _frame(RunnerSessionMessage.INVOCATION_OFFER, sequence, binding, invocation),
    )
    prepare = fence.read_frame()
    if prepare.message is not RunnerSessionMessage.PREPARE:
        raise RuntimeError("Core did not prepare the exact candidate invocation")
    request = decode_runner_prepare_payload(prepare.payload, binding.request_hash)
    refuse_unbound_runner_a_request(request)
    resolver = FreeRunnerAuthorizationResolver()
    auth = request.resolved_binding.auth_profile
    reference = resolver.reference_for(auth)
    if prepare.payload[PREPARE_AUTH_REFERENCE_FIELD] != reference.encode("ascii"):
        raise RuntimeError("auth-profile-unresolvable")
    resolver.resolve(auth, reference)
    manifest = decode_runner_manifest(manifest_path.read_bytes())
    if runner_manifest_id(manifest) != binding.manifest_id:
        raise RuntimeError("runner-manifest-mismatch")
    sequence += 1
    _write_frame(
        channel,
        _frame(
            RunnerSessionMessage.READY,
            sequence,
            binding,
            invocation,
            _measured_ready_payload(manifest, reference, identity),
        ),
    )
    if fence.read_frame().message is not RunnerSessionMessage.LAUNCH:
        raise RuntimeError("Core did not durably arm the candidate invocation")
    sequence += 1
    if retained is not None:
        # A prior lifetime already fixed this invocation's one terminal fact
        # (published, or even acknowledged) before it died. Starting a second
        # child now would race a real result against the retained one for no
        # reason; the wire handshake still replays in full, just without the
        # child.
        _write_frame(
            channel,
            _frame(
                RunnerSessionMessage.STARTED,
                sequence,
                binding,
                invocation,
                (struct.pack(">Q", 1),),
            ),
        )
        terminal_record_source: (
            RunnerTerminalEvidenceEnvelope | RunnerTerminalEvidenceAckTombstone
        ) = retained
        if isinstance(retained, RunnerTerminalEvidenceEnvelope):
            evidence_hash = RunnerTerminalEvidenceHash.for_envelope(retained)
        else:
            evidence_hash = retained.evidence_hash
    else:
        child = start_runner_child(
            _child_command(scenario, manifest), child_allowlist()
        )
        try:
            _write_frame(
                channel,
                _frame(
                    RunnerSessionMessage.STARTED,
                    sequence,
                    binding,
                    invocation,
                    (struct.pack(">Q", 1),),
                ),
            )
            control = _control_or_child_exit(fence, child, manifest)
            if (
                control is not None
                and control.message is not RunnerSessionMessage.CANCEL
            ):
                raise RuntimeError("Core sent an unexpected post-start control frame")
            if control is not None:
                if control.payload[3] != b"NONE":
                    raise RuntimeError("runner-replacement-not-supported-a")
                envelope = RunnerTerminalEvidenceEnvelope(
                    binding,
                    invocation,
                    RunnerCancellation(
                        control.payload[1].decode("utf-8"),
                        _reap_child(child, manifest),
                    ),
                )
            else:
                if child.poll() is None:
                    raise RuntimeError("candidate child outlived the attempt")
                output, _error = child.communicate()
                if child.returncode != 0:
                    raise RuntimeError(
                        "free candidate child did not return its bounded result"
                    )
                envelope = RunnerTerminalEvidenceEnvelope(
                    binding,
                    invocation,
                    RunnerProviderResult(AgentExecutionResult(output.strip())),
                )
        except BaseException:
            if child.poll() is None:
                _reap_child(child, manifest)
            raise
        journal.publish(envelope)
        _after_terminal_evidence_published()
        terminal_record_source = envelope
        evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
    sequence += 1
    _write_frame(
        channel,
        _frame(
            RunnerSessionMessage.TERMINAL_AVAILABLE,
            sequence,
            binding,
            invocation,
            (evidence_hash.value.encode("ascii"),),
        ),
    )
    readback = fence.read_frame()
    if readback.message is RunnerSessionMessage.CANCEL:
        readback, sequence = _decline_crossing_cancel(
            channel, fence, binding, invocation, sequence, evidence_hash
        )
    if readback.message is not RunnerSessionMessage.READBACK:
        raise RuntimeError("Core did not request retained evidence")
    sequence += 1
    encoded = encode_runner_terminal_evidence_record(terminal_record_source)
    _write_frame(
        channel,
        _frame(
            RunnerSessionMessage.TERMINAL_RECORD,
            sequence,
            binding,
            invocation,
            (encoded,),
        ),
    )
    acknowledgement = fence.read_frame()
    if acknowledgement.message is not RunnerSessionMessage.ACK:
        raise RuntimeError("Core did not acknowledge durable evidence")
    accepted = require_matching_evidence_hash(acknowledgement.payload, evidence_hash)
    if isinstance(terminal_record_source, RunnerTerminalEvidenceAckTombstone):
        # This exact lifetime already collected this ACK before it died --
        # the journal still holds nothing else to re-tombstone.
        tombstone = terminal_record_source
    else:
        tombstone = journal.acknowledge(terminal_record_source, accepted)
    sequence += 1
    _write_frame(
        channel,
        _frame(
            RunnerSessionMessage.ACK_TOMBSTONE,
            sequence,
            binding,
            invocation,
            (encode_runner_terminal_evidence_record(tombstone),),
        ),
    )
    _after_ack_tombstone_published()
    release = fence.read_frame()
    if release.message is not RunnerSessionMessage.RELEASE:
        raise RuntimeError("Core did not release acknowledged evidence")
    released = require_matching_evidence_hash(release.payload, evidence_hash)
    journal.release(binding, released)
    sequence += 1
    _write_frame(
        channel,
        _frame(
            RunnerSessionMessage.RELEASED,
            sequence,
            binding,
            invocation,
            (evidence_hash.value.encode("ascii"),),
        ),
    )
