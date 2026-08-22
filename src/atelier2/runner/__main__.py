from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import time
from enum import StrEnum
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

from atelier2.adapters.free_runner_executor import (
    FreeRunnerAuthorizationResolver,
    refuse_unbound_runner_a_request,
)
from atelier2.adapters.runner_child import (
    REQUIRED_LANDLOCK_ABI,
    LandlockUnavailable,
    install_landlock_guard,
    landlock_kernel_abi,
    reap_cancelled_runner_child,
    start_runner_child,
)
from atelier2.adapters.runner_journal import RunnerJournal
from atelier2.adapters.runner_tls import (
    CORE_DNS_NAME,
    pin_tls_13,
    runner_uri_for_invocation,
    validate_peer_certificate,
)
from atelier2.application.run_runner_session import (
    decode_runner_prepare_payload,
    require_matching_evidence_hash,
    require_ready_matches_manifest,
)
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerCancellation,
    RunnerCancellationObservation,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
    RunnerProviderResult,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import AgentExecutionRequestHash, AgentExecutionResult
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
from atelier2.runner.identity_receiver import load_published_identity

_CONTROL_POLL_SECONDS = 0.05


class CandidateScenario(StrEnum):
    """The session ending the witness Core declares in the public bootstrap.

    The A candidate exists to prove two endings — a bounded child result and a
    cancel that reaps the child — so the child it launches is a declared,
    closed candidate fact rather than an implicit test key.
    """

    SUCCESS = "success"
    CANCEL = "cancel"


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
        connection: socket.socket,
        binding: RunnerGenerationBinding,
        invocation: RunnerInvocationId,
    ) -> None:
        self._connection = connection
        self._binding = binding
        self._invocation = invocation
        self._buffer = bytearray()
        self._next_core_sequence = 1

    def read_frame(self, timeout: float | None = None) -> RunnerSessionFrame:
        self._connection.settimeout(timeout)
        try:
            self._buffer_exactly(4)
            frame_length = 4 + runner_session_body_length(bytes(self._buffer[:4]))
            self._buffer_exactly(frame_length)
        finally:
            self._connection.settimeout(None)
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
            piece = self._connection.recv(length - len(self._buffer))
            if not piece:
                raise ConnectionError("Core closed the candidate session")
            self._buffer.extend(piece)


def _write_frame(connection: ssl.SSLSocket, frame: RunnerSessionFrame) -> None:
    connection.sendall(encode_runner_session_frame(frame))


def _binding(bootstrap: dict[str, str]) -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        AgentAttemptId(bootstrap["attempt_id"]),
        AgentExecutionRequestHash(bootstrap["request_hash"]),
        RunnerGenerationId(bootstrap["generation_id"]),
        RunnerManifestId(bootstrap["manifest_id"]),
    )


def _declared_scenario(bootstrap: dict[str, str]) -> CandidateScenario:
    return CandidateScenario(bootstrap["scenario"])


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


def _frame(
    message: RunnerSessionMessage,
    sequence: int,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    payload: tuple[bytes, ...] = (),
) -> RunnerSessionFrame:
    return RunnerSessionFrame(message, sequence, binding, invocation, payload)


def _wait_for_file(path: Path, reason: str) -> None:
    deadline = time.monotonic() + 10
    while not path.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    if not path.is_file():
        raise RuntimeError(reason)


def _wait_for_client_identity(identity: Path) -> None:
    ready = identity / "ready"
    _wait_for_file(
        ready, "external issuer did not complete the candidate identity handoff"
    )
    if (
        not (identity / "client.crt").is_file()
        or not (identity / "client.key").is_file()
    ):
        raise RuntimeError(
            "external issuer did not complete the candidate identity handoff"
        )


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


def _child_allowlist() -> tuple[Path, ...]:
    paths = [Path("/usr"), Path("/lib"), Path("/proc"), Path("/dev")]
    for extra in (Path("/lib64"), Path("/workspace")):
        if extra.exists():
            paths.append(extra)
    return tuple(paths)


def _measured_ready_payload(manifest, auth_reference: str, identity: Path):
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


def _load_verified_client_identity(
    context: ssl.SSLContext, certificate: bytes, key: bytes, staging: Path
) -> None:
    """Load the exact bytes the identity reread validated, not the path again.

    A path-based load_cert_chain would reopen the volume files after
    validation, leaving a window in which different bytes could be picked up.
    An unlinked O_TMPFILE inode carries the validated bytes themselves;
    /proc/self/fd is the only way ssl can be handed pathless PEM material,
    and the descriptor is closed here before any child exists.
    """
    descriptor = os.open(staging, os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600)
    try:
        payload = certificate + b"\n" + key
        if os.write(descriptor, payload) != len(payload):
            raise RuntimeError("runner identity staging write was short")
        context.load_cert_chain(f"/proc/self/fd/{descriptor}")
    finally:
        os.close(descriptor)


