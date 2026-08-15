from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

from atelier2.adapters.agent_process_coordinator import (
    ProviderOutputUnobservable,
    ProviderStartFailed,
    ProviderStream,
    WatchdogCoordinator,
)
from atelier2.adapters.agent_process_protocol import (
    CONTROL_FRAME_TIMEOUT_SECONDS,
    MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES,
    ProviderLaunch,
    encode_control_frame,
    launch_request,
)
from atelier2.ports.agent_executions import (
    MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES,
    AgentProcessInvocation,
)

_GRACE_SECONDS = 5.0
_DECLARED_FRAME_BYTES = 4_096
_WAIT = encode_control_frame({"operation": "WAIT"})
_CANCEL = encode_control_frame({"operation": "CANCEL"})
_FINALIZE = encode_control_frame({"operation": "FINALIZE"})


class _FakeProviderGeneration:
    """The provider generation a test hands the coordinator instead of an OS."""

    def __init__(
        self,
        *,
        start_failure: Exception | None = None,
        output_failure: Exception | None = None,
    ) -> None:
        self.launches: list[ProviderLaunch] = []
        self.signals: list[str] = []
        self.exit_code: int | None = None
        self.contained = False
        self.standard_input_open = True
        self.open_streams: set[ProviderStream] = set()
        self.reaps = 0
        self._start_failure = start_failure
        self._output_failure = output_failure

    def start(self, launch: ProviderLaunch) -> None:
        if self._start_failure is not None:
            raise self._start_failure
        self.launches.append(launch)

    def observe_output(self) -> None:
        if self._output_failure is not None:
            raise self._output_failure
        self.open_streams = set(ProviderStream)

    def exit_status(self) -> int | None:
        return self.exit_code

    def reap(self) -> int:
        if self.exit_code is None:
            raise AssertionError("a live provider was reaped")
        self.reaps += 1
        return self.exit_code

    def terminate_group(self) -> bool:
        self.signals.append("TERM")
        return self.exit_code is None

    def kill_group(self) -> None:
        self.signals.append("KILL")

    def close_standard_input(self) -> None:
        self.standard_input_open = False

    def close_output_stream(self, stream: ProviderStream) -> None:
        self.open_streams.discard(stream)

    def close_all_streams(self) -> None:
        self.standard_input_open = False
        self.open_streams.clear()

    def contained_processes_remain(self) -> bool:
        return self.contained

    def kill_contained_processes(self) -> None:
        self.signals.append("CGROUP_KILL")
        self.contained = False


def _launch_frame(
    arguments: tuple[str, ...] = (sys.executable, "-c", "pass"),
    environment: tuple[tuple[str, str], ...] = (),
    standard_input: bytes = b"",
    declared_frame_bytes: int = _DECLARED_FRAME_BYTES,
) -> bytes:
    return encode_control_frame(
        launch_request(
            AgentProcessInvocation(
                arguments,
                Path.cwd(),
                environment,
                standard_input,
                standard_output_frame_bytes=declared_frame_bytes,
            )
        )
    )


def _request(
    coordinator: WatchdogCoordinator, identity: int, frame: bytes, now: float
) -> bytes | None:
    """Deliver one whole request the way the transport would, and read the answer."""

    coordinator.open_channel(identity, now)
    coordinator.receive_request(identity, frame, now)
    coordinator.close_request(identity, now)
    channel = coordinator.channel(identity)
    return None if channel is None else channel.outgoing


def _answer(
    coordinator: WatchdogCoordinator, identity: int, frame: bytes, now: float
) -> bytes | None:
    """One whole exchange: the answer is also flushed, so the role is released."""

    response = _request(coordinator, identity, frame, now)
    if response is not None:
        coordinator.record_response_sent(identity, len(response))
    return response


