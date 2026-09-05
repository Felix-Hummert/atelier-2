from __future__ import annotations

import hashlib
import json
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from atelier2.contracts.host_configuration import ProjectId
from atelier2.host.mcp_tools import MCP_SERVER_NAME
from atelier2.host.terminal_seat import (
    SeatCommandResult,
    TerminalSeat,
    TerminalSeatCommandFailed,
    TerminalSeatOutcome,
    TerminalSeatSettings,
)

PROJECT_ID = ProjectId("atelier zwei")
DATABASE_PATH = "/var/lib/atelier2/atelier.db"
DOOR_COMMAND = ("/usr/bin/python3", "-m", "atelier2", "mcp", "--service")
SERVICE_URL = "http://127.0.0.1:8422"
PATH_TOKEN = "eeGh2Fp0Q"
SEAT_PORT = 7681


def digest_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class FakeSeatHost:
    """A machine that only remembers what was asked of it."""

    programs: dict[str, Path] = field(
        default_factory=lambda: {
            "systemd-run": Path("/usr/bin/systemd-run"),
            "systemctl": Path("/usr/bin/systemctl"),
        }
    )
    session_alive: bool = False
    scope_active: bool = False
    free_port: bool = True
    exit_codes: dict[str, int] = field(default_factory=dict)
    commands: list[tuple[str, ...]] = field(default_factory=list)

    def run(self, argv: Sequence[str]) -> SeatCommandResult:
        command = tuple(argv)
        self.commands.append(command)
        failure = next(
            (code for word, code in self.exit_codes.items() if word in command), 0
        )
        if failure != 0:
            return SeatCommandResult(failure, "refused by the fake host")
        if "has-session" in command:
            return SeatCommandResult(0 if self.session_alive else 1)
        if "is-active" in command:
            return SeatCommandResult(0 if self.scope_active else 3)
        if "new-session" in command:
            self.session_alive = True
            self.scope_active = True
        if "kill-session" in command:
            self.session_alive = False
        if "stop" in command:
            self.scope_active = False
        return SeatCommandResult(0)

    def locate_executable(self, program: str) -> Path | None:
        return self.programs.get(program)

    def loopback_port_is_free(self, port: int) -> bool:
        return self.free_port

    def commands_containing(self, word: str) -> list[tuple[str, ...]]:
        return [command for command in self.commands if word in command]


def settings_for(
    tmp_path: Path,
    *,
    database_path: str = DATABASE_PATH,
    project_root: Path | None = None,
    claude_executable: Path = Path("/opt/bin/claude"),
    port: int = SEAT_PORT,
) -> TerminalSeatSettings:
    root = project_root if project_root is not None else tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    return TerminalSeatSettings(
        project_id=PROJECT_ID,
        project_root=root,
        database_path=database_path,
        state_directory=tmp_path / "state",
        tmux_executable=Path("/usr/bin/tmux"),
        ttyd_executable=Path("/usr/bin/ttyd"),
        claude_executable=claude_executable,
        mcp_door_command=(*DOOR_COMMAND, SERVICE_URL),
        port=port,
    )


def seat_on(host: FakeSeatHost, settings: TerminalSeatSettings) -> TerminalSeat:
    return TerminalSeat(settings, host, PATH_TOKEN)


def test_names_carry_the_full_project_and_instance_digests(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)

    assert settings.session_name == f"seat-{digest_of(PROJECT_ID.value)}"
    assert settings.socket_name == f"atelier-seat-{digest_of(DATABASE_PATH)}"
    assert settings.scope_unit == f"atelier2-seat-{digest_of(PROJECT_ID.value)}.scope"


def test_two_deployments_of_one_project_never_share_a_socket(tmp_path: Path) -> None:
    live = settings_for(tmp_path, database_path="/var/lib/atelier2/atelier.db")
    harness = settings_for(tmp_path, database_path="/tmp/e2e/atelier.db")

    assert live.session_name == harness.session_name
    assert live.socket_name != harness.socket_name


