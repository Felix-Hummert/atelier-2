from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from atelier2.contracts.agent_attempts import (
    AgentAttemptReplacement,
    RunnerBindingConflict,
    RunnerGenerationBinding,
    RunnerInvocationId,
    RunnerTerminalEvidenceHash,
)
from atelier2.contracts.runner_sessions import RunnerSessionFrame, RunnerSessionMessage
from atelier2.ports.agent_attempts import (
    AgentAttemptCancellationAccepted,
    AgentAttemptCancellationResult,
    AgentAttemptReplacementNotAllowed,
)
from atelier2.ports.runner_sessions import RunnerSessionCore


class RunnerSessionRefusal(ValueError):
    """A peer's otherwise valid frame cannot advance this single-use exchange."""


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
    return "runner-cancel-conflict"


@dataclass
class CoreRunnerSession:
    """The one-shot Core-side ordering fence for one authenticated invocation."""

    binding: RunnerGenerationBinding
    core: RunnerSessionCore
    _phase: _CorePhase = _CorePhase.OFFER
    _next_runner_sequence: int = 1
    _next_core_sequence: int = 1
    _accepted: dict[int, tuple[RunnerSessionFrame, RunnerSessionFrame | None]] = field(
        default_factory=dict
    )
    _terminal_hash: RunnerTerminalEvidenceHash | None = None
    _invocation: RunnerInvocationId | None = None
    _cancellation: RunnerSessionFrame | None = None

    def accept(self, frame: RunnerSessionFrame) -> RunnerSessionFrame | None:
        duplicate = self._accepted.get(frame.sequence)
        if duplicate is not None:
            previous, response = duplicate
            if previous != frame:
                raise RunnerSessionRefusal("runner-session-replay")
            return response
        if frame.sequence != self._next_runner_sequence:
            raise RunnerSessionRefusal("runner-session-sequence-mismatch")
        if frame.binding != self.binding:
            raise RunnerSessionRefusal("runner-session-binding-mismatch")
        response = self._advance(frame)
        self._accepted[frame.sequence] = (frame, response)
        self._next_runner_sequence += 1
        return response

    def cancel(self) -> RunnerSessionFrame:
        if self._cancellation is not None:
            return self._cancellation
        if self._phase is not _CorePhase.TERMINAL_AVAILABLE or self._invocation is None:
            raise RunnerSessionRefusal("runner-cancel-conflict")
        command = self.core.cancel()
        if command.replacement is not AgentAttemptReplacement.NONE:
            raise RunnerSessionRefusal("runner-replacement-not-supported-a")
        self._cancellation = RunnerSessionFrame(
            RunnerSessionMessage.CANCEL,
            self._next_core_sequence,
            self.binding,
            self._invocation,
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
                self._invocation = frame.invocation_id
                self._phase = _CorePhase.READY
                return self._core_frame(
                    RunnerSessionMessage.PREPARE, (b"",) * 19, frame
                )
            case _CorePhase.READY:
                try:
                    self.core.arm(self.binding, frame.invocation_id)
                except RunnerBindingConflict as error:
                    raise RunnerSessionRefusal("runner-arm-conflict") from error
                self._phase = _CorePhase.STARTED
                return self._core_frame(RunnerSessionMessage.LAUNCH, (), frame)
            case _CorePhase.STARTED:
                self._phase = _CorePhase.TERMINAL_AVAILABLE
                return None
            case _CorePhase.TERMINAL_AVAILABLE:
                self._phase = _CorePhase.TERMINAL_RECORD
                return self._core_frame(RunnerSessionMessage.READBACK, (), frame)
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
                    frame,
                )
            case _CorePhase.RELEASED:
                return None
        raise AssertionError("runner session phase must be closed")

    def accept_terminal_record(self, frame: RunnerSessionFrame) -> RunnerSessionFrame:
        if (
            self._phase is not _CorePhase.TERMINAL_RECORD
            or frame.message is not RunnerSessionMessage.TERMINAL_RECORD
        ):
            raise RunnerSessionRefusal("runner-session-out-of-order")
        if (
            frame.sequence != self._next_runner_sequence
            or frame.binding != self.binding
        ):
            raise RunnerSessionRefusal("runner-session-binding-mismatch")
        self._terminal_hash = self.core.commit_terminal_record(
            self.binding, frame.payload[0]
        )
        self._phase = _CorePhase.ACK_TOMBSTONE
        response = self._core_frame(
            RunnerSessionMessage.ACK,
            (self._terminal_hash.value.encode("ascii"),),
            frame,
        )
        self._accepted[frame.sequence] = (frame, response)
        self._next_runner_sequence += 1
        return response

    def _core_frame(
        self,
        message: RunnerSessionMessage,
        payload: tuple[bytes, ...],
        runner_frame: RunnerSessionFrame,
    ) -> RunnerSessionFrame:
        response = RunnerSessionFrame(
            message=message,
            sequence=self._next_core_sequence,
            binding=self.binding,
            invocation_id=runner_frame.invocation_id,
            payload=payload,
        )
        self._next_core_sequence += 1
        return response