def _provider_exits(
    coordinator: WatchdogCoordinator,
    provider: _FakeProviderGeneration,
    return_code: int = 0,
) -> None:
    """Both output streams reach their end, then the process itself does."""

    for stream in ProviderStream:
        coordinator.close_provider_output(stream)
    provider.exit_code = return_code


def _launched(
    provider: _FakeProviderGeneration, now: float = 0.0
) -> WatchdogCoordinator:
    coordinator = WatchdogCoordinator(provider, _GRACE_SECONDS)
    assert _answer(coordinator, 1, _launch_frame(), now) == encode_control_frame(
        {"type": "STARTED"}
    )
    return coordinator


def test_a_launch_frame_starts_exactly_the_provider_it_declared() -> None:
    provider = _FakeProviderGeneration()
    coordinator = WatchdogCoordinator(provider, _GRACE_SECONDS)

    answer = _answer(
        coordinator,
        1,
        _launch_frame(("/bin/echo", "hello"), (("NAME", "value"),), b"input"),
        0.0,
    )

    assert answer == encode_control_frame({"type": "STARTED"})
    assert provider.launches == [
        ProviderLaunch(
            ("/bin/echo", "hello"),
            str(Path.cwd()),
            {"NAME": "value"},
            b"input",
            _DECLARED_FRAME_BYTES,
        )
    ]


def test_a_repeated_launch_frame_replays_its_answer_without_starting_twice() -> None:
    provider = _FakeProviderGeneration()
    coordinator = WatchdogCoordinator(provider, _GRACE_SECONDS)
    frame = _launch_frame()

    first = _answer(coordinator, 1, frame, 0.0)
    replayed = _answer(coordinator, 2, frame, 0.1)

    assert replayed is first
    assert len(provider.launches) == 1


def test_a_changed_launch_frame_is_refused_as_a_mismatch() -> None:
    provider = _FakeProviderGeneration()
    coordinator = _launched(provider)

    answer = _answer(coordinator, 2, _launch_frame(("/bin/true",)), 0.1)

    assert answer == encode_control_frame({"type": "LAUNCH_MISMATCH"})
    assert len(provider.launches) == 1


@pytest.mark.parametrize(
    "field",
    (
        {"arguments": []},
        {"working_directory": "relative/path"},
        {"environment": "PATH=/usr/bin"},
        {"standard_input": "not base64"},
        {"standard_output_frame_bytes": 0},
        {"unexpected": "field"},
    ),
    ids=(
        "no-arguments",
        "relative-directory",
        "unstructured-environment",
        "undecodable-input",
        "frameless",
        "unexpected-field",
    ),
)
def test_a_malformed_launch_request_starts_nothing_and_arms_the_waiter(
    field: dict[str, object],
) -> None:
    provider = _FakeProviderGeneration()
    coordinator = WatchdogCoordinator(provider, _GRACE_SECONDS)
    request = launch_request(
        AgentProcessInvocation(
            (sys.executable,),
            Path.cwd(),
            standard_output_frame_bytes=_DECLARED_FRAME_BYTES,
        )
    )
    request.update(field)

    answer = _answer(coordinator, 1, encode_control_frame(request), 0.0)

    assert answer == encode_control_frame(
        {"outcome": "SUPERVISION_FAILED", "type": "TERMINAL_BEFORE_START"}
    )
    assert provider.launches == []
    assert _answer(coordinator, 2, _WAIT, 0.1) == encode_control_frame(
        {"type": "SUPERVISION_FAILED"}
    )


def test_a_provider_that_will_not_start_is_terminal_before_start() -> None:
    provider = _FakeProviderGeneration(start_failure=ProviderStartFailed("no binary"))
    coordinator = WatchdogCoordinator(provider, _GRACE_SECONDS)

    answer = _answer(coordinator, 1, _launch_frame(), 0.0)

    assert answer == encode_control_frame(
        {"outcome": "SUPERVISION_FAILED", "type": "TERMINAL_BEFORE_START"}
    )
    assert _answer(coordinator, 2, _CANCEL, 0.1) == encode_control_frame(
        {
            "disposition": "REAPED_AFTER_PROCESS_BOUNDARY_FAILURE",
            "type": "CANCELLED",
        }
    )


