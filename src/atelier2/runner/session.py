"""The candidate Runner's session state machine, off the wire.

Everything here drives OFFER through RELEASED against a narrow byte channel —
no socket, TLS, or DNS import crosses this boundary. `__main__` owns the
socket connect, the TLS handshake, and the peer-certificate fence; once such
an authenticated channel exists, this module owns every frame that flows
across it, the child process it supervises, and the journal it publishes to.

A Core that dies mid-session is not this invocation's death: the provider
child keeps running and the session asks its caller for another authenticated
channel, replaying the whole handshake from OFFER on it. Core answers each
repeated frame out of its own cache, so the replay costs no second child and
no second durable write.
"""

from __future__ import annotations

import errno
import hashlib
import os
import struct
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from atelier2.adapters.bounded_processes import bounded_process_streams
from atelier2.adapters.free_runner_executor import (
    free_runner_auth_reference,
    refuse_unbound_runner_a_request,
    resolve_free_runner_authorization,
)
from atelier2.adapters.runner_child import (
    REQUIRED_LANDLOCK_ABI,
    LandlockUnavailable,
    landlock_kernel_abi,
    reap_cancelled_runner_child,
    start_runner_child,
)
from atelier2.adapters.runner_cli_pins import (
    RunnerToolchainRefused,
    RunnerToolchainUnpinned,
    runner_executor_cli_pin,
)
from atelier2.adapters.runner_journal import RunnerJournal
from atelier2.application.run_runner_session import (
    CROSSING_CANCEL_REFUSAL_CODE,
    decode_runner_prepare_payload,
    require_matching_evidence_hash,
    require_ready_matches_manifest,
)
from atelier2.contracts.agent_attempts import (
    ProcessExitSignature,
    RunnerCancellation,
    RunnerCancellationObservation,
    RunnerEvidenceCannotCarryTranscript,
    RunnerGenerationBinding,
    RunnerInvocationId,
    RunnerProviderFailure,
    RunnerProviderResult,
    RunnerTerminalEvidenceAckTombstone,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import AgentExecutionRequestV2
from atelier2.contracts.runner_manifests import (
    RunnerManifestV1,
    decode_runner_manifest,
    encode_measured_provider_cli,
    runner_manifest_id,
)
from atelier2.contracts.runner_session_codec import (
    PREPARE_AUTH_REFERENCE_FIELD,
    decode_runner_session_frame,
    encode_runner_session_frame,
    runner_session_body_length,
)
from atelier2.contracts.runner_sessions import (
    NO_REFUSED_EVIDENCE,
    RunnerSessionFrame,
    RunnerSessionMessage,
)
from atelier2.contracts.runner_terminal_evidence_codec import (
    encode_runner_terminal_evidence_record,
)
from atelier2.ports.agent_executions import (
    AgentAttemptWorkspaceLease,
    AgentExecutionFailure,
    AgentExecutorV2,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from atelier2.runner.executors import (
    attest_runner_provider_toolchain,
    select_runner_executor,
)

_CONTROL_POLL_SECONDS = 0.05

# Distinct from every reserved shell/signal exit code (0, 1, 2, 126-165) so
# the disposable witness launcher can tell this declared crash apart from an
# unrelated failure when it asserts on the exited container's reported exit
# status. Never a promise the wire protocol makes to Core -- Core only ever
# observes the dropped connection.
_CRASH_AFTER_PUBLISH_EXIT_CODE = 92


class CandidateScenario(StrEnum):
    """The session ending the witness Core declares in the public bootstrap.

    The A candidate exists to prove three endings — a bounded child result, a
    cancel that reaps the child, and a real crash the Docker witness restarts
    and resumes from (`CRASH_AFTER_PUBLISH`) — so the child it launches, and
    the process death it may itself trigger, are declared, closed candidate
    facts rather than an implicit test key.
    """

    SUCCESS = "success"
    CANCEL = "cancel"
    CRASH_AFTER_PUBLISH = "crash-after-publish"


class RunnerFrameChannel(Protocol):
    """The narrow byte boundary the candidate session drives.

    Anything that can recv, sendall, settimeout, and close satisfies it — a
    real TLS socket in production, a plain `socket.socketpair()` half in a
    test. The session state machine never imports socket or ssl itself.
    """

    def recv(self, buffer_size: int, /) -> bytes: ...

    def sendall(self, data: bytes, /) -> None: ...

    def settimeout(self, value: float | None, /) -> None: ...

    def close(self) -> None: ...


# Open one already-connected, peer-verified channel to Core, or raise `OSError`
# if Core cannot be reached right now. The socket, the TLS handshake and the
# peer fence behind it belong to `__main__`; this module only ever asks for
# another channel when the one it had died.
ConnectToCore = Callable[[], RunnerFrameChannel]


class CoreConnectionLost(ConnectionError):
    """This Core connection is gone; the invocation it carried may still live."""


class CoreUnreachable(ConnectionError):
    """Core stayed unreachable for the whole span this invocation was given."""


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
            try:
                piece = self._channel.recv(length - len(self._buffer))
            except TimeoutError:
                # The control poll's own bound, not a failure: `read_frame`
                # asks for one with a timeout on purpose while a child runs.
                raise
            except OSError as error:
                raise CoreConnectionLost("the Core session channel failed") from error
            if not piece:
                raise CoreConnectionLost("Core closed the candidate session")
            self._buffer.extend(piece)


class _SessionWire:
    """One Core connection, driven as this invocation's ordered frame exchange.

    The outbound sequence restarts at one on every connection because that is
    what makes a replay a replay: Core answers a sequence it already accepted
    out of its own cache, and only if the frame arrives as the exact bytes it
    cached, so a reconnect must number and shape its frames the way the lost
    connection did.
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
        self._fence = _CoreFrameFence(channel, binding, invocation)
        self._sequence = 0

    def send(
        self, message: RunnerSessionMessage, payload: tuple[bytes, ...] = ()
    ) -> None:
        self._sequence += 1
        frame = RunnerSessionFrame(
            message, self._sequence, self._binding, self._invocation, payload
        )
        try:
            self._channel.sendall(encode_runner_session_frame(frame))
        except OSError as error:
            raise CoreConnectionLost("the Core session channel failed") from error

    def read(self, timeout: float | None = None) -> RunnerSessionFrame:
        return self._fence.read_frame(timeout)


def _reap_child(
    child: subprocess.Popen[bytes], manifest: RunnerManifestV1
) -> RunnerCancellationObservation:
    return reap_cancelled_runner_child(
        child,
        manifest.terminate_grace_milliseconds / 1000,
        manifest.reap_deadline_milliseconds / 1000,
    )


@dataclass(frozen=True, slots=True)
class _ProviderChild:
    """The one provider process this invocation launched, and all it was given.

    It outlives a dropped Core connection deliberately: a Core that dies while
    a paid provider child is working must not cost that work. Its deadline is
    fixed at launch rather than recomputed per connection, so a reconnect
    extends this container's patience with Core and never the span the
    manifest gave the child.
    """

    executor: AgentExecutorV2
    command: AgentProcessCommand
    process: subprocess.Popen[bytes]
    deadline: float

    def release(self) -> None:
        """Take back what this child was given, once it can no longer use it."""
        self.executor.release_credential_channel(self.command)
        self.executor.close()


def _child_deadline(manifest: RunnerManifestV1) -> float:
    """When the span this manifest gave one provider child runs out."""
    return time.monotonic() + manifest.total_attempt_milliseconds / 1000


def _control_or_child_exit(
    wire: _SessionWire, child: subprocess.Popen[bytes], deadline: float
) -> RunnerSessionFrame | None:
    while child.poll() is None and time.monotonic() < deadline:
        try:
            return wire.read(timeout=_CONTROL_POLL_SECONDS)
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


def _runner_workspace_directory() -> Path:
    """The one ephemeral workspace this container was given before it started.

    A fresh tmpfs Docker provisions once per container in production
    (`scripts/runner_candidate.sh`); nothing here creates it or ever releases
    it -- see `AgentAttemptWorkspaceLease`, whose identity is entered rather
    than minted.
    """
    return Path("/workspace")


def _workspace_lease(
    binding: RunnerGenerationBinding, workspace: Path
) -> AgentAttemptWorkspaceLease:
    """This container's one workspace, entered by an identity this call itself checks.

    The descriptor is opened once; both the working directory and the device
    and inode `decode_process_completion` needs come off that same descriptor,
    so nothing between the open and the `fstat` can substitute what the path
    names.
    """
    descriptor = os.open(
        workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    )
    try:
        status = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    return AgentAttemptWorkspaceLease(
        binding.attempt_id, workspace, status.st_dev, status.st_ino
    )


def _drain_exited_child(
    child: subprocess.Popen[bytes],
    manifest: RunnerManifestV1,
    maximum_output_bytes: int,
) -> AgentProcessCompletion:
    """Both bounded streams of a child that has already exited.

    Reuses `bounded_processes` instead of a second read loop; the child is
    already dead here (the caller only reaches this once `child.poll()` is no
    longer `None`), so the deadline only bounds how long fully draining its
    already-produced pipes may legitimately take.
    """
    return_code, standard_output, standard_error = bounded_process_streams(
        child, manifest.reap_deadline_milliseconds / 1000, maximum_output_bytes
    )
    return AgentProcessCompletion(return_code, standard_output, standard_error)


def _measured_ready_payload(
    manifest: RunnerManifestV1, auth_reference: str, identity: Path
) -> tuple[bytes, ...]:
    """Everything this container can prove about itself, measured before READY.

    The provider-toolchain attestation runs here, ahead of the whole rest of
    the session: a Runner whose installed CLI is outside the conformance set
    its manifest's executor revision names, or whose host or account still
    lets administrator policy act, refuses before Core ever arms the attempt
    and therefore before any provider process could start.
    """
    if landlock_kernel_abi() < REQUIRED_LANDLOCK_ABI:
        raise LandlockUnavailable(CHILD_BOUNDARY_UNAVAILABLE_REFUSAL_CODE)
    measured_cli = attest_runner_provider_toolchain(manifest)
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
        encode_measured_provider_cli(measured_cli),
    )
    require_ready_matches_manifest(
        payload, manifest, auth_reference, runner_executor_cli_pin(manifest)
    )
    return payload


# The one code this Runner names when the kernel itself cannot enforce the
# child boundary. `LandlockUnavailable` carries prose from its other raise
# sites, so the wire never reads its message: the code is stated here, checked
# against the protocol's declared vocabulary by Core, and used for both the
# refusal this module raises and the frame it sends about it.
CHILD_BOUNDARY_UNAVAILABLE_REFUSAL_CODE = "runner-child-boundary-unavailable"


def _attested_ready_payload(
    wire: _SessionWire,
    manifest: RunnerManifestV1,
    auth_reference: str,
    identity: Path,
) -> tuple[bytes, ...]:
    """Measure this container for READY, or tell Core by name why it cannot.

    A Runner that cannot attest itself -- its provider toolchain, or the kernel
    boundary it would confine a child with -- has to say so on the wire before
    it dies. Dropping the connection would leave Core with a torn socket and no
    reason, so the refusal Core would otherwise have to guess at becomes a
    REFUSE frame carrying the exact code -- loud and named -- and only then does
    this lifetime end. Nothing is armed and nothing durable is written on
    either side.
    """
    try:
        return _measured_ready_payload(manifest, auth_reference, identity)
    except (RunnerToolchainUnpinned, RunnerToolchainRefused) as refusal:
        # These carry their own declared code as their message by construction.
        _refuse_before_start(wire, str(refusal))
        raise
    except LandlockUnavailable:
        _refuse_before_start(wire, CHILD_BOUNDARY_UNAVAILABLE_REFUSAL_CODE)
        raise


def _refuse_before_start(wire: _SessionWire, code: str) -> None:
    wire.send(RunnerSessionMessage.REFUSE, (code.encode("ascii"), NO_REFUSED_EVIDENCE))


def _decline_crossing_cancel(
    wire: _SessionWire, evidence_hash: RunnerTerminalEvidenceHash
) -> RunnerSessionFrame:
    """Answer a CANCEL that crossed the already-sent TERMINAL_AVAILABLE.

    This invocation's one evidence envelope is already published to the
    journal before it is ever offered, so a cancellation racing that offer
    cannot change the outcome. REFUSE closes the race by naming it instead of
    silently dropping Core's frame or killing the session; the caller then
    keeps waiting for the READBACK that was always coming.
    """
    wire.send(
        RunnerSessionMessage.REFUSE,
        (
            CROSSING_CANCEL_REFUSAL_CODE.encode("ascii"),
            evidence_hash.value.encode("ascii"),
        ),
    )
    return wire.read()


# The exact message `RunnerJournal.readback` raises when nothing is retained
# yet for a binding -- the one legal "first lifetime" case a resumed
# candidate must tell apart from real journal corruption (a foreign binding,
# or unreadable bytes), which stays a loud failure.
_JOURNAL_RECORD_MISSING = "runner-terminal-record-missing"


def retained_terminal_record(
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
    (`TERMINAL_AVAILABLE`) -- crash cut C3. The Docker-witness surface for
    that same cut is `CandidateScenario.CRASH_AFTER_PUBLISH` below, which
    exits for real from declared scenario data rather than a monkeypatched
    seam; this hook stays a no-op for that scenario too.
    """


def _after_ack_tombstone_published() -> None:
    """No-op in production; the deterministic pytest crash-cut counterpart.

    A resume test replaces this with `os._exit` to model a candidate dying
    right after telling Core its evidence was acknowledged (`ACK_TOMBSTONE`)
    but before `RELEASE` arrives -- crash cut C5.
    """


_TerminalRecord = RunnerTerminalEvidenceEnvelope | RunnerTerminalEvidenceAckTombstone


def _evidence_hash_of(record: _TerminalRecord) -> RunnerTerminalEvidenceHash:
    return (
        record.evidence_hash
        if isinstance(record, RunnerTerminalEvidenceAckTombstone)
        else RunnerTerminalEvidenceHash.for_envelope(record)
    )


def _selected_manifest(
    manifest_path: Path, binding: RunnerGenerationBinding
) -> RunnerManifestV1:
    """The carrier facts Core selected for this exact generation, or a refusal."""
    manifest = decode_runner_manifest(manifest_path.read_bytes())
    if runner_manifest_id(manifest) != binding.manifest_id:
        raise RuntimeError("runner-manifest-mismatch")
    return manifest


class _ReconnectPatience:
    """How long this container keeps trying to reach a Core that went away.

    The manifest's attempt span is the bound, because it is the span Core
    selected for this invocation and the span this container's own identity
    was minted for (ADR 0009 sec. 2): past it a reconnect would present a key
    that opens nothing, and a Runner still waiting would be a leak rather than
    a hope.

    Between tries it waits exactly `_CONTROL_POLL_SECONDS` -- the cadence this
    session already spends looking at an open channel while a child runs. A
    refused connect inside the Attempt's own private network costs no more
    than that poll does, so a widening ladder would buy nothing and would
    leave a Core that came back waiting out whichever step the container
    happened to be asleep in. What must never be unbounded is the total, and
    the span above is what bounds it.
    """

    def __init__(self, manifest: RunnerManifestV1) -> None:
        self._deadline = time.monotonic() + manifest.total_attempt_milliseconds / 1000

    def wait_or_give_up(self, loss: OSError) -> None:
        if time.monotonic() + _CONTROL_POLL_SECONDS >= self._deadline:
            raise CoreUnreachable(
                "Core stayed unreachable for this invocation's whole attempt span"
            ) from loss
        time.sleep(_CONTROL_POLL_SECONDS)


class _CandidateLifetime:
    """One candidate process's whole invocation, across every connection it needs.

    A dropped connection costs this container nothing it already did: the
    measured READY attestation, a running provider child, the terminal fact
    this invocation fixed and the tombstone Core acknowledged all live here.
    The next connection therefore replays the exact frames Core already
    cached, instead of re-measuring a provider, racing a second child against
    work this attempt is already paying for, or minting bytes Core would
    refuse as a different frame under a sequence it already answered.
    """

    def __init__(
        self,
        binding: RunnerGenerationBinding,
        invocation: RunnerInvocationId,
        scenario: CandidateScenario,
        manifest: RunnerManifestV1,
        identity: Path,
        journal: RunnerJournal,
    ) -> None:
        self._binding = binding
        self._invocation = invocation
        self._scenario = scenario
        self._manifest = manifest
        self._identity = identity
        self._journal = journal
        self._ready_payload: tuple[bytes, ...] | None = None
        self._child: _ProviderChild | None = None
        self._terminal_record: _TerminalRecord | None = None
        self._tombstone: RunnerTerminalEvidenceAckTombstone | None = None
        self._cancel_observed = False

    @property
    def survives_a_lost_connection(self) -> bool:
        """Whether replaying this invocation on a fresh connection stays honest.

        Replay works because every frame this lifetime sends is derived from
        state it pinned, so Core answers each repeated sequence out of its own
        cache. A CANCEL breaks exactly that: Core sends it whenever it decides
        to, and where it landed in the lost connection is what decided this
        candidate's own frame numbering around it, which a replay cannot
        reproduce. A cancelled attempt has no paid work left to save anyway,
        so it ends here the way it did before this loop existed -- the child
        reaped, the journal keeping whatever it already fixed.
        """
        return not self._cancel_observed

    def drive(self, channel: RunnerFrameChannel) -> None:
        """Replay this invocation from OFFER to RELEASED on one Core connection."""
        wire = _SessionWire(channel, self._binding, self._invocation)
        request, auth_reference = self._offer(wire)
        wire.send(
            RunnerSessionMessage.READY, self._attested_ready(wire, auth_reference)
        )
        if wire.read().message is not RunnerSessionMessage.LAUNCH:
            raise RuntimeError("Core did not durably arm the candidate invocation")
        work = self._work_under_way(request)
        wire.send(RunnerSessionMessage.STARTED, (struct.pack(">Q", 1),))
        self._deliver(wire, self._fixed_terminal_record(wire, work))

    def close(self) -> None:
        """Give up this invocation's child, leaving nothing of it running."""
        child = self._child
        if child is None:
            return
        self._child = None
        try:
            if child.process.poll() is None:
                _reap_child(child.process, self._manifest)
        finally:
            child.release()

    def _offer(self, wire: _SessionWire) -> tuple[AgentExecutionRequestV2, str]:
        wire.send(RunnerSessionMessage.INVOCATION_OFFER)
        prepare = wire.read()
        if prepare.message is not RunnerSessionMessage.PREPARE:
            raise RuntimeError("Core did not prepare the exact candidate invocation")
        request = decode_runner_prepare_payload(
            prepare.payload, self._binding.request_hash
        )
        refuse_unbound_runner_a_request(request)
        auth = request.resolved_binding.auth_profile
        auth_reference = free_runner_auth_reference(auth)
        if prepare.payload[PREPARE_AUTH_REFERENCE_FIELD] != auth_reference.value.encode(
            "ascii"
        ):
            raise RuntimeError("auth-profile-unresolvable")
        resolve_free_runner_authorization(auth, auth_reference)
        return request, auth_reference.value

    def _attested_ready(
        self, wire: _SessionWire, auth_reference: str
    ) -> tuple[bytes, ...]:
        """This container's own measurement, taken once and then repeated verbatim.

        Re-measuring on a reconnect would run the provider CLI again for an
        answer Core already cached under the same sequence, and a measurement
        that drifted between the two would make the replayed READY a different
        frame -- refused as one, rather than recognized as the retry it is.
        """
        if self._ready_payload is None:
            self._ready_payload = _attested_ready_payload(
                wire, self._manifest, auth_reference, self._identity
            )
        return self._ready_payload

    def _work_under_way(
        self, request: AgentExecutionRequestV2
    ) -> _ProviderChild | _TerminalRecord:
        """This invocation's live child, or the terminal fact already fixed for it.

        A child this lifetime already started, a fact it already journalled,
        or a record a prior lifetime left behind all mean the work is under
        way or done; only a genuinely empty invocation launches a provider.
        """
        if self._child is not None:
            return self._child
        if self._terminal_record is None:
            self._terminal_record = retained_terminal_record(
                self._journal, self._binding
            )
        if self._terminal_record is not None:
            return self._terminal_record
        self._child = self._launch(request)
        return self._child

    def _launch(self, request: AgentExecutionRequestV2) -> _ProviderChild:
        executor = select_runner_executor(self._manifest)
        try:
            command = executor.prepare_process(request)
            try:
                process = start_runner_child(
                    command.arguments,
                    self._manifest.child_path_grants,
                    environment=command.environment,
                    standard_input=command.standard_input,
                )
            except BaseException:
                executor.release_credential_channel(command)
                raise
        except BaseException:
            executor.close()
            raise
        return _ProviderChild(
            executor, command, process, _child_deadline(self._manifest)
        )

    def _fixed_terminal_record(
        self, wire: _SessionWire, work: _ProviderChild | _TerminalRecord
    ) -> _TerminalRecord:
        if not isinstance(work, _ProviderChild):
            return work
        self._terminal_record = self._journalled_child_result(wire, work)
        return self._terminal_record

    def _journalled_child_result(
        self, wire: _SessionWire, child: _ProviderChild
    ) -> RunnerTerminalEvidenceEnvelope:
        """Wait this invocation's child out, then fix what it did, once and for good.

        A connection lost while waiting escapes from here untouched, leaving
        the child running for the next one -- that is what the reconnect loop
        exists for. Every other ending is terminal for the child, so it is
        reaped and its credential channel taken back before anything at all is
        journalled.
        """
        control = _control_or_child_exit(wire, child.process, child.deadline)
        # Past the control wait this child ends here on every remaining path,
        # so the lifetime lets go of it now: a connection lost *before* this
        # line keeps the child for the next one, and nothing after it may reap
        # or release the same child a second time from `close`.
        self._child = None
        try:
            envelope = self._child_evidence(control, child)
        except BaseException:
            if child.process.poll() is None:
                _reap_child(child.process, self._manifest)
            raise
        finally:
            child.release()
        self._journal.publish(envelope, self._manifest.journal_bytes)
        _after_terminal_evidence_published()
        if self._scenario is CandidateScenario.CRASH_AFTER_PUBLISH:
            # A declared witness scenario, not a test seam: this lifetime dies
            # for real right after journaling its terminal fact but before
            # telling Core (crash cut C3), so the Docker witness can restart
            # this exact container and prove resume delivers the retained
            # evidence through RELEASED (`#15-B5`).
            os._exit(_CRASH_AFTER_PUBLISH_EXIT_CODE)
        return envelope

    def _child_evidence(
        self, control: RunnerSessionFrame | None, child: _ProviderChild
    ) -> RunnerTerminalEvidenceEnvelope:
        if control is None:
            return RunnerTerminalEvidenceEnvelope(
                self._binding, self._invocation, self._completed_child_evidence(child)
            )
        if control.message is not RunnerSessionMessage.CANCEL:
            raise RuntimeError("Core sent an unexpected post-start control frame")
        self._cancel_observed = True
        if control.payload[3] != b"NONE":
            raise RuntimeError("runner-replacement-not-supported-a")
        return RunnerTerminalEvidenceEnvelope(
            self._binding,
            self._invocation,
            RunnerCancellation(
                control.payload[1].decode("utf-8"),
                _reap_child(child.process, self._manifest),
            ),
        )

    def _completed_child_evidence(
        self, child: _ProviderChild
    ) -> RunnerProviderResult | RunnerProviderFailure:
        if child.process.poll() is None:
            raise RuntimeError("candidate child outlived the attempt")
        completion = _drain_exited_child(
            child.process, self._manifest, child.command.standard_output_frame_bytes
        )
        lease = _workspace_lease(self._binding, _runner_workspace_directory())
        outcome = child.executor.decode_process_completion(
            AgentProcessInvocation(child.command, lease), completion
        )
        # A failing outcome's steps would be dropped here rather than by the
        # evidence record, because this evidence carries only how the child
        # exited. The success door refuses one in `RunnerProviderResult`; this
        # is the same refusal at the other door, so neither ending can
        # silently record a Runner-carried attempt as having decoded nothing.
        if (
            isinstance(outcome, AgentExecutionFailure)
            and outcome.transcript is not None
        ):
            raise RunnerEvidenceCannotCarryTranscript(
                "runner terminal evidence does not carry an attempt transcript yet"
            )
        return (
            RunnerProviderFailure(
                ProcessExitSignature(completion.return_code, completion.standard_error)
            )
            if isinstance(outcome, AgentExecutionFailure)
            else RunnerProviderResult(outcome)
        )

    def _deliver(self, wire: _SessionWire, record: _TerminalRecord) -> None:
        """Offer, hand over and release this invocation's one terminal fact."""
        evidence_hash = _evidence_hash_of(record)
        wire.send(
            RunnerSessionMessage.TERMINAL_AVAILABLE,
            (evidence_hash.value.encode("ascii"),),
        )
        readback = wire.read()
        if readback.message is RunnerSessionMessage.CANCEL:
            self._cancel_observed = True
            readback = _decline_crossing_cancel(wire, evidence_hash)
        if readback.message is not RunnerSessionMessage.READBACK:
            raise RuntimeError("Core did not request retained evidence")
        wire.send(
            RunnerSessionMessage.TERMINAL_RECORD,
            (encode_runner_terminal_evidence_record(record),),
        )
        acknowledgement = wire.read()
        if acknowledgement.message is not RunnerSessionMessage.ACK:
            raise RuntimeError("Core did not acknowledge durable evidence")
        accepted = require_matching_evidence_hash(
            acknowledgement.payload, evidence_hash
        )
        wire.send(
            RunnerSessionMessage.ACK_TOMBSTONE,
            (
                encode_runner_terminal_evidence_record(
                    self._acknowledged(record, accepted)
                ),
            ),
        )
        _after_ack_tombstone_published()
        release = wire.read()
        if release.message is not RunnerSessionMessage.RELEASE:
            raise RuntimeError("Core did not release acknowledged evidence")
        self._release(require_matching_evidence_hash(release.payload, evidence_hash))
        wire.send(RunnerSessionMessage.RELEASED, (evidence_hash.value.encode("ascii"),))

    def _acknowledged(
        self, record: _TerminalRecord, accepted: RunnerTerminalEvidenceHash
    ) -> RunnerTerminalEvidenceAckTombstone:
        """This invocation's one ACK tombstone, minted once and repeated verbatim.

        A record that arrived as a prior lifetime's tombstone is already the
        answer; anything else is tombstoned here, exactly once. The journal
        cannot stand in for this memory on a replay: it holds the tombstone
        only between ACK and RELEASE, and nothing at all after.
        """
        if self._tombstone is None:
            self._tombstone = (
                record
                if isinstance(record, RunnerTerminalEvidenceAckTombstone)
                else self._journal.acknowledge(record, accepted)
            )
        return self._tombstone

    def _release(self, released: RunnerTerminalEvidenceHash) -> None:
        """Drop the retained record, unless a lost connection already got here.

        The journal is its own record of whether this ran: release unlinks
        what it releases, so an empty journal on a replay means this
        invocation already reached exactly this point.
        """
        if retained_terminal_record(self._journal, self._binding) is not None:
            self._journal.release(self._binding, released)


def run_candidate_session(
    connect_to_core: ConnectToCore,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    scenario: CandidateScenario,
    manifest_path: Path,
    identity: Path,
    journal_directory: Path,
) -> None:
    """Drive one candidate invocation from OFFER to RELEASED, across Core deaths.

    `connect_to_core` hands back one already-connected, peer-verified channel
    per call; this function never resolves a name, opens a socket, or
    negotiates TLS. Core dying is not this invocation's death: the provider
    child keeps running and the next channel replays the whole handshake from
    OFFER, which Core answers out of its durable and cached state until the
    real terminal evidence lands. The manifest's attempt span bounds that
    patience, and a Core still unreachable at the end of it ends the
    invocation the way a lost connection always did -- the child reaped, the
    credential channel taken back, nothing of either left running.

    A prior lifetime's retained journal record, if any, decides whether this
    call launches a real child at all -- see `retained_terminal_record`.

    Named, deferred gap (`#15-B4`): an empty journal only proves "nothing was
    ever published"; it cannot yet tell apart a lifetime that crashed before
    calling `start_runner_child` from one that crashed *after* -- while a
    real child from that earlier lifetime could still be alive, orphaned,
    outside this process's own reap path. Closing that gap needs a durable
    "child observed started" marker neither this journal nor anything else
    carries yet; the reconnect loop here removes only the in-process half of
    it, where this same process still holds the child it started. Until then,
    only a caller that can guarantee this exact container's previous lifetime
    never reached `start_runner_child` may safely resume it this way.

    `journal_directory` must survive a caller's own crash and restart for
    `retained_terminal_record` to ever find anything -- a tmpfs mount, which
    a Docker restart wipes, cannot carry that guarantee (`#15-B5`).
    """
    manifest = _selected_manifest(manifest_path, binding)
    lifetime = _CandidateLifetime(
        binding,
        invocation,
        scenario,
        manifest,
        identity,
        RunnerJournal(journal_directory),
    )
    patience = _ReconnectPatience(manifest)
    try:
        while True:
            try:
                channel = connect_to_core()
            except OSError as unreachable:
                patience.wait_or_give_up(unreachable)
                continue
            try:
                lifetime.drive(channel)
                return
            except CoreConnectionLost as lost:
                if not lifetime.survives_a_lost_connection:
                    raise
                patience.wait_or_give_up(lost)
            finally:
                channel.close()
    finally:
        lifetime.close()
