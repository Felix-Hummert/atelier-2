from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from atelier2.adapters.agent_process_protocol import (
    CONTROL_FRAME_TIMEOUT_SECONDS,
    MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES,
    ProviderLaunch,
    decode_launch_request,
    encode_control_frame,
    encode_wait_response,
)
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
)


class ProviderStream(StrEnum):
    """The provider output the watchdog reads and bounds."""

    STANDARD_OUTPUT = "stdout"
    STANDARD_ERROR = "stderr"


class ProviderStartFailed(RuntimeError):
    """No provider process exists; nothing was started that could outlive us."""


class ProviderOutputUnobservable(RuntimeError):
    """A provider process exists but its output cannot be watched."""


class ProviderSupervision(Protocol):
    """The provider generation as the operating system shows it to the watchdog."""

    def start(self, launch: ProviderLaunch) -> None:
        """Start the guarded provider, raising ProviderStartFailed if none runs."""
        ...

    def observe_output(self) -> None:
        """Watch the started provider's output streams."""
        ...

    def exit_status(self) -> int | None:
        """The provider's return code once it exited, without reaping it."""
        ...

    def reap(self) -> int:
        """Wait for the exited provider and return its reaped return code."""
        ...

    def terminate_group(self) -> bool:
        """Signal the provider group to terminate; False when it is already gone."""
        ...

    def kill_group(self) -> None: ...

    def close_standard_input(self) -> None: ...

    def close_output_stream(self, stream: ProviderStream) -> None: ...

    def close_all_streams(self) -> None: ...

    def contained_processes_remain(self) -> bool: ...

    def kill_contained_processes(self) -> None: ...


@dataclass
class ControlChannel:
    """One control peer's turn: what it asked, and what it is owed."""

    identity: int
    accepted_at: float
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytes | None = None
    sent_bytes: int = 0
    response_deadline: float | None = None
    slot: str = "UNCLASSIFIED"
    operation: str | None = None
    refuse_as_busy: bool = False


class _CoordinatorState(StrEnum):
    READY = "READY"
    LAUNCHING = "LAUNCHING"
    RUNNING = "RUNNING"
    CANCEL_TERMINATING = "CANCEL_TERMINATING"
    OVERFLOW_TERMINATING = "OVERFLOW_TERMINATING"
    SUPERVISION_TERMINATING = "SUPERVISION_TERMINATING"
    OWNER_DEATH_TERMINATING = "OWNER_DEATH_TERMINATING"
    RECOVERY_HANDOFF = "RECOVERY_HANDOFF"
    TERMINATED = "TERMINATED"
    FINALIZING = "FINALIZING"


_SLOT_OF_OPERATION = {
    "WAIT": "WAIT",
    "CANCEL": "TERMINAL_CONTROL",
    "FINALIZE": "TERMINAL_CONTROL",
    "LAUNCH": "LAUNCH_RETRY",
}
_TERMINATING_STATE_OF_OWNER = {
    "CANCEL": _CoordinatorState.CANCEL_TERMINATING,
    "OVERFLOW": _CoordinatorState.OVERFLOW_TERMINATING,
    "SUPERVISION": _CoordinatorState.SUPERVISION_TERMINATING,
    "OWNER_DEATH": _CoordinatorState.OWNER_DEATH_TERMINATING,
}
_IDLE_TIMEOUT_SECONDS = 0.05