def test_a_provider_whose_output_cannot_be_watched_is_started_then_terminated() -> None:
    provider = _FakeProviderGeneration(
        output_failure=ProviderOutputUnobservable("no pipes")
    )
    coordinator = _launched(provider)

    assert provider.signals == ["TERM"]

    _provider_exits(coordinator, provider)
    coordinator.advance(0.1)

    assert _answer(coordinator, 2, _WAIT, 0.2) == encode_control_frame(
        {"type": "SUPERVISION_FAILED"}
    )


def test_a_completed_provider_publishes_one_completion_every_waiter_replays() -> None:
    provider = _FakeProviderGeneration()
    coordinator = _launched(provider)
    coordinator.receive_provider_output(ProviderStream.STANDARD_OUTPUT, b"out", 0.1)
    coordinator.receive_provider_output(ProviderStream.STANDARD_ERROR, b"err", 0.1)
    _provider_exits(coordinator, provider, 7)

    coordinator.advance(0.2)
    first = _answer(coordinator, 2, _WAIT, 0.3)
    coordinator.advance(0.4)
    replayed = _answer(coordinator, 3, _WAIT, 0.5)

    assert first == encode_control_frame(
        {
            "return_code": 7,
            "standard_error": base64.b64encode(b"err").decode("ascii"),
            "standard_output": base64.b64encode(b"out").decode("ascii"),
            "type": "COMPLETED",
        }
    )
    assert replayed is first
    assert provider.reaps == 1


def test_a_cancel_before_any_launch_stops_the_generation_it_never_started() -> None:
    provider = _FakeProviderGeneration()
    coordinator = WatchdogCoordinator(provider, _GRACE_SECONDS)

    cancelled = _answer(coordinator, 1, _CANCEL, 0.0)

    assert cancelled == encode_control_frame(
        {"disposition": "NEVER_LAUNCHED", "type": "CANCELLED"}
    )
    assert _answer(coordinator, 2, _WAIT, 0.1) == encode_control_frame(
        {"type": "STOPPED"}
    )
    assert _answer(coordinator, 3, _launch_frame(), 0.2) == encode_control_frame(
        {"outcome": "STOPPED", "type": "TERMINAL_BEFORE_START"}
    )
    assert provider.launches == []


def test_a_cancel_terminates_a_running_provider_and_reports_its_completion() -> None:
    provider = _FakeProviderGeneration()
    coordinator = _launched(provider)

    pending = _request(coordinator, 2, _CANCEL, 0.1)

    assert pending is None
    assert provider.signals == ["TERM"]
    assert provider.standard_input_open is False

    _provider_exits(coordinator, provider)
    coordinator.advance(0.2)
    channel = coordinator.channel(2)

    assert channel is not None
    assert channel.outgoing == encode_control_frame(
        {"disposition": "REAPED_AFTER_TERM", "type": "CANCELLED"}
    )
    assert _answer(coordinator, 3, _WAIT, 0.3) == encode_control_frame(
        {
            "return_code": 0,
            "standard_error": "",
            "standard_output": "",
            "type": "COMPLETED",
        }
    )


def test_a_second_supervision_failure_publishes_one_cached_recovery_handoff() -> None:
    provider = _FakeProviderGeneration()
    coordinator = _launched(provider)

    coordinator.report_supervision_failure(1.0)
    coordinator.report_supervision_failure(2.0)
    handed_off = _answer(coordinator, 2, _WAIT, 2.1)
    coordinator.report_supervision_failure(3.0)
    replayed = _answer(coordinator, 3, _CANCEL, 3.1)

    assert handed_off == encode_control_frame({"type": "RECOVERY_HANDOFF"})
    assert replayed is handed_off
    assert provider.open_streams == set()
    assert provider.standard_input_open is False