def test_a_seat_port_outside_the_port_range_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"1\.\.65535"):
        settings_for(tmp_path, port=70000)


def test_creating_a_seat_starts_tmux_inside_a_transient_scope(tmp_path: Path) -> None:
    host = FakeSeatHost()
    settings = settings_for(tmp_path)

    outcome = seat_on(host, settings).ensure_session()

    assert outcome is TerminalSeatOutcome.CREATED
    assert host.commands_containing("new-session") == [
        (
            "/usr/bin/systemd-run",
            "--user",
            "--scope",
            "--collect",
            f"--unit=atelier2-{settings.session_name}",
            "/usr/bin/tmux",
            "-L",
            settings.socket_name,
            "new-session",
            "-d",
            "-s",
            settings.session_name,
            "-c",
            str(settings.project_root),
        )
    ]


def test_the_agent_is_typed_into_the_login_shell(tmp_path: Path) -> None:
    host = FakeSeatHost()
    settings = settings_for(tmp_path)

    seat_on(host, settings).ensure_session()

    typed, entered = host.commands_containing("send-keys")
    assert typed[:-1] == (
        "/usr/bin/tmux",
        "-L",
        settings.socket_name,
        "send-keys",
        "-t",
        f"={settings.session_name}",
        "-l",
    )
    assert shlex.split(typed[-1]) == [
        "/opt/bin/claude",
        "--mcp-config",
        str(settings.state_directory / f"{settings.session_name}.json"),
    ]
    assert entered[-1] == "Enter"


