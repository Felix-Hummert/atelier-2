from __future__ import annotations

import argparse
import json
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import time
from pathlib import Path

from cryptography.x509.oid import ExtendedKeyUsageOID

from atelier2.adapters.runner_child import (
    LandlockUnavailable,
    install_landlock_guard,
    reap_cancelled_runner_child,
    start_runner_child,
)
from atelier2.adapters.runner_journal import RunnerJournal
from atelier2.adapters.runner_tls import CORE_DNS_NAME, validate_peer_certificate
from atelier2.contracts.agent_attempts import (
    AgentAttemptId,
    RunnerCancellation,
    RunnerGenerationBinding,
    RunnerGenerationId,
    RunnerInvocationId,
    RunnerManifestId,
    RunnerProviderResult,
    RunnerTerminalEvidenceEnvelope,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import AgentExecutionRequestHash, AgentExecutionResult
from atelier2.contracts.runner_session_codec import (
    decode_runner_session_frame,
    encode_runner_session_frame,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame, RunnerSessionMessage
from atelier2.contracts.runner_terminal_evidence_codec import (
    encode_runner_terminal_evidence_record,
)


def _read_exact(connection: ssl.SSLSocket, length: int) -> bytes:
    pieces: list[bytes] = []
    remaining = length
    while remaining:
        piece = connection.recv(remaining)
        if not piece:
            raise ConnectionError("Core closed the candidate session")
        pieces.append(piece)
        remaining -= len(piece)
    return b"".join(pieces)


def _read_frame(connection: ssl.SSLSocket) -> RunnerSessionFrame:
    prefix = _read_exact(connection, 4)
    return decode_runner_session_frame(
        prefix + _read_exact(connection, struct.unpack(">I", prefix)[0])
    )


def _write_frame(connection: ssl.SSLSocket, frame: RunnerSessionFrame) -> None:
    connection.sendall(encode_runner_session_frame(frame))


def _binding(bootstrap: dict[str, str]) -> RunnerGenerationBinding:
    return RunnerGenerationBinding(
        AgentAttemptId(bootstrap["attempt_id"]),
        AgentExecutionRequestHash(bootstrap["request_hash"]),
        RunnerGenerationId(bootstrap["generation_id"]),
        RunnerManifestId(bootstrap["manifest_id"]),
    )


def _frame(
    message: RunnerSessionMessage,
    sequence: int,
    binding: RunnerGenerationBinding,
    invocation: RunnerInvocationId,
    payload: tuple[bytes, ...] = (),
) -> RunnerSessionFrame:
    return RunnerSessionFrame(message, sequence, binding, invocation, payload)


def _wait_for_client_identity(identity: Path) -> None:
    ready = identity / "ready"
    deadline = time.monotonic() + 10
    while not ready.is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    if (
        not ready.is_file()
        or not (identity / "client.crt").is_file()
        or not (identity / "client.key").is_file()
    ):
        raise RuntimeError(
            "external issuer did not complete the candidate identity handoff"
        )


def _control_or_child_exit(
    connection: ssl.SSLSocket, child: subprocess.Popen[bytes]
) -> RunnerSessionFrame | None:
    deadline = time.monotonic() + 60
    while child.poll() is None and time.monotonic() < deadline:
        connection.settimeout(0.05)
        try:
            return _read_frame(connection)
        except TimeoutError:
            continue
        finally:
            connection.settimeout(None)
    return None


def _run_candidate_session(
    handoff: Path, identity: Path, journal_directory: Path, invocation_offer: Path
) -> int:
    bootstrap = json.loads(
        handoff.joinpath("bootstrap.json").read_text(encoding="utf-8")
    )
    binding = _binding(bootstrap)
    invocation = RunnerInvocationId(secrets.token_urlsafe(32))
    invocation_offer.write_text(
        json.dumps(
            {"invocation_id": invocation.value}, sort_keys=True, separators=(",", ":")
        ),
        encoding="utf-8",
    )
    _wait_for_client_identity(identity)
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
    if identity.joinpath("ca.crt").read_bytes() != ca_certificate:
        raise RuntimeError("Runner volume CA differs from bootstrap CA")
    context = ssl.create_default_context(
        ssl.Purpose.SERVER_AUTH, cafile=str(identity / "ca.crt")
    )
    context.load_cert_chain(identity / "client.crt", identity / "client.key")
    context.check_hostname = False
    with (
        socket.create_connection((CORE_DNS_NAME, 8443), 5) as raw,
        context.wrap_socket(raw, server_hostname=CORE_DNS_NAME) as connection,
    ):
        sequence = 1
        _write_frame(
            connection,
            _frame(
                RunnerSessionMessage.INVOCATION_OFFER, sequence, binding, invocation
            ),
        )
        prepare = _read_frame(connection)
        if prepare.message is not RunnerSessionMessage.PREPARE:
            raise RuntimeError("Core did not prepare the exact candidate invocation")
        sequence += 1
        _write_frame(
            connection,
            _frame(
                RunnerSessionMessage.READY,
                sequence,
                binding,
                invocation,
                (
                    b"fake-free/v1",
                    b"free-runner-candidate",
                    struct.pack(">Q", 10001),
                    struct.pack(">Q", 10001),
                    b"0000000000000000",
                    b"1",
                    b"1",
                    struct.pack(">Q", 64),
                    struct.pack(">Q", 1),
                    b"DENIED",
                    b"0" * 64,
                ),
            ),
        )
        if _read_frame(connection).message is not RunnerSessionMessage.LAUNCH:
            raise RuntimeError("Core did not durably arm the candidate invocation")
        sequence += 1
        if bootstrap.get("scenario") == "cancel":
            child = start_runner_child(
                (
                    sys.executable,
                    "-c",
                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
                )
            )
        else:
            child = start_runner_child(
                (
                    sys.executable,
                    "-c",
                    "import json; print(json.dumps('runner candidate'))",
                )
            )
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
        control = _control_or_child_exit(connection, child)
        if control is not None and control.message is not RunnerSessionMessage.CANCEL:
            raise RuntimeError("Core sent an unexpected post-start control frame")
        if control is not None:
            if control.payload[3] != b"NONE":
                raise RuntimeError("runner-replacement-not-supported-a")
            envelope = RunnerTerminalEvidenceEnvelope(
                binding,
                invocation,
                RunnerCancellation(
                    control.payload[1].decode("utf-8"),
                    reap_cancelled_runner_child(child, 1, 5),
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
        if _read_frame(connection).message is not RunnerSessionMessage.READBACK:
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
        acknowledgement = _read_frame(connection)
        if acknowledgement.message is not RunnerSessionMessage.ACK:
            raise RuntimeError("Core did not acknowledge durable evidence")
        tombstone = journal.acknowledge(envelope, evidence_hash)
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
        release = _read_frame(connection)
        if release.message is not RunnerSessionMessage.RELEASE:
            raise RuntimeError("Core did not release acknowledged evidence")
        journal.release(binding, evidence_hash)
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
        install_landlock_guard((Path("/usr"), Path("/lib"), Path("/lib64")))
    except LandlockUnavailable as error:
        parser.error(f"Landlock is unavailable: {error}")
    return 0
