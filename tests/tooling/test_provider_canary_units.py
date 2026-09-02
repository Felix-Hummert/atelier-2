from __future__ import annotations

import configparser
import shlex
from pathlib import Path

from atelier2.host.provider_canary import PROVIDER_CANARY_PROCESS_TIMEOUT_SECONDS

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = PROJECT_ROOT / "scripts"


def unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    parser.read(path, encoding="utf-8")
    return parser


def test_provider_canary_service_invokes_the_real_cli_subcommand() -> None:
    service = unit(SCRIPTS / "atelier2-provider-canary.service")

    command = shlex.split(service["Service"]["ExecStart"])

    assert service["Service"]["WorkingDirectory"] == "/absolute/path/to/atelier-2"
    assert command == [
        "%h/.local/bin/uv",
        "run",
        "--locked",
        "atelier2",
        "provider-canary",
    ]
    assert service["Service"]["Type"] == "oneshot"
    assert service["Service"]["TimeoutStartSec"] == "15300"
    assert float(service["Service"]["TimeoutStartSec"]) == (
        PROVIDER_CANARY_PROCESS_TIMEOUT_SECONDS
    )


def test_provider_canary_timer_is_persistent_and_targets_the_oneshot() -> None:
    timer = unit(SCRIPTS / "atelier2-provider-canary.timer")

    assert timer.getboolean("Timer", "Persistent")
    assert timer["Timer"]["Unit"] == "atelier2-provider-canary.service"
    assert "OnCalendar" in timer["Timer"]


def test_serve_drop_in_triggers_the_provider_canary_unit_after_start() -> None:
    drop_in = unit(SCRIPTS / "atelier2-serve.service.d" / "provider-canary.conf")

    command = shlex.split(drop_in["Service"]["ExecStartPost"])

    assert command == [
        "-/usr/bin/systemctl",
        "--user",
        "--no-block",
        "start",
        "atelier2-provider-canary.service",
    ]


def test_serve_clean_stop_drop_in_accepts_sigterm_exit_status() -> None:
    drop_in = unit(SCRIPTS / "atelier2-serve.service.d" / "clean-stop.conf")

    assert drop_in["Service"]["SuccessExitStatus"] == "143"
