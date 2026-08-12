from __future__ import annotations

import json
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol
from urllib.request import urlopen

import pytest
from fastapi.testclient import TestClient

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.reconciler import DbosEffectReconcileCommander
from atelier2.adapters.dbos.run_store import DbosWaitAnswerer
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.yaml_workflows import parse_workflow_document
from atelier2.api.app import ApiPorts, create_app
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.host import HostSettings, api_limits, event_poll_backoff
from tests.scenarios.api import api_limits as scenario_api_limits
from tests.scenarios.api import event_poll_backoff as scenario_event_poll_backoff
from tests.scenarios.runtime import exact_output_runtime


@pytest.mark.parametrize(
    "path",
    [
        "/atelier",
        "/atelier/",
        "/atelier/runs",
        "/atelier/new",
        "/atelier/runs/run1.cnVu",
    ],
)
def test_frontend_routes_serve_one_fixed_index_without_catching_api(
    runtime, frontend_dist: Path, path: str
) -> None:
    app = create_app(
        source_commit="commit",
        source_tree="tree",
        ports=api_ports(runtime),
        limits=scenario_api_limits(),
        event_poll_backoff=scenario_event_poll_backoff(),
        frontend_dist=frontend_dist,
    )

    with TestClient(app) as client:
        assert client.get(path).text == "<main>cockpit</main>"
        assert (
            client.get("/atelier/api/v1/not-real")
            .headers["content-type"]
            .startswith("application/problem+json")
        )


def test_frontend_assets_are_mounted_only_below_the_same_origin_asset_path(
    runtime, frontend_dist: Path
) -> None:
    app = create_app(
        source_commit="commit",
        source_tree="tree",
        ports=api_ports(runtime),
        limits=scenario_api_limits(),
        event_poll_backoff=scenario_event_poll_backoff(),
        frontend_dist=frontend_dist,
    )

    with TestClient(app) as client:
        assert client.get("/atelier/assets/app.js").text == "cockpit"
        assert client.get("/assets/app.js").status_code == 404
        assert (
            "/atelier/runs"
            not in client.get("/atelier/api/v1/openapi.json").json()["paths"]
        )


@pytest.mark.parametrize("missing", ["index", "assets"])
def test_frontend_configuration_fails_loud_when_build_output_is_incomplete(
    runtime, tmp_path: Path, missing: str
) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    if missing != "index":
        (frontend / "index.html").write_text("index")
    if missing != "assets":
        (frontend / "assets").mkdir()

    with pytest.raises(ValueError, match="frontend distribution"):
        create_app(
            source_commit="commit",
            source_tree="tree",
            ports=api_ports(runtime),
            limits=scenario_api_limits(),
            event_poll_backoff=scenario_event_poll_backoff(),
            frontend_dist=frontend,
        )


@pytest.fixture
def frontend_dist(tmp_path: Path) -> Path:
    frontend = tmp_path / "frontend"
    assets = frontend / "assets"
    assets.mkdir(parents=True)
    (frontend / "index.html").write_text("<main>cockpit</main>")
    (assets / "app.js").write_text("cockpit")
    return frontend


@pytest.fixture
def runtime(tmp_path: Path) -> Iterator[DbosRuntime]:
    configured = exact_output_runtime(
        DbosRuntimeSettings(tmp_path / "atelier.sqlite", "host-test"),
        LoopbackEffectAdapterFactory(
            tmp_path / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("host-test"),
        ),
    )
    configured.initialize_storage()
    try:
        yield configured
    finally:
        configured.close()


def api_ports(runtime: DbosRuntime) -> ApiPorts:
    queries = DbosQueries(runtime.engine)
    return ApiPorts(
        workflow_revision_publisher=DbosWorkflowRevisionPublisher(runtime.engine),
        published_run_starter=DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        ),
        wait_answerer=DbosWaitAnswerer(
            runtime.engine, runtime.settings.application_version
        ),
        reconcile_commander=DbosEffectReconcileCommander(
            runtime.engine, runtime.settings
        ),
        workflow_revision_queries=queries,
        run_queries=queries,
        run_event_queries=queries,
        workflow_document_parser=parse_workflow_document,
        agent_configuration_catalog=DbosAgentConfigurationCatalog(
            runtime.engine, runtime.agent_executor_registry
        ),
    )


def test_host_settings_own_named_production_defaults(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("index")
    settings = HostSettings(
        database_path=tmp_path / "durable.sqlite",
        effect_store_path=tmp_path / "effects.sqlite",
        effect_adapter_revision="loopback-v1",
        effect_destination="local",
        application_version="test",
        source_commit="commit",
        source_tree="tree",
        frontend_dist=frontend,
    )

    assert settings.host == "127.0.0.1"
    assert settings.port == 8422
    assert settings.limits == api_limits()
    assert settings.event_poll_backoff == event_poll_backoff()


def test_real_console_launcher_starts_and_closes_one_runtime(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<main>launcher</main>")
    port = free_port()
    command = [
        "uv",
        "run",
        "atelier2",
        "serve",
        "--database",
        str(tmp_path / "durable.sqlite"),
        "--effect-store",
        str(tmp_path / "effects.sqlite"),
        "--effect-adapter-revision",
        "loopback-v1",
        "--effect-destination",
        "launcher-test",
        "--application-version",
        "launcher-v1",
        "--source-commit",
        "exact-commit",
        "--source-tree",
        "exact-tree",
        "--frontend-dist",
        str(frontend),
        "--port",
        str(port),
    ]

    first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert wait_for_health(port) == {
            "status": "serving",
            "source_commit": "exact-commit",
            "source_tree": "exact-tree",
        }
    finally:
        first_error = stop_child_process(first)
    assert first.returncode == 0, first_error.decode(errors="replace")

    second = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        assert wait_for_health(port)["status"] == "serving"
    finally:
        second_error = stop_child_process(second)
    assert second.returncode == 0, second_error.decode(errors="replace")


def test_child_cleanup_kills_and_waits_after_communicate_timeout() -> None:
    child = TimeoutChild()

    stop_child_process(child)

    assert child.calls == ["signal", "communicate", "kill", "communicate"]


class TimeoutChild:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def send_signal(self, sig: int) -> None:
        del sig
        self.calls.append("signal")

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        del input
        self.calls.append("communicate")
        if self.calls.count("communicate") == 1:
            raise subprocess.TimeoutExpired(
                "atelier2", 0 if timeout is None else timeout
            )
        return b"", b""

    def kill(self) -> None:
        self.calls.append("kill")


class ChildProcess(Protocol):
    def send_signal(self, sig: int) -> None: ...

    def communicate(
        self, input: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes]: ...

    def kill(self) -> None: ...


def stop_child_process(child: ChildProcess) -> bytes:
    child.send_signal(signal.SIGINT)
    try:
        _, stderr = child.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        _, stderr = child.communicate(timeout=10)
    return stderr


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_health(port: int) -> dict[str, str]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with urlopen(
                f"http://127.0.0.1:{port}/atelier/api/v1/health", timeout=0.2
            ) as response:
                return json.load(response)
        except OSError:
            time.sleep(0.05)
    raise AssertionError("local host did not become healthy")
