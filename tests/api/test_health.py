from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path

from fastapi.testclient import TestClient

from atelier2.adapters.redeploy_status import filesystem_redeploy_status_reader
from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.api.routes.health import REDEPLOY_STATUS_UNREADABLE_REASON
from atelier2.contracts.when import RECORDED_AT_PATTERN, RecordedAt, recorded_instant
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

HEALTH_PATH = API_PREFIX + "/health"


def client(
    *,
    boot_clock: Callable[[], RecordedAt] = recorded_instant,
    redeploy_status_path: Path | None = None,
) -> TestClient:
    reader = (
        None
        if redeploy_status_path is None
        else filesystem_redeploy_status_reader(redeploy_status_path)
    )
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(redeploy_status_reader=reader),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
            boot_clock=boot_clock,
        )
    )


def write_redeploy_status(
    path: Path,
    *,
    failure_count: int,
    last_failure_reason: str | None = None,
    last_failure_at: str | None = None,
    last_success_commit: str | None = None,
    last_success_at: str | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "failure_count": failure_count,
                "last_failure_reason": last_failure_reason,
                "last_failure_at": last_failure_at,
                "last_success_commit": last_success_commit,
                "last_success_at": last_success_at,
            }
        ),
        encoding="utf-8",
    )


def test_health_names_when_this_serve_process_started() -> None:
    fixed_boot = RecordedAt("2026-08-31T08:00:00Z")

    response = client(boot_clock=lambda: fixed_boot).get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.json()["serve_started_at"] == "2026-08-31T08:00:00Z"


def test_health_serve_started_at_is_a_recorded_instant_by_default() -> None:
    response = client().get(HEALTH_PATH)

    assert response.status_code == 200
    assert re.fullmatch(RECORDED_AT_PATTERN, response.json()["serve_started_at"])


def test_health_reports_the_same_boot_instant_across_repeated_requests() -> None:
    served = client(boot_clock=lambda: RecordedAt("2026-08-31T08:00:00Z"))

    first = served.get(HEALTH_PATH).json()["serve_started_at"]
    second = served.get(HEALTH_PATH).json()["serve_started_at"]

    assert first == second == "2026-08-31T08:00:00Z"


def test_health_omits_redeploy_when_no_status_path_is_configured() -> None:
    response = client().get(HEALTH_PATH)

    assert response.status_code == 200
    assert "redeploy" not in response.json()


def test_health_omits_redeploy_when_the_status_file_is_absent(
    tmp_path: Path,
) -> None:
    response = client(redeploy_status_path=tmp_path / "redeploy-status.json").get(
        HEALTH_PATH
    )

    assert response.status_code == 200
    assert "redeploy" not in response.json()


def test_health_omits_redeploy_below_the_failure_threshold(tmp_path: Path) -> None:
    status_path = tmp_path / "redeploy-status.json"
    write_redeploy_status(
        status_path,
        failure_count=2,
        last_failure_reason="checkout is dirty",
        last_failure_at="2026-09-04T08:00:00Z",
    )

    response = client(redeploy_status_path=status_path).get(HEALTH_PATH)

    assert response.status_code == 200
    assert "redeploy" not in response.json()


def test_health_names_a_redeploy_block_at_the_failure_threshold(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "redeploy-status.json"
    write_redeploy_status(
        status_path,
        failure_count=3,
        last_failure_reason="checkout is dirty",
        last_failure_at="2026-09-04T08:00:00Z",
    )

    response = client(redeploy_status_path=status_path).get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.json()["redeploy"] == {
        "blocked_since": "2026-09-04T08:00:00Z",
        "reason": "checkout is dirty",
    }


def test_health_names_a_malformed_status_file_instead_of_swallowing_it(
    tmp_path: Path,
) -> None:
    status_path = tmp_path / "redeploy-status.json"
    status_path.write_text("not json", encoding="utf-8")

    response = client(redeploy_status_path=status_path).get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.json()["redeploy"] == {
        "blocked_since": None,
        "reason": REDEPLOY_STATUS_UNREADABLE_REASON,
    }