def test_a_termination_that_outlives_its_grace_escalates_then_hands_off() -> None:
    provider = _FakeProviderGeneration()
    provider.contained = True
    coordinator = _launched(provider)
    _request(coordinator, 2, _CANCEL, 0.0)

    coordinator.advance(_GRACE_SECONDS)
    coordinator.advance(2 * _GRACE_SECONDS + 1)
    channel = coordinator.channel(2)

    assert provider.signals == ["TERM", "CGROUP_KILL", "KILL"]
    assert channel is not None
    assert channel.outgoing == encode_control_frame({"type": "RECOVERY_HANDOFF"})


@pytest.mark.parametrize(
    ("stream", "overflowing_bytes", "declared_frame_bytes"),
    (
        (ProviderStream.STANDARD_OUTPUT, 9, 8),
        (
            ProviderStream.STANDARD_ERROR,
            MAXIMUM_AGENT_PROCESS_STANDARD_ERROR_BYTES + 1,
            _DECLARED_FRAME_BYTES,
        ),
    ),
    ids=("declared-frame", "standard-error"),
)
def test_output_beyond_its_bound_terminates_the_generation_by_that_name(
    stream: ProviderStream, overflowing_bytes: int, declared_frame_bytes: int
) -> None:
    provider = _FakeProviderGeneration()
    coordinator = WatchdogCoordinator(provider, _GRACE_SECONDS)
    _answer(
        coordinator, 1, _launch_frame(declared_frame_bytes=declared_frame_bytes), 0.0
    )

    coordinator.receive_provider_output(stream, b"x" * overflowing_bytes, 0.1)

    assert provider.signals == ["TERM"]
    assert provider.standard_input_open is False

    _provider_exits(coordinator, provider)
    coordinator.advance(0.2)

    assert _answer(coordinator, 2, _WAIT, 0.3) == encode_control_frame(
        {"type": "OUTPUT_LIMIT_EXCEEDED"}
    )
    assert _answer(coordinator, 3, _CANCEL, 0.4) == encode_control_frame(
        {
            "disposition": "REAPED_AFTER_PROCESS_BOUNDARY_FAILURE",
            "type": "CANCELLED",
        }
    )


def test_provider_output_before_its_declared_frame_is_refused_loudly() -> None:
    coordinator = WatchdogCoordinator(_FakeProviderGeneration(), _GRACE_SECONDS)

    with pytest.raises(RuntimeError, match="declared frame"):
        coordinator.receive_provider_output(
            ProviderStream.STANDARD_OUTPUT, b"early", 0.0
        )


def test_a_lost_owner_before_any_launch_ends_the_generation() -> None:
    coordinator = WatchdogCoordinator(_FakeProviderGeneration(), _GRACE_SECONDS)

    coordinator.owner_lost(0.0)

    assert coordinator.finished is True


def test_a_lost_owner_ends_the_generation_only_once_its_provider_is_reaped() -> None:
    provider = _FakeProviderGeneration()
    coordinator = _launched(provider)

    coordinator.owner_lost(0.1)

    assert coordinator.finished is False
    assert provider.signals == ["TERM"]

    _provider_exits(coordinator, provider)
    coordinator.advance(0.2)

    assert coordinator.finished is True


def test_each_control_role_admits_one_peer_and_refuses_the_next_as_busy() -> None:
    coordinator = _launched(_FakeProviderGeneration())
    busy = encode_control_frame({"type": "BUSY"})
    for identity, frame in ((2, _launch_frame()), (3, _WAIT), (4, _CANCEL)):
        _request(coordinator, identity, frame, 0.1)

    contenders = {
        "LAUNCH": _request(coordinator, 5, _launch_frame(), 0.2),
        "WAIT": _request(coordinator, 6, _WAIT, 0.2),
        "FINALIZE": _request(coordinator, 7, _FINALIZE, 0.2),
    }

    assert contenders == {"LAUNCH": busy, "WAIT": busy, "FINALIZE": busy}