class WatchdogCoordinator:
    """Every watchdog decision as a call: no socket, no process, no clock.

    The caller owns the transport and the operating system; it reports what
    arrived and applies what this coordinator decided. Time is an argument, so
    each transition is reachable without waiting for one.
    """

    def __init__(self, provider: ProviderSupervision, grace: float) -> None:
        self._provider = provider
        self._grace = grace
        self._channels: dict[int, ControlChannel] = {}
        self._slots: dict[str, int] = {}
        self._closed_channels: list[int] = []
        self._channels_awaiting_write: list[int] = []
        self._state = _CoordinatorState.READY
        self._launched = False
        self._return_code: int | None = None
        self._open_output: set[ProviderStream] = set()
        self._standard_output = bytearray()
        self._standard_error = bytearray()
        self._standard_output_frame_bytes: int | None = None
        self._launch_replay: tuple[bytes, bytes] | None = None
        self._wait_response: bytes | None = None
        self._cancel_response: bytes | None = None
        self._finalize_response: bytes | None = None
        self._termination_deadline: float | None = None
        self._termination_escalated = False
        self._termination_disposition: str | None = None
        self._termination_owner: str | None = None
        self._owner_dead = False

    @property
    def finished(self) -> bool:
        """The generation is over; the transport may stop serving."""

        return self._state is _CoordinatorState.FINALIZING

    def channel(self, identity: int) -> ControlChannel | None:
        return self._channels.get(identity)

    def open_channel(self, identity: int, now: float) -> ControlChannel:
        channel = ControlChannel(identity, now)
        if "UNCLASSIFIED" in self._slots:
            channel.refuse_as_busy = True
        else:
            self._slots["UNCLASSIFIED"] = identity
        self._channels[identity] = channel
        return channel

    def receive_budget(self, identity: int) -> int:
        """How many request bytes this channel may still be given."""

        channel = self._channels[identity]
        return MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES + 1 - len(channel.incoming)

    def receive_request(self, identity: int, chunk: bytes, now: float) -> None:
        channel = self._channels[identity]
        channel.incoming.extend(chunk)
        if len(channel.incoming) > MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES:
            response = "BUSY" if channel.refuse_as_busy else "FRAME_TOO_LARGE"
            self._queue_response(channel, {"type": response}, now)

    def close_request(self, identity: int, now: float) -> None:
        """The peer finished its request; classify and answer it."""

        channel = self._channels[identity]
        if channel.outgoing is not None:
            return
        if channel.refuse_as_busy:
            self._queue_response(channel, {"type": "BUSY"}, now)
            return
        self._classify_request(channel, bytes(channel.incoming), now)

    def record_response_sent(self, identity: int, byte_count: int) -> None:
        channel = self._channels[identity]
        channel.sent_bytes += byte_count
        if channel.outgoing is not None and channel.sent_bytes == len(channel.outgoing):
            self._close_channel(channel)

    def abandon_channel(self, identity: int) -> None:
        """The transport is gone; forget this channel without answering it."""

        channel = self._channels.get(identity)
        if channel is None:
            return
        self._release_slot(channel)
        self._channels.pop(identity, None)

    def drain_closed_channels(self) -> tuple[int, ...]:
        closed = tuple(self._closed_channels)
        self._closed_channels.clear()
        return closed

    def drain_channels_awaiting_write(self) -> tuple[int, ...]:
        awaiting = tuple(self._channels_awaiting_write)
        self._channels_awaiting_write.clear()
        return awaiting

    def owner_lost(self, now: float) -> None:
        self._owner_dead = True
        self._begin_termination("OWNER_DEATH", now)

    def report_provider_failure(self, now: float) -> None:
        """The provider's own descriptors failed; terminate the generation."""

        self._begin_termination("SUPERVISION", now)

    def report_supervision_failure(self, now: float) -> None:
        """This watchdog cannot supervise any further; converge or hand off."""

        if self._termination_owner is None:
            self._begin_termination("SUPERVISION", now)
        else:
            self._publish_recovery_handoff(now)

    def receive_provider_output(
        self, stream: ProviderStream, chunk: bytes, now: float
    ) -> None:
        if self._termination_owner is not None:
            return
        target = (
            self._standard_output
            if stream is ProviderStream.STANDARD_OUTPUT
            else self._standard_error
        )
        target.extend(chunk)
        declared_frame_bytes = self._standard_output_frame_bytes
        if declared_frame_bytes is None:
            raise RuntimeError("provider output arrived before its declared frame")
        limit = (
            declared_frame_bytes
            if stream is ProviderStream.STANDARD_OUTPUT
            else MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES
        )
        if len(target) > limit:
            self._standard_output.clear()
            self._standard_error.clear()
            self._provider.close_standard_input()
            self._begin_termination("OVERFLOW", now)

    def close_provider_output(self, stream: ProviderStream) -> None:
        if stream not in self._open_output:
            return
        self._open_output.discard(stream)
        self._provider.close_output_stream(stream)

    def advance(self, now: float) -> None:
        """Expire what ran out of time and follow the provider's own progress."""

        self._expire_channels(now)
        self._advance_provider(now)

    def next_deadline(self, now: float) -> float:
        deadlines = [
            channel.response_deadline
            if channel.outgoing is not None
            else (
                channel.accepted_at + CONTROL_FRAME_TIMEOUT_SECONDS
                if channel.operation is None
                else None
            )
            for channel in self._channels.values()
        ]
        deadlines.append(self._termination_deadline)
        finite = [deadline for deadline in deadlines if deadline is not None]
        if not finite:
            return _IDLE_TIMEOUT_SECONDS
        return max(0.0, min(_IDLE_TIMEOUT_SECONDS, min(finite) - now))

    def _classify_request(
        self, channel: ControlChannel, frame: bytes, now: float
    ) -> None:
        try:
            request = json.loads(frame.decode("ascii"))
            if not isinstance(request, dict) or encode_control_frame(request) != frame:
                raise ValueError
            operation = request.get("operation")
            if not isinstance(operation, str):
                raise TypeError
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            self._queue_response(channel, {"type": "MALFORMED"}, now)
            return
        slot = _SLOT_OF_OPERATION.get(operation)
        if slot is None:
            self._queue_response(channel, {"type": "MALFORMED"}, now)
            return
        self._release_slot(channel)
        if slot in self._slots:
            self._queue_response(channel, {"type": "BUSY"}, now)
            return
        self._slots[slot] = channel.identity
        channel.slot = slot
        channel.operation = operation
        if operation == "LAUNCH":
            self._handle_launch(channel, request, frame, now)
        elif operation == "WAIT":
            self._handle_wait(channel, now)
        elif operation == "CANCEL":
            self._handle_cancel(channel, now)
        else:
            self._handle_finalize(channel, now)

    def _handle_launch(
        self,
        channel: ControlChannel,
        request: dict[str, Any],
        frame: bytes,
        now: float,
    ) -> None:
        launch_replay = self._launch_replay
        if launch_replay is not None:
            launch_frame, launch_response = launch_replay
            response = (
                launch_response
                if frame == launch_frame
                else encode_control_frame({"type": "LAUNCH_MISMATCH"})
            )
            self._queue_encoded_response(channel, response, now)
            return
        if self._state is not _CoordinatorState.READY:
            launch_response = encode_control_frame(
                {
                    "outcome": "STOPPED",
                    "type": "TERMINAL_BEFORE_START",
                }
            )
            self._launch_replay = (frame, launch_response)
            self._publish_wait({"type": "STOPPED"}, now)
            self._termination_disposition = "NEVER_LAUNCHED"
            self._queue_encoded_response(channel, launch_response, now)
            return
        self._state = _CoordinatorState.LAUNCHING
        try:
            launch = decode_launch_request(request)
            self._standard_output_frame_bytes = launch.standard_output_frame_bytes
            self._provider.start(launch)
        except (ProviderStartFailed, TypeError, ValueError):
            self._provider.close_all_streams()
            self._open_output.clear()
            launch_response = encode_control_frame(
                {
                    "outcome": "SUPERVISION_FAILED",
                    "type": "TERMINAL_BEFORE_START",
                }
            )
            self._termination_disposition = "REAPED_AFTER_PROCESS_BOUNDARY_FAILURE"
            self._publish_wait({"type": "SUPERVISION_FAILED"}, now)
        else:
            self._launched = True
            launch_response = encode_control_frame({"type": "STARTED"})
            try:
                self._provider.observe_output()
            except ProviderOutputUnobservable:
                self._provider.close_all_streams()
                self._open_output.clear()
                self._begin_termination("SUPERVISION", now)
            else:
                self._open_output = {
                    ProviderStream.STANDARD_OUTPUT,
                    ProviderStream.STANDARD_ERROR,
                }
                self._state = _CoordinatorState.RUNNING
        self._launch_replay = (frame, launch_response)
        self._queue_encoded_response(channel, launch_response, now)

    def _handle_wait(self, channel: ControlChannel, now: float) -> None:
        if self._wait_response is not None:
            self._queue_encoded_response(channel, self._wait_response, now)

    def _handle_cancel(self, channel: ControlChannel, now: float) -> None:
        if self._cancel_response is not None:
            self._queue_encoded_response(channel, self._cancel_response, now)
            return
        if self._wait_response is not None:
            disposition = self._termination_disposition or "EXITED_BEFORE_SIGNAL"
            self._cancel_response = encode_control_frame(
                {"disposition": disposition, "type": "CANCELLED"}
            )
            self._queue_encoded_response(channel, self._cancel_response, now)
            return
        self._begin_termination("CANCEL", now)

    def _handle_finalize(self, channel: ControlChannel, now: float) -> None:
        if self._finalize_response is not None:
            self._queue_encoded_response(channel, self._finalize_response, now)
            return
        if self._wait_response is None:
            self._queue_response(channel, {"type": "FINALIZE_REFUSED"}, now)
            return
        self._finalize_response = encode_control_frame({"type": "FINALIZE_ACCEPTED"})
        self._queue_encoded_response(channel, self._finalize_response, now)

    def _advance_provider(self, now: float) -> None:
        if self._state is _CoordinatorState.TERMINATED:
            return
        if not self._launched:
            return
        if self._termination_owner is None:
            if (
                self._provider.exit_status() is not None
                and not self._open_output
                and not self._provider.contained_processes_remain()
            ):
                self._return_code = self._provider.reap()
                self._publish_process_completion(now)
            return
        if self._state is _CoordinatorState.RECOVERY_HANDOFF:
            return
        if (
            self._provider.exit_status() is not None
            and not self._provider.contained_processes_remain()
            and not self._open_output
        ):
            self._return_code = self._provider.reap()
            self._finish_termination(now)
            return
        if self._termination_deadline is not None and now >= self._termination_deadline:
            self._escalate_termination(now)

    def _begin_termination(self, owner: str, now: float) -> None:
        if self._termination_owner is not None:
            if owner == "OWNER_DEATH" and self._wait_response is not None:
                self._state = _CoordinatorState.FINALIZING
            return
        self._termination_owner = owner
        self._termination_escalated = False
        self._provider.close_standard_input()
        if not self._launched:
            self._termination_disposition = "NEVER_LAUNCHED"
            if owner == "CANCEL":
                self._publish_wait({"type": "STOPPED"}, now)
                self._publish_cancel(now)
            elif owner == "OWNER_DEATH":
                self._state = _CoordinatorState.FINALIZING
            else:
                arm = (
                    "OUTPUT_LIMIT_EXCEEDED"
                    if owner == "OVERFLOW"
                    else "SUPERVISION_FAILED"
                )
                self._publish_wait({"type": arm}, now)
            return
        self._state = _TERMINATING_STATE_OF_OWNER[owner]
        if (
            self._provider.exit_status() is not None
            and not self._provider.contained_processes_remain()
        ):
            self._termination_disposition = "EXITED_BEFORE_SIGNAL"
            if not self._open_output:
                self._return_code = self._provider.reap()
                self._finish_termination(now)
                return
            self._termination_deadline = now + self._grace
            return
        signalled = False
        if self._provider.exit_status() is None:
            signalled = self._provider.terminate_group()
        if signalled:
            self._termination_disposition = "REAPED_AFTER_TERM"
        self._termination_deadline = now + self._grace

    def _escalate_termination(self, now: float) -> None:
        if self._termination_escalated:
            self._publish_recovery_handoff(now)
            return
        self._termination_escalated = True
        self._termination_deadline = None
        if self._provider.contained_processes_remain():
            self._provider.kill_contained_processes()
            self._termination_disposition = "REAPED_AFTER_KILL"
        if self._launched and self._provider.exit_status() is None:
            self._provider.kill_group()
        self._termination_deadline = now + max(1.0, self._grace)

    def _finish_termination(self, now: float) -> None:
        owner = self._termination_owner
        if owner == "OWNER_DEATH" or self._owner_dead:
            self._state = _CoordinatorState.FINALIZING
            return
        if owner == "CANCEL":
            self._publish_process_completion(now)
            self._publish_cancel(now)
            return
        if owner == "OVERFLOW":
            self._termination_disposition = "REAPED_AFTER_PROCESS_BOUNDARY_FAILURE"
            self._publish_wait({"type": "OUTPUT_LIMIT_EXCEEDED"}, now)
            self._publish_cancel(now)
            return
        self._termination_disposition = "REAPED_AFTER_PROCESS_BOUNDARY_FAILURE"
        self._publish_wait({"type": "SUPERVISION_FAILED"}, now)
        self._publish_cancel(now)

    def _publish_recovery_handoff(self, now: float) -> None:
        if self._state is _CoordinatorState.RECOVERY_HANDOFF:
            return
        encoded = encode_control_frame({"type": "RECOVERY_HANDOFF"})
        self._wait_response = encoded
        self._cancel_response = encoded
        self._termination_deadline = None
        self._state = _CoordinatorState.RECOVERY_HANDOFF
        self._provider.close_all_streams()
        self._open_output.clear()
        for channel in tuple(self._channels.values()):
            if channel.operation in {"WAIT", "CANCEL"} and channel.outgoing is None:
                self._queue_encoded_response(channel, encoded, now)

    def _publish_process_completion(self, now: float) -> None:
        if self._return_code is None:
            raise RuntimeError("provider completion has no reaped return code")
        self._publish_wait(
            {
                "return_code": self._return_code,
                "standard_error": base64.b64encode(self._standard_error).decode(
                    "ascii"
                ),
                "standard_output": base64.b64encode(self._standard_output).decode(
                    "ascii"
                ),
                "type": "COMPLETED",
            },
            now,
        )

    def _publish_cancel(self, now: float) -> None:
        disposition = self._termination_disposition or "EXITED_BEFORE_SIGNAL"
        self._cancel_response = encode_control_frame(
            {"disposition": disposition, "type": "CANCELLED"}
        )
        for channel in tuple(self._channels.values()):
            if channel.operation == "CANCEL" and channel.outgoing is None:
                self._queue_encoded_response(channel, self._cancel_response, now)

    def _publish_wait(self, response: dict[str, object], now: float) -> None:
        if self._wait_response is not None:
            return
        encoded = encode_wait_response(response, self._standard_output_frame_bytes)
        self._wait_response = encoded
        self._state = _CoordinatorState.TERMINATED
        for channel in tuple(self._channels.values()):
            if channel.operation == "WAIT" and channel.outgoing is None:
                self._queue_encoded_response(channel, encoded, now)

    def _expire_channels(self, now: float) -> None:
        for channel in tuple(self._channels.values()):
            if channel.outgoing is None:
                if (
                    channel.operation is None
                    and now >= channel.accepted_at + CONTROL_FRAME_TIMEOUT_SECONDS
                ):
                    response = (
                        "BUSY" if channel.refuse_as_busy else "CONTROL_FRAME_TIMEOUT"
                    )
                    self._queue_response(channel, {"type": response}, now)
            elif (
                channel.response_deadline is not None
                and now >= channel.response_deadline
            ):
                self._close_channel(channel)

    def _queue_response(
        self, channel: ControlChannel, response: dict[str, object], now: float
    ) -> None:
        self._queue_encoded_response(channel, encode_control_frame(response), now)

    def _queue_encoded_response(
        self, channel: ControlChannel, response: bytes, now: float
    ) -> None:
        channel.outgoing = response
        channel.sent_bytes = 0
        channel.response_deadline = now + CONTROL_FRAME_TIMEOUT_SECONDS
        self._channels_awaiting_write.append(channel.identity)

    def _release_slot(self, channel: ControlChannel) -> None:
        if self._slots.get(channel.slot) == channel.identity:
            self._slots.pop(channel.slot, None)
        channel.slot = ""

    def _close_channel(self, channel: ControlChannel) -> None:
        self._release_slot(channel)
        self._channels.pop(channel.identity, None)
        self._closed_channels.append(channel.identity)
