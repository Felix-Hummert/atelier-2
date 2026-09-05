"""The terminal seat's lifecycle: one tmux session per project, outside serve.

A seat is a tmux session running the agent CLI, held by a transient systemd
user scope so that stopping the serve unit cannot take it down, and reached
through a ttyd child that the serve owns. Both halves live here because they
are one lifecycle: `ensure_session` and `stop_session` decide about the session
that outlives the serve, `ttyd_command` builds the child the serve starts and
stops with itself. The tmux socket is the truth about a seat; the unit name
only holds its process tree.

Nothing here reads, forwards, or stores terminal content: no `pipe-pane`, no
`capture-pane`, no seat bytes into journal, events, or receipts. The commands
composed here are management commands, and only their exit code and their own
error channel are read.
"""

from __future__ import annotations

import json
import secrets
import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol, Self

from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.host.mcp_tools import MCP_SERVER_NAME

# The seat binds loopback unconditionally, unlike the API's configurable host:
# a pty is a shell, and the machine itself is the whole trust boundary.
SEAT_BIND_ADDRESS = "127.0.0.1"
DEFAULT_SEAT_PORT = 7681
SEAT_PATH_TOKEN_BYTES = 24
HIGHEST_PORT = 65535
SYSTEMD_RUN_PROGRAM = "systemd-run"
SYSTEMCTL_PROGRAM = "systemctl"
SCOPE_UNIT_SUFFIX = ".scope"


class TerminalSeatCommandFailed(RuntimeError):
    """A management command the seat issued did not succeed."""


class TerminalSeatOutcome(Enum):
    """What one seat lifecycle call did, or why it did nothing."""

    CREATED = "created"
    ALREADY_RUNNING = "already-running"
    RECREATED_AFTER_ORPHANED_SCOPE = "recreated-after-orphaned-scope"
    STOPPED = "stopped"
    NOT_RUNNING = "not-running"
    UNUSABLE_PROJECT_ROOT_MISSING = "unusable-project-root-missing"
    REFUSED_SYSTEMD_MISSING = "refused-systemd-missing"
    REFUSED_PORT_BUSY = "refused-port-busy"


@dataclass(frozen=True, slots=True)
class SeatCommandResult:
    """What a management command answered: never terminal content."""

    exit_code: int
    stderr: str = ""


class TerminalSeatHost(Protocol):
    """The machine a seat lives on, as narrowly as the seat needs it."""

    def run(self, argv: Sequence[str]) -> SeatCommandResult: ...

    def locate_executable(self, program: str) -> Path | None: ...

    def loopback_port_is_free(self, port: int) -> bool: ...


@dataclass(frozen=True, slots=True)
class TerminalSeatSettings:
    """Everything one project's seat is composed from."""

    project_id: ProjectId
    project_root: Path
    database_path: str
    state_directory: Path
    tmux_executable: Path
    ttyd_executable: Path
    claude_executable: Path
    mcp_door_command: tuple[str, ...]
    port: int = DEFAULT_SEAT_PORT

    def __post_init__(self) -> None:
        if not 1 <= self.port <= HIGHEST_PORT:
            raise ValueError(f"a seat port must be 1..{HIGHEST_PORT}, not {self.port}")
        if not self.mcp_door_command:
            raise ValueError("a seat needs the command its MCP door is spawned with")

    @property
    def session_name(self) -> str:
        """The tmux session of this project, under the full project digest."""

        return f"seat-{Sha256Hash.of(self.project_id.value.encode('utf-8')).value}"

    @property
    def socket_name(self) -> str:
        """The tmux socket of this deployment, so two serves never share a seat."""

        return f"atelier-seat-{Sha256Hash.of(self.database_path.encode('utf-8')).value}"

    @property
    def unit_name(self) -> str:
        """The transient systemd user unit holding this seat's process tree."""

        return f"atelier2-{self.session_name}"

    @property
    def scope_unit(self) -> str:
        return f"{self.unit_name}{SCOPE_UNIT_SUFFIX}"