def test_an_unclassified_peer_holds_its_role_and_the_next_is_refused() -> None:
    coordinator = _launched(_FakeProviderGeneration())
    coordinator.open_channel(2, 0.1)

    assert _request(coordinator, 3, _WAIT, 0.2) == encode_control_frame(
        {"type": "BUSY"}
    )


def test_a_request_that_never_finishes_is_refused_when_its_frame_times_out() -> None:
    coordinator = _launched(_FakeProviderGeneration())
    coordinator.open_channel(2, 0.1)
    coordinator.receive_request(2, b'{"operation"', 0.1)

    coordinator.advance(0.1 + CONTROL_FRAME_TIMEOUT_SECONDS)
    channel = coordinator.channel(2)

    assert channel is not None
    assert channel.outgoing == encode_control_frame({"type": "CONTROL_FRAME_TIMEOUT"})


@pytest.mark.parametrize(
    "frame",
    (
        b'{"operation": "WAIT"}',
        b'{"operation":"RESUME"}',
        b'["WAIT"]',
        b"{",
        b'{"operation":"WA\xc3\x9cT"}',
    ),
    ids=("noncanonical", "unknown-operation", "not-an-object", "not-json", "not-ascii"),
)
def test_a_request_that_is_not_an_exact_control_frame_is_refused(frame: bytes) -> None:
    coordinator = _launched(_FakeProviderGeneration())

    assert _answer(coordinator, 2, frame, 0.1) == encode_control_frame(
        {"type": "MALFORMED"}
    )


def test_a_request_beyond_the_launch_bound_is_refused_before_it_completes() -> None:
    coordinator = _launched(_FakeProviderGeneration())
    coordinator.open_channel(2, 0.1)

    coordinator.receive_request(2, b"x" * (MAXIMUM_AGENT_LAUNCH_REQUEST_BYTES + 1), 0.1)
    channel = coordinator.channel(2)

    assert channel is not None
    assert channel.outgoing == encode_control_frame({"type": "FRAME_TOO_LARGE"})
    assert coordinator.receive_budget(2) == 0


def test_a_response_nobody_reads_is_dropped_and_replayed_to_the_next_peer() -> None:
    provider = _FakeProviderGeneration()
    coordinator = _launched(provider)
    _provider_exits(coordinator, provider)
    coordinator.advance(0.1)
    stalled = _request(coordinator, 2, _WAIT, 0.2)
    coordinator.drain_closed_channels()

    coordinator.advance(0.2 + CONTROL_FRAME_TIMEOUT_SECONDS)

    assert coordinator.drain_closed_channels() == (2,)
    assert coordinator.channel(2) is None
    assert _answer(coordinator, 3, _WAIT, 0.4) is stalled


def test_finalization_is_refused_before_a_terminal_and_accepted_after_it() -> None:
    provider = _FakeProviderGeneration()
    coordinator = _launched(provider)

    assert _answer(coordinator, 2, _FINALIZE, 0.1) == encode_control_frame(
        {"type": "FINALIZE_REFUSED"}
    )

    _provider_exits(coordinator, provider)
    coordinator.advance(0.2)
    accepted = _answer(coordinator, 3, _FINALIZE, 0.3)

    assert accepted == encode_control_frame({"type": "FINALIZE_ACCEPTED"})
    assert _answer(coordinator, 4, _FINALIZE, 0.4) is accepted


def test_an_abandoned_channel_frees_its_role_for_the_next_peer() -> None:
    coordinator = _launched(_FakeProviderGeneration())
    _request(coordinator, 2, _WAIT, 0.1)

    coordinator.abandon_channel(2)

    assert coordinator.channel(2) is None
    assert _request(coordinator, 3, _WAIT, 0.2) is None
