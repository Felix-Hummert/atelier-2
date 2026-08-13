from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


class Watchdog:
    def __init__(
        self, endpoint: Path, cgroup: Path, owner_pipe: int, grace: float
    ) -> None:
        self._endpoint = endpoint
        self._cgroup = cgroup
        self._owner_pipe = owner_pipe
        self._grace = grace
        self._lock = threading.Lock()
        self._cancellation_complete = threading.Event()
        self._completion_ready = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._completion: dict[str, object] | None = None
        self._disposition: str | None = None
        self._cancellation_started = False

    def serve(self) -> None:
        self._endpoint.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._endpoint.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(self._endpoint))
            os.chmod(self._endpoint, 0o600)
            server.listen()
            threading.Thread(target=self._watch_owner, daemon=True).start()
            print("READY", flush=True)
            while True:
                connection, _address = server.accept()
                threading.Thread(
                    target=self._respond, args=(connection,), daemon=True
                ).start()

    def _watch_owner(self) -> None:
        try:
            while os.read(self._owner_pipe, 1):
                pass
        finally:
            os.close(self._owner_pipe)
        try:
            self._cancel()
        finally:
            # The dead owner's durable cancellation may not yet be attested.
            # Leave both witness paths for the recovering supervisor; release()
            # removes them only after the terminal store commit.
            os._exit(0)

    def _respond(self, connection: socket.socket) -> None:
        with connection:
            try:
                request = json.loads(_read_all(connection).decode("utf-8"))
                operation = request["operation"]
                if operation == "LAUNCH":
                    response = self._launch(request)
                elif operation == "WAIT":
                    self._completion_ready.wait()
                    response = self._completion
                elif operation == "CANCEL":
                    response = {"disposition": self._cancel()}
                elif operation == "CLOSE":
                    response = {"disposition": self._close_without_launch()}
                else:
                    raise ValueError("unknown watchdog operation")
            except (
                KeyError,
                OSError,
                RuntimeError,
                subprocess.SubprocessError,
                TypeError,
                ValueError,
            ) as error:
                response = {"error": type(error).__name__}
            connection.sendall(json.dumps(response, separators=(",", ":")).encode())

    def _launch(self, request: dict[str, Any]) -> dict[str, object]:
        with self._lock:
            if (
                self._process is not None
                or self._disposition is not None
                or self._cancellation_started
            ):
                raise RuntimeError("watchdog generation is already closed or launched")
            arguments = tuple(str(value) for value in request["arguments"])
            environment = {
                str(name): str(value) for name, value in request["environment"]
            }
            guarded = (
                sys.executable,
                "-m",
                "atelier2.adapters.agent_process_exec_guard",
                "--cgroup",
                str(self._cgroup),
                "--watchdog-pid",
                str(os.getpid()),
                "--",
                *arguments,
            )
            process = subprocess.Popen(
                guarded,
                cwd=str(request["working_directory"]),
                env={
                    **os.environ,
                    "ATELIER2_AGENT_ENVIRONMENT_B64": base64.b64encode(
                        json.dumps(
                            sorted(environment.items()), separators=(",", ":")
                        ).encode("utf-8")
                    ).decode("ascii"),
                },
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            self._process = process
            standard_input = base64.b64decode(request["standard_input"], validate=True)
            threading.Thread(
                target=self._collect, args=(process, standard_input), daemon=True
            ).start()
            return {"launched": True}

    def _collect(self, process: subprocess.Popen[bytes], standard_input: bytes) -> None:
        output, error = process.communicate(standard_input)
        with self._lock:
            self._completion = {
                "return_code": int(process.returncode),
                "standard_output": base64.b64encode(output).decode("ascii"),
                "standard_error": base64.b64encode(error).decode("ascii"),
            }
            self._completion_ready.set()

    def _close_without_launch(self) -> str:
        with self._lock:
            if self._process is not None:
                raise RuntimeError(
                    "launched watchdog cannot be closed as never launched"
                )
            self._disposition = self._disposition or "NEVER_LAUNCHED"
            return self._disposition

    def _cancel(self) -> str:
        with self._lock:
            if self._disposition is not None:
                return self._disposition
            if self._cancellation_started:
                cancellation_owner = False
            else:
                self._cancellation_started = True
                cancellation_owner = True
            process = self._process
        if not cancellation_owner:
            if not self._cancellation_complete.wait(
                timeout=max(1.0, (self._grace * 2) + 1.0)
            ):
                raise RuntimeError("watchdog cancellation did not finish in bounds")
            with self._lock:
                if self._disposition is None:
                    raise RuntimeError("watchdog cancellation finished without proof")
                return self._disposition
        try:
            if process is None:
                disposition = "NEVER_LAUNCHED"
            elif (
                process.poll() is not None
                and self._completion_ready.is_set()
                and not _cgroup_populated(self._cgroup)
            ):
                disposition = "EXITED_BEFORE_SIGNAL"
            else:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                if _wait_for_cleanup_proof(
                    self._cgroup, self._completion_ready, self._grace
                ):
                    disposition = "REAPED_AFTER_TERM"
                else:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    (self._cgroup / "cgroup.kill").write_text("1", encoding="ascii")
                    if not _wait_for_cleanup_proof(
                        self._cgroup,
                        self._completion_ready,
                        max(1.0, self._grace),
                    ):
                        raise RuntimeError(
                            "watchdog could not prove bounded cgroup cleanup"
                        )
                    disposition = "REAPED_AFTER_KILL"
        except (OSError, RuntimeError):
            with self._lock:
                self._cancellation_started = False
            raise
        with self._lock:
            self._disposition = disposition
            self._cancellation_complete.set()
            return disposition


def _wait_for_cleanup_proof(
    cgroup: Path, completion_ready: threading.Event, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if completion_ready.is_set() and not _cgroup_populated(cgroup):
            return True
        time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))
    return completion_ready.is_set() and not _cgroup_populated(cgroup)


def _cgroup_populated(cgroup: Path) -> bool:
    events = (cgroup / "cgroup.events").read_text(encoding="ascii").splitlines()
    return "populated 1" in events


def _read_all(connection: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = connection.recv(65_536)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def main(arguments: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="atelier2-agent-watchdog")
    parser.add_argument("--endpoint", type=Path, required=True)
    parser.add_argument("--cgroup", type=Path, required=True)
    parser.add_argument("--owner-pipe", type=int, required=True)
    parser.add_argument("--grace", type=float, required=True)
    parsed = parser.parse_args(arguments)
    Watchdog(parsed.endpoint, parsed.cgroup, parsed.owner_pipe, parsed.grace).serve()


if __name__ == "__main__":
    main()