def _run_candidate_session(
    handoff: Path, identity: Path, journal_directory: Path, invocation_offer: Path
) -> int:
    for name in ("bootstrap.json", "core-peer.json", "core.crt", "ca.crt", "manifest"):
        _wait_for_file(
            handoff / name, "launcher did not copy the public bootstrap inbound"
        )
    bootstrap = json.loads(
        handoff.joinpath("bootstrap.json").read_text(encoding="utf-8")
    )
    binding = _binding(bootstrap)
    scenario = _declared_scenario(bootstrap)
    invocation = RunnerInvocationId(secrets.token_urlsafe(32))
    invocation_offer.write_text(
        json.dumps(
            {"invocation_id": invocation.value}, sort_keys=True, separators=(",", ":")
        ),
        encoding="utf-8",
    )
    _wait_for_client_identity(identity)
    expected_runner_uri = runner_uri_for_invocation(binding, invocation)
    peer = json.loads(handoff.joinpath("core-peer.json").read_text(encoding="utf-8"))
    core_certificate = handoff.joinpath("core.crt").read_bytes()
    ca_certificate = handoff.joinpath("ca.crt").read_bytes()
    validate_peer_certificate(
        core_certificate,
        ca_certificate,
        expected_dns_name=CORE_DNS_NAME,
        expected_uri=peer["uri"],
        expected_eku=ExtendedKeyUsageOID.SERVER_AUTH,
    )
    client_certificate, client_key, volume_ca = load_published_identity(
        identity, expected_uri=expected_runner_uri, expected_ca=ca_certificate
    )
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH, cadata=volume_ca.decode("ascii")
    )
    pin_tls_13(context)
    _load_verified_client_identity(
        context, client_certificate, client_key, journal_directory
    )
    # The built-in hostname check would only repeat the DNS half of the manual
    # post-handshake fence below: validate_peer_certificate pins SAN DNS name,
    # SPKI-bound URI, and EKU against the bootstrap CA, and the fingerprint
    # comparison pins the exact presented leaf.
    context.check_hostname = False
    with (
        socket.create_connection((CORE_DNS_NAME, 8443), 5) as raw,
        context.wrap_socket(raw, server_hostname=CORE_DNS_NAME) as connection,
    ):
        presented = connection.getpeercert(binary_form=True)
        if presented is None:
            raise RuntimeError("Core did not present an authenticated leaf")
        presented_pem = x509.load_der_x509_certificate(presented).public_bytes(
            serialization.Encoding.PEM
        )
        validate_peer_certificate(
            presented_pem,
            ca_certificate,
            expected_dns_name=CORE_DNS_NAME,
            expected_uri=peer["uri"],
            expected_eku=ExtendedKeyUsageOID.SERVER_AUTH,
        )
        if hashlib.sha256(presented).hexdigest() != peer["fingerprint"]:
            raise RuntimeError("runner-peer-unverified")
        fence = _CoreFrameFence(connection, binding, invocation)
        sequence = 1
        _write_frame(
            connection,
            _frame(
                RunnerSessionMessage.INVOCATION_OFFER, sequence, binding, invocation
            ),
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
        manifest = decode_runner_manifest(handoff.joinpath("manifest").read_bytes())
        if runner_manifest_id(manifest) != binding.manifest_id:
            raise RuntimeError("runner-manifest-mismatch")
        sequence += 1
        _write_frame(
            connection,
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
        child = start_runner_child(
            _child_command(scenario, manifest), _child_allowlist()
        )
        try:
            _write_frame(
                connection,
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
        journal = RunnerJournal(journal_directory)
        journal.publish(envelope)
        evidence_hash = RunnerTerminalEvidenceHash.for_envelope(envelope)
        sequence += 1
        _write_frame(
            connection,
            _frame(
                RunnerSessionMessage.TERMINAL_AVAILABLE,
                sequence,
                binding,
                invocation,
                (evidence_hash.value.encode("ascii"),),
            ),
        )
        if fence.read_frame().message is not RunnerSessionMessage.READBACK:
            raise RuntimeError("Core did not request retained evidence")
        sequence += 1
        encoded = encode_runner_terminal_evidence_record(envelope)
        _write_frame(
            connection,
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
        accepted = require_matching_evidence_hash(
            acknowledgement.payload, evidence_hash
        )
        tombstone = journal.acknowledge(envelope, accepted)
        sequence += 1
        _write_frame(
            connection,
            _frame(
                RunnerSessionMessage.ACK_TOMBSTONE,
                sequence,
                binding,
                invocation,
                (encode_runner_terminal_evidence_record(tombstone),),
            ),
        )
        release = fence.read_frame()
        if release.message is not RunnerSessionMessage.RELEASE:
            raise RuntimeError("Core did not release acknowledged evidence")
        released = require_matching_evidence_hash(release.payload, evidence_hash)
        journal.release(binding, released)
        sequence += 1
        _write_frame(
            connection,
            _frame(
                RunnerSessionMessage.RELEASED,
                sequence,
                binding,
                invocation,
                (evidence_hash.value.encode("ascii"),),
            ),
        )
    return 0


def main(arguments: list[str] | None = None) -> int:
    """Run only the candidate Runner preflight until its Core handoff is provided."""
    parser = argparse.ArgumentParser(prog="atelier2-runner")
    parser.add_argument("--landlock-probe", action="store_true")
    parser.add_argument("--candidate-handoff", type=Path)
    parser.add_argument("--candidate-identity", type=Path)
    parser.add_argument("--candidate-journal", type=Path)
    parser.add_argument("--candidate-invocation-offer", type=Path)
    parsed = parser.parse_args(arguments)
    if parsed.candidate_handoff is not None:
        if (
            parsed.candidate_identity is None
            or parsed.candidate_journal is None
            or parsed.candidate_invocation_offer is None
        ):
            parser.error("candidate identity and journal are required together")
        return _run_candidate_session(
            parsed.candidate_handoff,
            parsed.candidate_identity,
            parsed.candidate_journal,
            parsed.candidate_invocation_offer,
        )
    if not parsed.landlock_probe:
        parser.error("the candidate Runner needs an explicit handoff mode")
    try:
        install_landlock_guard(_child_allowlist())
    except LandlockUnavailable as error:
        parser.error(f"Landlock is unavailable: {error}")
    return 0
