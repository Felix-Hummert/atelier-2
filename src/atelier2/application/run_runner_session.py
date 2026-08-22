from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.agent_attempts import (
    AgentAttemptReplacement,
    RunnerBindingConflict,
    RunnerGenerationBinding,
    RunnerInvocationId,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.agents import (
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentConfigurationRevisionHash,
    AgentExecutionCapability,
    AgentExecutionRequestHash,
    AgentExecutionRequestV2,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    AuthProfileRevisionHash,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runner_manifests import RunnerManifestV1
from atelier2.contracts.runner_sessions import RunnerSessionFrame, RunnerSessionMessage
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationResult,
    AgentAttemptReplacementNotAllowed,
)
from atelier2.ports.runner_sessions import RunnerSessionCore


class RunnerSessionRefusal(ValueError):
    """A peer's otherwise valid frame cannot advance this single-use exchange."""


# The runner's closed REFUSE answer to a CANCEL that crossed its already-sent
# TERMINAL_AVAILABLE: the same conflict code `cancel()` itself raises when a
# caller asks to cancel after Core has already accepted that frame locally.
# One protocol token shared by both ends of the wire, not two literals that
# could drift apart.
CROSSING_CANCEL_REFUSAL_CODE = "runner-cancel-conflict"


class _CorePhase(StrEnum):
    OFFER = "OFFER"
    READY = "READY"
    STARTED = "STARTED"
    TERMINAL_AVAILABLE = "TERMINAL_AVAILABLE"
    TERMINAL_RECORD = "TERMINAL_RECORD"
    ACK_TOMBSTONE = "ACK_TOMBSTONE"
    RELEASED = "RELEASED"


def cancellation_refusal_code(result: AgentAttemptCancellationResult) -> str:
    if isinstance(result, AgentAttemptCancellationAccepted):
        raise TypeError("accepted cancellation is not a refusal")
    if isinstance(result, AgentAttemptReplacementNotAllowed):
        return "runner-replacement-not-supported-a"
    return CROSSING_CANCEL_REFUSAL_CODE


def encode_runner_prepare_payload(
    request: AgentExecutionRequestV2, auth_reference: str
) -> tuple[bytes, ...]:
    binding = request.resolved_binding
    auth = binding.auth_profile
    configuration = binding.configuration
    reference = auth_reference.encode("ascii")
    if not 1 <= len(reference) <= 128:
        raise RunnerSessionRefusal("auth-profile-unresolvable")
    return (
        request.node_execution_id.value.encode("ascii"),
        request.run_id.value.encode("utf-8"),
        request.workflow_revision_hash.value.encode("ascii"),
        request.node_id.encode("utf-8"),
        binding.role.value.encode("utf-8"),
        configuration.revision_hash.value.encode("ascii"),
        auth.revision_hash.value.encode("ascii"),
        auth.profile_id.encode("utf-8"),
        struct.pack(">Q", auth.revision_number),
        auth.provider_id.value.encode("ascii"),
        auth.auth_mode.value.encode("ascii"),
        configuration.model.encode("utf-8"),
        configuration.executor_revision.value.encode("utf-8"),
        configuration.requested_capability.value.encode("ascii"),
        struct.pack(">Q", int(configuration.revision_format_version)),
        request.executor_operational_identity.value.encode("utf-8"),
        request.job_bytes,
        struct.pack(">Q", request.round_ordinal),
        reference,
    )


def decode_runner_prepare_payload(
    payload: tuple[bytes, ...], expected_request_hash: AgentExecutionRequestHash
) -> AgentExecutionRequestV2:
    if len(payload) != 19:
        raise RunnerSessionRefusal("runner-session-noncanonical")
    try:
        auth = AuthProfileRevision(
            payload[7].decode("utf-8"),
            _uint64(payload[8]),
            ProviderId(_ascii(payload[9])),
            AuthMode(_ascii(payload[10])),
        )
        if auth.revision_hash != AuthProfileRevisionHash(_ascii(payload[6])):
            raise RunnerSessionRefusal("runner-request-hash-mismatch")
        configuration = AgentConfigurationRevision(
            payload[11].decode("utf-8"),
            auth.revision_hash,
            AgentExecutorRevision(payload[12].decode("utf-8")),
            AgentExecutionCapability(_ascii(payload[13])),
            AgentConfigurationRevisionFormatVersion(_uint64(payload[14])),
        )
        if configuration.revision_hash != AgentConfigurationRevisionHash(
            _ascii(payload[5])
        ):
            raise RunnerSessionRefusal("runner-request-hash-mismatch")
        request = AgentExecutionRequestV2(
            NodeExecutionId(_ascii(payload[0])),
            RunId(payload[1].decode("utf-8")),
            WorkflowRevisionHash(_ascii(payload[2])),
            payload[3].decode("utf-8"),
            ResolvedAgentBinding(
                AgentRole(payload[4].decode("utf-8")), configuration, auth
            ),
            AgentExecutorOperationalIdentity(payload[15].decode("utf-8")),
            payload[16],
            round_ordinal=_uint64(payload[17]),
        )
    except RunnerSessionRefusal:
        raise
    except (TypeError, UnicodeDecodeError, ValueError) as error:
        raise RunnerSessionRefusal("runner-session-noncanonical") from error
    if request.request_hash != expected_request_hash:
        raise RunnerSessionRefusal("runner-request-hash-mismatch")
    return request


def encode_runner_ready_payload(
    manifest: RunnerManifestV1, auth_reference: str
) -> tuple[bytes, ...]:
    return (
        manifest.executor_revision.encode("utf-8"),
        manifest.executor_operational_identity.encode("utf-8"),
        struct.pack(">Q", manifest.effective_uid),
        struct.pack(">Q", manifest.effective_gid),
        manifest.effective_capabilities.encode("ascii"),
        b"1" if manifest.no_new_privileges else b"0",
        b"1" if manifest.read_only_root else b"0",
        struct.pack(">Q", manifest.process_limit),
        struct.pack(">Q", manifest.required_landlock_abi),
        b"DENIED",
        hashlib.sha256(auth_reference.encode("ascii")).hexdigest().encode("ascii"),
    )


def require_ready_matches_manifest(
    payload: tuple[bytes, ...],
    manifest: RunnerManifestV1,
    auth_reference: str,
) -> None:
    expected = encode_runner_ready_payload(manifest, auth_reference)
    if len(payload) != 11:
        raise RunnerSessionRefusal("runner-session-noncanonical")
    if payload[5] != b"1" or payload[6] != b"1" or payload[9] != b"DENIED":
        raise RunnerSessionRefusal("runner-attestation-mismatch")
    if payload[10] != expected[10]:
        raise RunnerSessionRefusal("auth-profile-unresolvable")
    if payload[0] != expected[0] or payload[1] != expected[1]:
        raise RunnerSessionRefusal("runner-manifest-mismatch")
    if payload[2:9] != expected[2:9]:
        raise RunnerSessionRefusal("runner-attestation-mismatch")


def _uint64(field: bytes) -> int:
    if len(field) != 8:
        raise RunnerSessionRefusal("runner-session-noncanonical")
    return struct.unpack(">Q", field)[0]


def _ascii(value: bytes) -> str:
    return value.decode("ascii")


def require_matching_evidence_hash(
    payload: tuple[bytes, ...], retained: RunnerTerminalEvidenceHash
) -> RunnerTerminalEvidenceHash:
    if len(payload) != 1:
        raise RunnerSessionRefusal("runner-session-noncanonical")
    try:
        presented = RunnerTerminalEvidenceHash(_ascii(payload[0]))
    except (TypeError, UnicodeDecodeError, ValueError) as error:
        raise RunnerSessionRefusal("runner-ack-hash-mismatch") from error
    if presented != retained:
        raise RunnerSessionRefusal("runner-ack-hash-mismatch")
    return presented


@dataclass
class CoreRunnerSession:
    """The one-shot Core-side ordering fence for one authenticated invocation."""

    binding: RunnerGenerationBinding
    core: RunnerSessionCore
    prepare_payload: tuple[bytes, ...]
    manifest: RunnerManifestV1
    auth_reference: str
    invocation_id: RunnerInvocationId
    _phase: _CorePhase = _CorePhase.OFFER
    _next_runner_sequence: int = 1
    _next_core_sequence: int = 1
    _accepted: dict[int, tuple[RunnerSessionFrame, RunnerSessionFrame | None]] = field(
        default_factory=dict
    )
    _terminal_hash: RunnerTerminalEvidenceHash | None = None
    _cancellation: RunnerSessionFrame | None = None
    _offered_evidence_hash: RunnerTerminalEvidenceHash | None = None

    def accept(self, frame: RunnerSessionFrame) -> RunnerSessionFrame | None:
        duplicate = self._accepted.get(frame.sequence)
        if duplicate is not None:
            previous, response = duplicate
            if previous != frame:
                raise RunnerSessionRefusal("runner-session-replay")
            return response
        if frame.sequence != self._next_runner_sequence:
            raise RunnerSessionRefusal("runner-session-sequence-mismatch")
        if frame.binding != self.binding or frame.invocation_id != self.invocation_id:
            raise RunnerSessionRefusal("runner-session-binding-mismatch")
        response = self._advance(frame)
        self._accepted[frame.sequence] = (frame, response)
        self._next_runner_sequence += 1
        return response

    def cancel(self) -> RunnerSessionFrame:
        if self._cancellation is not None:
            return self._cancellation
        if self._phase is not _CorePhase.TERMINAL_AVAILABLE:
            raise RunnerSessionRefusal(CROSSING_CANCEL_REFUSAL_CODE)
        command = self.core.cancel()
        if command.replacement is not AgentAttemptReplacement.NONE:
            raise RunnerSessionRefusal("runner-replacement-not-supported-a")
        self._cancellation = RunnerSessionFrame(
            RunnerSessionMessage.CANCEL,
            self._next_core_sequence,
            self.binding,
            self.invocation_id,
            (
                command.run_id.value.encode("utf-8"),
                command.command_id.encode("utf-8"),
                command.expected_attempt_state_version.to_bytes(8, "big"),
                command.replacement.value.encode("ascii"),
            ),
        )
        self._next_core_sequence += 1
        return self._cancellation

    def _advance(self, frame: RunnerSessionFrame) -> RunnerSessionFrame | None:
        if (
            self._phase is _CorePhase.TERMINAL_RECORD
            and frame.message is RunnerSessionMessage.REFUSE
        ):
            return self._accept_crossing_cancel_refusal(frame)
        expected = {
            _CorePhase.OFFER: RunnerSessionMessage.INVOCATION_OFFER,
            _CorePhase.READY: RunnerSessionMessage.READY,
            _CorePhase.STARTED: RunnerSessionMessage.STARTED,
            _CorePhase.TERMINAL_AVAILABLE: RunnerSessionMessage.TERMINAL_AVAILABLE,
            _CorePhase.ACK_TOMBSTONE: RunnerSessionMessage.ACK_TOMBSTONE,
            _CorePhase.RELEASED: RunnerSessionMessage.RELEASED,
        }.get(self._phase)
        if frame.message is not expected:
            raise RunnerSessionRefusal("runner-session-out-of-order")
        match self._phase:
            case _CorePhase.OFFER:
                self._phase = _CorePhase.READY
                return self._core_frame(
                    RunnerSessionMessage.PREPARE, self.prepare_payload
                )
            case _CorePhase.READY:
                require_ready_matches_manifest(
                    frame.payload, self.manifest, self.auth_reference
                )
                try:
                    self.core.arm(self.binding, self.invocation_id)
                except RunnerBindingConflict as error:
                    raise RunnerSessionRefusal("runner-arm-conflict") from error
                self._phase = _CorePhase.STARTED
                return self._core_frame(RunnerSessionMessage.LAUNCH, ())
            case _CorePhase.STARTED:
                self._phase = _CorePhase.TERMINAL_AVAILABLE
                return None
            case _CorePhase.TERMINAL_AVAILABLE:
                try:
                    self._offered_evidence_hash = RunnerTerminalEvidenceHash(
                        _ascii(frame.payload[0])
                    )
                except (UnicodeDecodeError, ValueError) as error:
                    raise RunnerSessionRefusal("runner-session-noncanonical") from error
                self._phase = _CorePhase.TERMINAL_RECORD
                return self._core_frame(RunnerSessionMessage.READBACK, ())
            case _CorePhase.ACK_TOMBSTONE:
                if self._terminal_hash is None:
                    raise RunnerSessionRefusal("runner-terminal-record-missing")
                self.core.acknowledge(
                    self.binding, self._terminal_hash, frame.payload[0]
                )
                self._phase = _CorePhase.RELEASED
                return self._core_frame(
                    RunnerSessionMessage.RELEASE,
                    (self._terminal_hash.value.encode("ascii"),),
                )
            case _CorePhase.RELEASED:
                return None
        raise AssertionError("runner session phase must be closed")

    def _accept_crossing_cancel_refusal(
        self, frame: RunnerSessionFrame
    ) -> RunnerSessionFrame | None:
        """A CANCEL this session sent crossed the runner's TERMINAL_AVAILABLE.

        The runner already fixed its one evidence envelope before offering it
        and refused the crossing cancel instead of desynchronizing; this
        session only reaches that refusal after it legally issued the cancel
        itself, while `_phase` was still `TERMINAL_AVAILABLE`. The readback
        flow continues unchanged with the evidence the runner already
        retained -- success or an earlier cancel, whichever the runner's
        journal fixed first. The refusal must name that exact evidence, not
        merely a well-formed hash -- otherwise a REFUSE about some other
        record would be absorbed as if it answered this session's cancel.
        """
        if self._cancellation is None:
            raise RunnerSessionRefusal("runner-session-out-of-order")
        if frame.payload[0] != CROSSING_CANCEL_REFUSAL_CODE.encode("ascii"):
            raise RunnerSessionRefusal("runner-session-noncanonical")
        try:
            presented = RunnerTerminalEvidenceHash(_ascii(frame.payload[1]))
        except (UnicodeDecodeError, ValueError) as error:
            raise RunnerSessionRefusal("runner-session-noncanonical") from error
        if presented != self._offered_evidence_hash:
            raise RunnerSessionRefusal("runner-session-noncanonical")
        return None

    def accept_terminal_record(self, frame: RunnerSessionFrame) -> RunnerSessionFrame:
        duplicate = self._accepted.get(frame.sequence)
        if duplicate is not None:
            previous, response = duplicate
            if previous != frame or response is None:
                raise RunnerSessionRefusal("runner-session-replay")
            return response
        if (
            self._phase is not _CorePhase.TERMINAL_RECORD
            or frame.message is not RunnerSessionMessage.TERMINAL_RECORD
        ):
            raise RunnerSessionRefusal("runner-session-out-of-order")
        if frame.sequence != self._next_runner_sequence:
            raise RunnerSessionRefusal("runner-session-sequence-mismatch")
        if frame.binding != self.binding or frame.invocation_id != self.invocation_id:
            raise RunnerSessionRefusal("runner-session-binding-mismatch")
        self._terminal_hash = self.core.commit_terminal_record(
            self.binding, frame.payload[0]
        )
        self._phase = _CorePhase.ACK_TOMBSTONE
        response = self._core_frame(
            RunnerSessionMessage.ACK,
            (self._terminal_hash.value.encode("ascii"),),
        )
        self._accepted[frame.sequence] = (frame, response)
        self._next_runner_sequence += 1
        return response

    def _core_frame(
        self,
        message: RunnerSessionMessage,
        payload: tuple[bytes, ...],
    ) -> RunnerSessionFrame:
        response = RunnerSessionFrame(
            message=message,
            sequence=self._next_core_sequence,
            binding=self.binding,
            invocation_id=self.invocation_id,
            payload=payload,
        )
        self._next_core_sequence += 1
        return response
