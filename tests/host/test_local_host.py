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

from atelier2.adapters.claude_subscription import (
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    ClaudeSubscriptionSettings,
)
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
from atelier2.host import (
    DEFAULT_HOST,
    HostSettings,
    api_limits,
    compose_application,
    event_poll_backoff,
    main,
)
from tests.scenarios.agents import claude_subscription_deployment
from tests.scenarios.api import api_limits as scenario_api_limits
from tests.scenarios.api import event_poll_backoff as scenario_event_poll_backoff
from tests.scenarios.runtime import exact_output_runtime

INERT_CLAUDE = "raise SystemExit(0)\n"


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


def served_settings(
    tmp_path: Path,
    claude_subscription: ClaudeSubscriptionSettings | None = None,
    host: str = DEFAULT_HOST,
) -> HostSettings:
    frontend = tmp_path / "frontend"
    if not frontend.is_dir():
        (frontend / "assets").mkdir(parents=True)
        (frontend / "index.html").write_text("index")
    return HostSettings(
        database_path=tmp_path / "durable.sqlite",
        effect_store_path=tmp_path / "effects.sqlite",
        effect_adapter_revision="loopback-v1",
        effect_destination="local",
        application_version="composition-test",
        source_commit="commit",
        source_tree="tree",
        frontend_dist=frontend,
        host=host,
        claude_subscription=claude_subscription,
    )


def test_an_undeclared_claude_deployment_offers_no_provider_executor(
    tmp_path: Path,
) -> None:
    _app, runtime = compose_application(served_settings(tmp_path))

    try:
        assert runtime.agent_executor_registry.keys == frozenset()
    finally:
        runtime.close()


def test_a_declared_claude_deployment_offers_its_executor_to_every_run(
    tmp_path: Path,
) -> None:
    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    settings = served_settings(
        tmp_path,
        claude_subscription=claude_subscription_deployment(deployment, INERT_CLAUDE),
    )

    _app, runtime = compose_application(settings)

    try:
        assert runtime.agent_executor_registry.keys == frozenset(
            {CLAUDE_SUBSCRIPTION_EXECUTOR_KEY}
        )
    finally:
        runtime.close()


def serve_arguments(tmp_path: Path, *extra: str) -> list[str]:
    frontend = tmp_path / "frontend"
    if not frontend.is_dir():
        (frontend / "assets").mkdir(parents=True)
        (frontend / "index.html").write_text("index")
    return [
        "serve",
        "--database",
        str(tmp_path / "durable.sqlite"),
        "--effect-store",
        str(tmp_path / "effects.sqlite"),
        "--effect-adapter-revision",
        "loopback-v1",
        "--effect-destination",
        "local",
        "--application-version",
        "refusal-test",
        "--source-commit",
        "commit",
        "--source-tree",
        "tree",
        "--frontend-dist",
        str(frontend),
        *extra,
    ]


def test_a_partly_declared_claude_deployment_refuses_to_serve(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as refusal:
        main(serve_arguments(tmp_path, "--claude-executable", str(tmp_path / "claude")))

    assert refusal.value.code == 2


@pytest.mark.parametrize(
    "bind",
    ["0.0.0.0", "::", "192.168.1.10", "localhost", "cockpit.example"],
)
def test_a_claude_deployment_off_loopback_refuses_to_serve(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    bind: str,
) -> None:
    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    settings = claude_subscription_deployment(deployment, INERT_CLAUDE)
    monkeypatch.setenv("PATH", settings.search_path)

    with pytest.raises(SystemExit) as refusal:
        main(
            serve_arguments(
                tmp_path,
                "--host",
                bind,
                "--claude-executable",
                str(settings.executable),
                "--claude-workspace",
                str(settings.workspace),
                "--claude-credential-directory",
                str(settings.credential_directory),
            )
        )

    assert refusal.value.code == 2
    assert "loopback" in capsys.readouterr().err


def test_a_claude_deployment_on_an_unconformant_executable_refuses_to_serve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmeasured CLI stops the server, not the first billed run."""

    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    settings = claude_subscription_deployment(
        deployment, INERT_CLAUDE, version="2.1.222"
    )
    monkeypatch.setenv("PATH", settings.search_path)

    with pytest.raises(SystemExit) as refusal:
        main(
            serve_arguments(
                tmp_path,
                "--claude-executable",
                str(settings.executable),
                "--claude-workspace",
                str(settings.workspace),
                "--claude-credential-directory",
                str(settings.credential_directory),
            )
        )

    assert refusal.value.code == 2
    assert "not 2.1.222" in capsys.readouterr().err


def test_a_claude_deployment_without_bubblewrap_refuses_to_serve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server's own search path is what the launched provider receives."""

    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    settings = claude_subscription_deployment(deployment, INERT_CLAUDE)
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools-here"))

    with pytest.raises(SystemExit) as refusal:
        main(
            serve_arguments(
                tmp_path,
                "--claude-executable",
                str(settings.executable),
                "--claude-workspace",
                str(settings.workspace),
                "--claude-credential-directory",
                str(settings.credential_directory),
            )
        )

    assert refusal.value.code == 2
    assert "bwrap" in capsys.readouterr().err


@pytest.mark.parametrize("bind", ["127.0.0.1", "127.0.0.2", "::1"])
def test_a_claude_deployment_on_loopback_is_accepted(tmp_path: Path, bind: str) -> None:
    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()

    settings = served_settings(
        tmp_path,
        claude_subscription=claude_subscription_deployment(deployment, INERT_CLAUDE),
        host=bind,
    )

    assert settings.host == bind


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
