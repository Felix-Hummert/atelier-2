from __future__ import annotations

import re
from collections.abc import Callable

from fastapi.testclient import TestClient

from atelier2.api.app import create_app
from atelier2.api.openapi import API_PREFIX
from atelier2.contracts.when import RECORDED_AT_PATTERN, RecordedAt, recorded_instant
from tests.scenarios.api import api_limits, api_ports, event_poll_backoff

HEALTH_PATH = API_PREFIX + "/health"


def client(*, boot_clock: Callable[[], RecordedAt] = recorded_instant) -> TestClient:
    return TestClient(
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(),
            limits=api_limits(),
            event_poll_backoff=event_poll_backoff(),
            boot_clock=boot_clock,
        )
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
