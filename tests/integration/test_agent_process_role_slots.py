from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from atelier2.adapters import agent_process_watchdog as watchdog_module
from atelier2.adapters.agent_process_watchdog import Watchdog, encode_control_frame


def test_four_control_roles_are_independently_single_slot_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = tmp_path / "control.sock"
    owner_pipe, owner_writer = os.pipe()
    watchdog = Watchdog(endpoint, tmp_path / "cgroup", owner_pipe, 0.1)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(endpoint))
    server.listen()
    server.setblocking(False)
    watchdog._server = server
    clients: list[socket.socket] = []
    monkeypatch.setattr(watchdog, "_handle_launch", lambda *_arguments: None)
    monkeypatch.setattr(watchdog, "_handle_wait", lambda *_arguments: None)
    monkeypatch.setattr(watchdog, "_handle_cancel", lambda *_arguments: None)

    def admit(frame: bytes | None) -> watchdog_module._Connection:
        before = set(watchdog._connections)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        clients.append(client)
        client.connect(str(endpoint))
        if frame is not None:
            client.sendall(frame)
            client.shutdown(socket.SHUT_WR)
        watchdog._accept_connection(1.0)
        descriptor = (set(watchdog._connections) - before).pop()
        connection = watchdog._connections[descriptor]
        if frame is not None and connection.output_bytes is None:
            watchdog._read_connection(connection, 1.0)
            watchdog._read_connection(connection, 1.0)
        return connection

    try:
        held = tuple(
            admit(encode_control_frame({"operation": operation}))
            for operation in ("LAUNCH", "WAIT", "CANCEL")
        )
        unclassified = admit(None)

        assert set(watchdog._slots) == {
            "LAUNCH_RETRY",
            "WAIT",
            "TERMINAL_CONTROL",
            "UNCLASSIFIED",
        }
        busy_unclassified = admit(None)
        assert busy_unclassified.output_bytes == encode_control_frame({"type": "BUSY"})
        watchdog._close_connection(busy_unclassified)
        watchdog._close_connection(unclassified)

        for operation in ("LAUNCH", "WAIT", "FINALIZE"):
            competing = admit(encode_control_frame({"operation": operation}))
            assert competing.output_bytes == encode_control_frame({"type": "BUSY"})
            watchdog._close_connection(competing)

        for connection in held:
            watchdog._close_connection(connection)
    finally:
        for connection in tuple(watchdog._connections.values()):
            watchdog._close_connection(connection)
        for client in clients:
            client.close()
        server.close()
        watchdog._selector.close()
        endpoint.unlink(missing_ok=True)
        os.close(owner_pipe)
        os.close(owner_writer)