def test_a_path_with_spaces_and_metacharacters_survives_the_hand_over(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost()
    claude = tmp_path / "agent tools" / "cl$aude;rm -rf /"
    settings = settings_for(tmp_path, claude_executable=claude)

    seat_on(host, settings).ensure_session()

    typed = host.commands_containing("-l")[0]
    assert shlex.split(typed[-1])[0] == str(claude)


def test_a_running_seat_is_found_rather_than_created_again(tmp_path: Path) -> None:
    host = FakeSeatHost()
    settings = settings_for(tmp_path)
    seat = seat_on(host, settings)
    seat.ensure_session()

    outcome = seat.ensure_session()

    assert outcome is TerminalSeatOutcome.ALREADY_RUNNING
    assert len(host.commands_containing("new-session")) == 1
    assert len(host.commands_containing("send-keys")) == 2


def test_an_orphaned_scope_is_stopped_before_the_seat_is_recreated(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost(session_alive=False, scope_active=True)
    settings = settings_for(tmp_path)

    outcome = seat_on(host, settings).ensure_session()

    assert outcome is TerminalSeatOutcome.RECREATED_AFTER_ORPHANED_SCOPE
    stopped = host.commands_containing("stop")
    assert stopped == [("/usr/bin/systemctl", "--user", "stop", settings.scope_unit)]
    assert host.commands.index(stopped[0]) < host.commands.index(
        host.commands_containing("new-session")[0]
    )


def test_a_seat_whose_project_root_is_gone_is_reported_without_being_killed(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost(session_alive=True, scope_active=True)
    settings = settings_for(tmp_path, project_root=tmp_path / "gone")
    settings.project_root.rmdir()

    outcome = seat_on(host, settings).ensure_session()

    assert outcome is TerminalSeatOutcome.UNUSABLE_PROJECT_ROOT_MISSING
    assert host.commands == []


def test_a_machine_without_systemd_run_is_refused_without_a_fallback_child(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost(programs={"systemctl": Path("/usr/bin/systemctl")})

    outcome = seat_on(host, settings_for(tmp_path)).ensure_session()

    assert outcome is TerminalSeatOutcome.REFUSED_SYSTEMD_MISSING
    assert host.commands == []


def test_a_busy_seat_port_is_refused_rather_than_moved(tmp_path: Path) -> None:
    host = FakeSeatHost(free_port=False)

    outcome = seat_on(host, settings_for(tmp_path)).ensure_session()

    assert outcome is TerminalSeatOutcome.REFUSED_PORT_BUSY
    assert host.commands == []


def test_a_failing_management_command_is_not_swallowed(tmp_path: Path) -> None:
    host = FakeSeatHost(exit_codes={"new-session": 1})

    with pytest.raises(TerminalSeatCommandFailed, match="exited 1"):
        seat_on(host, settings_for(tmp_path)).ensure_session()


def test_stopping_the_seat_kills_the_session_and_stops_its_scope(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost()
    settings = settings_for(tmp_path)
    seat = seat_on(host, settings)
    seat.ensure_session()
    host.commands.clear()

    outcome = seat.stop_session()

    assert outcome is TerminalSeatOutcome.STOPPED
    assert host.commands_containing("kill-session") == [
        (
            "/usr/bin/tmux",
            "-L",
            settings.socket_name,
            "kill-session",
            "-t",
            f"={settings.session_name}",
        )
    ]
    assert host.commands_containing("stop") == [
        ("/usr/bin/systemctl", "--user", "stop", settings.scope_unit)
    ]


def test_stopping_a_seat_that_is_not_running_ends_nothing(tmp_path: Path) -> None:
    host = FakeSeatHost()

    outcome = seat_on(host, settings_for(tmp_path)).stop_session()

    assert outcome is TerminalSeatOutcome.NOT_RUNNING
    assert host.commands_containing("kill-session") == []
    assert host.commands_containing("stop") == []


def test_the_ttyd_child_serves_loopback_writable_and_origin_checked(
    tmp_path: Path,
) -> None:
    settings = settings_for(tmp_path)

    command = seat_on(FakeSeatHost(), settings).ttyd_command()

    assert command == (
        "/usr/bin/ttyd",
        "-i",
        "127.0.0.1",
        "-p",
        str(SEAT_PORT),
        "-W",
        "-O",
        "-b",
        f"/seat-{PATH_TOKEN}",
        "/usr/bin/tmux",
        "-L",
        settings.socket_name,
        "attach-session",
        "-t",
        f"={settings.session_name}",
    )


def test_the_ttyd_child_asks_for_no_credential(tmp_path: Path) -> None:
    command = seat_on(FakeSeatHost(), settings_for(tmp_path)).ttyd_command()

    assert "-c" not in command
    assert "-H" not in command


def test_the_seat_url_names_the_drawn_base_path_on_loopback(tmp_path: Path) -> None:
    seat = seat_on(FakeSeatHost(), settings_for(tmp_path))

    assert seat.url == f"http://127.0.0.1:{SEAT_PORT}/seat-{PATH_TOKEN}/"


def test_each_serve_draws_its_own_base_path(tmp_path: Path) -> None:
    settings = settings_for(tmp_path)
    host = FakeSeatHost()

    first = TerminalSeat.for_serve(settings, host)
    second = TerminalSeat.for_serve(settings, host)

    assert first.base_path != second.base_path
    assert first.base_path.startswith("/seat-")


def test_the_generated_mcp_document_names_only_the_loopback_door(
    tmp_path: Path,
) -> None:
    host = FakeSeatHost()
    settings = settings_for(tmp_path)

    seat_on(host, settings).ensure_session()

    document = settings.state_directory / f"{settings.session_name}.json"
    assert json.loads(document.read_text(encoding="utf-8")) == {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": DOOR_COMMAND[0],
                "args": [*DOOR_COMMAND[1:], SERVICE_URL],
            }
        }
    }
    assert not list(settings.project_root.iterdir())


def test_no_seat_command_ever_reads_the_terminal(tmp_path: Path) -> None:
    host = FakeSeatHost()
    seat = seat_on(host, settings_for(tmp_path))
    seat.ensure_session()
    seat.stop_session()

    issued = {argument for command in host.commands for argument in command}
    assert {"pipe-pane", "capture-pane"}.isdisjoint(issued)
