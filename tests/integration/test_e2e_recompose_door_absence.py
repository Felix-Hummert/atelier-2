"""The e2e harness's `/__e2e/recompose` restart door (#742).

`tests/e2e/serve_cockpit.py` intercepts `/__e2e/*` paths in its own ASGI
wrapper around the production app, never inside `compose_application` itself.
This is the door's own proof that it stays exactly that: unreachable on a
served production app, whether or not the request even names it as a POST.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from atelier2.host.serving import compose_application
from tests.host.test_local_host import served_settings


def test_the_e2e_recompose_door_does_not_exist_on_a_served_production_app(
    tmp_path: Path,
) -> None:
    app, runtime = compose_application(served_settings(tmp_path))
    try:
        with TestClient(app) as client:
            response = client.post("/__e2e/recompose")
    finally:
        runtime.close()
    assert response.status_code == 404