@dataclass(frozen=True, slots=True)
class TerminalSeat:
    """One project's seat, addressed under a base path this serve drew."""

    settings: TerminalSeatSettings
    host: TerminalSeatHost
    path_token: str

    @classmethod
    def for_serve(cls, settings: TerminalSeatSettings, host: TerminalSeatHost) -> Self:
        """A seat under a fresh base path, unguessable to a page that was not told it."""

        return cls(settings, host, secrets.token_urlsafe(SEAT_PATH_TOKEN_BYTES))

    @property
    def base_path(self) -> str:
        return f"/seat-{self.path_token}"

    @property
    def url(self) -> str:
        """Where a browser on this machine reaches the seat."""

        return f"http://{SEAT_BIND_ADDRESS}:{self.settings.port}{self.base_path}/"

    def ensure_session(self) -> TerminalSeatOutcome:
        """Create the seat's session, or find the one that is already running."""

        if not self.settings.project_root.is_dir():
            return TerminalSeatOutcome.UNUSABLE_PROJECT_ROOT_MISSING
        systemd_run = self.host.locate_executable(SYSTEMD_RUN_PROGRAM)
        systemctl = self.host.locate_executable(SYSTEMCTL_PROGRAM)
        if systemd_run is None or systemctl is None:
            return TerminalSeatOutcome.REFUSED_SYSTEMD_MISSING
        if not self.host.loopback_port_is_free(self.settings.port):
            return TerminalSeatOutcome.REFUSED_PORT_BUSY
        if self._session_is_alive():
            return TerminalSeatOutcome.ALREADY_RUNNING
        orphaned_scope = self._scope_is_active(systemctl)
        if orphaned_scope:
            self._stop_scope(systemctl)
        self._create_session(systemd_run)
        return (
            TerminalSeatOutcome.RECREATED_AFTER_ORPHANED_SCOPE
            if orphaned_scope
            else TerminalSeatOutcome.CREATED
        )

    def stop_session(self) -> TerminalSeatOutcome:
        """End the seat itself, which is what `atelier2 seat stop` means.

        Stopping the serve ends only the ttyd child; the session and the agent
        in it keep running in their own scope until this is called.
        """

        systemctl = self.host.locate_executable(SYSTEMCTL_PROGRAM)
        if systemctl is None:
            return TerminalSeatOutcome.REFUSED_SYSTEMD_MISSING
        alive = self._session_is_alive()
        if alive:
            self._run_checked(
                self._tmux_command("kill-session", "-t", self._session_target)
            )
        active_scope = self._scope_is_active(systemctl)
        if active_scope:
            self._stop_scope(systemctl)
        if alive or active_scope:
            return TerminalSeatOutcome.STOPPED
        return TerminalSeatOutcome.NOT_RUNNING

    def ttyd_command(self) -> tuple[str, ...]:
        """The serve's ttyd child: loopback, writable, origin-checked, no login.

        `-O` refuses a WebSocket whose origin is not ttyd's own page, which is
        the origin the seat's iframe carries; the drawn base path is what a page
        that was never told it cannot reach, since a rebound name passes the
        origin check. `-c` and `-H` stay absent: the seat asks for no password.
        """

        return (
            str(self.settings.ttyd_executable),
            "-i",
            SEAT_BIND_ADDRESS,
            "-p",
            str(self.settings.port),
            "-W",
            "-O",
            "-b",
            self.base_path,
            *self._tmux_command("attach-session", "-t", self._session_target),
        )

    @property
    def _session_target(self) -> str:
        """The exact-name form, so no other session answers for this seat."""

        return f"={self.settings.session_name}"

    def _tmux_command(self, *arguments: str) -> tuple[str, ...]:
        return (
            str(self.settings.tmux_executable),
            "-L",
            self.settings.socket_name,
            *arguments,
        )

    def _session_is_alive(self) -> bool:
        answer = self.host.run(
            self._tmux_command("has-session", "-t", self._session_target)
        )
        return answer.exit_code == 0

    def _stop_scope(self, systemctl: Path) -> None:
        self._run_checked((str(systemctl), "--user", "stop", self.settings.scope_unit))

    def _scope_is_active(self, systemctl: Path) -> bool:
        answer = self.host.run(
            (str(systemctl), "--user", "is-active", self.settings.scope_unit)
        )
        return answer.exit_code == 0

    def _create_session(self, systemd_run: Path) -> None:
        configuration = self._write_mcp_configuration()
        self._run_checked(
            (
                str(systemd_run),
                "--user",
                "--scope",
                "--collect",
                f"--unit={self.settings.unit_name}",
                *self._tmux_command(
                    "new-session",
                    "-d",
                    "-s",
                    self.settings.session_name,
                    "-c",
                    str(self.settings.project_root),
                ),
            )
        )
        self._type_into_login_shell(
            (
                str(self.settings.claude_executable),
                "--mcp-config",
                str(configuration),
            )
        )

    def _type_into_login_shell(self, argv: Sequence[str]) -> None:
        """Type the agent CLI where the operator would type it.

        The CLI is not the session's command: ending it leaves the login shell
        standing with its prompt, and nothing restarts it.
        """

        typed = self._tmux_command(
            "send-keys", "-t", self._session_target, "-l", shlex.join(argv)
        )
        self._run_checked(typed)
        self._run_checked(
            self._tmux_command("send-keys", "-t", self._session_target, "Enter")
        )

    def _write_mcp_configuration(self) -> Path:
        """Write the seat's MCP document where the serve keeps its state.

        Never into the operator's project tree, and naming exactly one server:
        this deployment's own loopback door, with no other key to carry.
        """

        document = self.settings.state_directory / f"{self.settings.session_name}.json"
        document.parent.mkdir(parents=True, exist_ok=True)
        command, *arguments = self.settings.mcp_door_command
        document.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        MCP_SERVER_NAME: {"command": command, "args": arguments}
                    }
                },
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        return document

    def _run_checked(self, argv: Sequence[str]) -> None:
        answer = self.host.run(argv)
        if answer.exit_code != 0:
            raise TerminalSeatCommandFailed(
                f"{shlex.join(argv)} exited {answer.exit_code}: {answer.stderr}"
            )
