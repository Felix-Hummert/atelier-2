from __future__ import annotations

import argparse
import os
import selectors
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from atelier2.adapters.agent_process_coordinator import (
    ProviderOutputUnobservable,
    ProviderStartFailed,
    ProviderStream,
    WatchdogCoordinator,
)
from atelier2.adapters.agent_process_protocol import (
    PROVIDER_ENVIRONMENT_CHANNEL,
    ProviderLaunch,
    cgroup_populated,
    encode_provider_environment,
)

EXEC_GUARD_MODULE = "atelier2.adapters.agent_process_exec_guard"
_SERVER = "server"
_OWNER = "owner"
_STANDARD_INPUT = "stdin"
_READ_CHUNK_BYTES = 65_536


def _announce_ready_on_standard_output() -> None:
    print("READY", flush=True)


@dataclass
class _ControlSocket:
    """One accepted control peer, and the coordinator channel it speaks for."""

    identity: int
    connection: socket.socket


class _GuardedProviderProcess:
    """One provider generation as processes, pipes, and a single cgroup."""

    def __init__(self, cgroup: Path, selector: selectors.BaseSelector) -> None:
        self._cgroup = cgroup
        self._selector = selector
        self._process: subprocess.Popen[bytes] | None = None
        self._standard_input = b""
        self._standard_input_offset = 0
        self._streams: dict[int, str] = {}

    def start(self, launch: ProviderLaunch) -> None:
        guarded = (
            sys.executable,
            "-m",
            EXEC_GUARD_MODULE,
            "--cgroup",
            str(self._cgroup),
            "--watchdog-pid",
            str(os.getpid()),
            "--",
            *launch.arguments,
        )
        try:
            process = subprocess.Popen(
                guarded,
                cwd=launch.working_directory,
                env={
                    **os.environ,
                    PROVIDER_ENVIRONMENT_CHANNEL: encode_provider_environment(
                        launch.environment
                    ),
                },
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError, TypeError, ValueError) as error:
            raise ProviderStartFailed("the guarded provider did not start") from error
        self._process = process
        self._standard_input = launch.standard_input

    def observe_output(self) -> None:
        process = self._require_process()
        try:
            if (
                process.stdin is None
                or process.stdout is None
                or process.stderr is None
            ):
                raise ProviderOutputUnobservable("provider pipes are absent")
            for stream, role, events in (
                (process.stdin, _STANDARD_INPUT, selectors.EVENT_WRITE),
                (
                    process.stdout,
                    ProviderStream.STANDARD_OUTPUT.value,
                    selectors.EVENT_READ,
                ),
                (
                    process.stderr,
                    ProviderStream.STANDARD_ERROR.value,
                    selectors.EVENT_READ,
                ),
            ):
                descriptor = stream.fileno()
                os.set_blocking(descriptor, False)
                self._streams[descriptor] = role
                self._selector.register(descriptor, events, role)
        except (KeyError, OSError, ValueError) as error:
            raise ProviderOutputUnobservable(
                "provider output could not be watched"
            ) from error
        if not self._standard_input:
            self.close_standard_input()

    def exit_status(self) -> int | None:
        return self._require_process().poll()

    def reap(self) -> int:
        return self._require_process().wait()

    def terminate_group(self) -> bool:
        try:
            os.killpg(self._require_process().pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True

    def kill_group(self) -> None:
        try:
            os.killpg(self._require_process().pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def contained_processes_remain(self) -> bool:
        return cgroup_populated(self._cgroup)

    def kill_contained_processes(self) -> None:
        (self._cgroup / "cgroup.kill").write_text("1", encoding="ascii")

    def close_standard_input(self) -> None:
        self._standard_input = b""
        self._close_stream(_STANDARD_INPUT)

    def close_output_stream(self, stream: ProviderStream) -> None:
        self._close_stream(stream.value)

    def close_all_streams(self) -> None:
        for role in tuple(self._streams.values()):
            self._close_stream(role)

    def write_standard_input(self, descriptor: int) -> None:
        try:
            written = os.write(
                descriptor, self._standard_input[self._standard_input_offset :]
            )
        except BlockingIOError:
            return
        self._standard_input_offset += written
        if self._standard_input_offset == len(self._standard_input):
            self.close_standard_input()

    def read_output(self, descriptor: int) -> bytes | None:
        """The next output chunk, empty at end of stream, None when it would block."""

        try:
            return os.read(descriptor, _READ_CHUNK_BYTES)
        except BlockingIOError:
            return None

    def _require_process(self) -> subprocess.Popen[bytes]:
        process = self._process
        if process is None:
            raise RuntimeError("the provider generation was never started")
        return process

    def _close_stream(self, role: str) -> None:
        descriptor = next(
            (fd for fd, current in self._streams.items() if current == role),
            None,
        )
        if descriptor is None:
            return
        self._streams.pop(descriptor, None)
        try:
            self._selector.unregister(descriptor)
        except (KeyError, ValueError):
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass


class Watchdog:
    """The selector shell around one coordinator: sockets in, decisions out."""

    def __init__(
        self,
        endpoint: Path,
        cgroup: Path,
        owner_pipe: int,
        grace: float,
    ) -> None:
        self._endpoint = endpoint
        self._owner_pipe = owner_pipe
        self._selector = selectors.DefaultSelector()
        self._server: socket.socket | None = None
        self._sockets: dict[int, _ControlSocket] = {}
        self._provider = _GuardedProviderProcess(cgroup, self._selector)
        self._coordinator = WatchdogCoordinator(self._provider, grace)

    def serve(self, announce_ready: Callable[[], None]) -> None:
        self._endpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._endpoint.exists():
            raise RuntimeError("watchdog endpoint already exists")
        os.set_blocking(self._owner_pipe, False)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server = server
        try:
            server.bind(str(self._endpoint))
            os.chmod(self._endpoint, 0o600)
            server.listen()
            server.setblocking(False)
            self._selector.register(server, selectors.EVENT_READ, _SERVER)
            self._selector.register(self._owner_pipe, selectors.EVENT_READ, _OWNER)
            announce_ready()
            while not self._coordinator.finished:
                try:
                    self._tick()
                except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
                    self._coordinator.report_supervision_failure(time.monotonic())
        finally:
            self._provider.close_all_streams()
            for identity in tuple(self._sockets):
                self._close_socket(identity)
            if self._server is not None:
                try:
                    self._selector.unregister(self._server)
                except (KeyError, ValueError):
                    pass
                self._server.close()
            try:
                self._selector.unregister(self._owner_pipe)
            except (KeyError, ValueError):
                pass
            os.close(self._owner_pipe)
            self._selector.close()

    def _tick(self) -> None:
        now = time.monotonic()
        self._coordinator.advance(now)
        self._apply_decisions()
        events = self._selector.select(self._coordinator.next_deadline(now))
        for key, mask in events:
            if key.data == _SERVER:
                self._accept_control(now)
            elif key.data == _OWNER:
                self._read_owner(now)
            elif isinstance(key.data, _ControlSocket):
                self._service_control(key.data, mask, now)
            else:
                self._service_provider(int(key.fd), str(key.data), now)
        self._apply_decisions()

    def _apply_decisions(self) -> None:
        for identity in self._coordinator.drain_channels_awaiting_write():
            control = self._sockets.get(identity)
            if control is None:
                continue
            try:
                self._selector.modify(
                    control.connection, selectors.EVENT_WRITE, control
                )
            except (KeyError, ValueError):
                self._coordinator.abandon_channel(identity)
                self._close_socket(identity)
        for identity in self._coordinator.drain_closed_channels():
            self._close_socket(identity)

    def _accept_control(self, now: float) -> None:
        if self._server is None:
            return
        try:
            connection, _address = self._server.accept()
        except BlockingIOError:
            return
        connection.setblocking(False)
        control = _ControlSocket(connection.fileno(), connection)
        self._sockets[control.identity] = control
        self._coordinator.open_channel(control.identity, now)
        self._selector.register(connection, selectors.EVENT_READ, control)

    def _read_owner(self, now: float) -> None:
        try:
            data = os.read(self._owner_pipe, 1)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            return
        try:
            self._selector.unregister(self._owner_pipe)
        except (KeyError, ValueError):
            pass
        self._coordinator.owner_lost(now)

    def _service_control(self, control: _ControlSocket, mask: int, now: float) -> None:
        if self._coordinator.channel(control.identity) is None:
            return
        if mask & selectors.EVENT_READ:
            self._read_control(control, now)
        if (
            self._coordinator.channel(control.identity) is not None
            and mask & selectors.EVENT_WRITE
        ):
            self._write_control(control)

    def _read_control(self, control: _ControlSocket, now: float) -> None:
        budget = self._coordinator.receive_budget(control.identity)
        try:
            chunk = control.connection.recv(max(1, min(_READ_CHUNK_BYTES, budget)))
        except BlockingIOError:
            return
        except OSError:
            self._abandon_control(control.identity)
            return
        if chunk:
            self._coordinator.receive_request(control.identity, chunk, now)
            return
        self._coordinator.close_request(control.identity, now)

    def _write_control(self, control: _ControlSocket) -> None:
        channel = self._coordinator.channel(control.identity)
        if channel is None or channel.outgoing is None:
            return
        try:
            sent = control.connection.send(channel.outgoing[channel.sent_bytes :])
        except BlockingIOError:
            return
        except OSError:
            self._abandon_control(control.identity)
            return
        self._coordinator.record_response_sent(control.identity, sent)

    def _service_provider(self, descriptor: int, role: str, now: float) -> None:
        try:
            if role == _STANDARD_INPUT:
                self._provider.write_standard_input(descriptor)
            else:
                self._read_provider_output(descriptor, ProviderStream(role), now)
        except BrokenPipeError:
            if role == _STANDARD_INPUT:
                self._provider.close_standard_input()
            else:
                self._coordinator.report_provider_failure(now)
        except OSError:
            self._coordinator.report_provider_failure(now)

    def _read_provider_output(
        self, descriptor: int, stream: ProviderStream, now: float
    ) -> None:
        chunk = self._provider.read_output(descriptor)
        if chunk is None:
            return
        if not chunk:
            self._coordinator.close_provider_output(stream)
            return
        self._coordinator.receive_provider_output(stream, chunk, now)

    def _abandon_control(self, identity: int) -> None:
        self._coordinator.abandon_channel(identity)
        self._close_socket(identity)

    def _close_socket(self, identity: int) -> None:
        control = self._sockets.pop(identity, None)
        if control is None:
            return
        try:
            self._selector.unregister(control.connection)
        except (KeyError, ValueError):
            pass
        control.connection.close()


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="atelier2-agent-watchdog")
    parser.add_argument("--endpoint", type=Path, required=True)
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--owner-pipe", type=int, required=True)
    parser.add_argument("--grace", type=float, required=True)
    parsed = parser.parse_args(arguments)
    Watchdog(
        parsed.endpoint,
        parsed.cgroup,
        parsed.owner_pipe,
        parsed.grace,
    ).serve(_announce_ready_on_standard_output)


if __name__ == "__main__":
    main()
