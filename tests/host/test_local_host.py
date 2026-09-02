from __future__ import annotations

import argparse
import base64
import json
import signal
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Never
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest
import sqlalchemy as sa
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import atelier2.adapters.dbos.runtime as dbos_runtime
from atelier2.adapters.claude_subscription import (
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY,
    ClaudeSubscriptionSettings,
)
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.catalog_store import DbosCatalogStore
from atelier2.adapters.dbos.host_configuration import (
    DbosHostConfigurationChannel,
    append_project_root,
)
from atelier2.adapters.dbos.runtime import (
    SQLITE_LOCK_TIMEOUT_SECONDS,
    AgentProcessSupervisorUnavailable,
    DbosRuntime,
    DbosRuntimeSettings,
)
from atelier2.adapters.dbos.schema import agent_attempts, initialize_schema, runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.github import (
    GitHubConnectionUncomposable,
    GitHubCredentialUnresolvable,
)
from atelier2.adapters.grok_subscription import (
    GROK_SUBSCRIPTION_EXECUTOR_KEY,
    GROK_WORKSPACE_TOOLS_EXECUTOR_KEY,
    GrokSubscriptionSettings,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.adapters.project_verification import PROJECT_MANIFEST_NAME
from atelier2.api.app import create_app
from atelier2.api.context import ApiPorts
from atelier2.api.limits import ApiLimitExceeded, base64_characters_for
from atelier2.api.openapi import API_PREFIX, PROJECT_PATH, PROJECTS_PATH
from atelier2.api.references import encode_public_project_reference
from atelier2.application.project_connections import (
    ProjectSourceConnectionPublished,
    connect_project_source,
)
from atelier2.contracts.agent_attempts import AGENT_ATTEMPT_ORDINAL
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.hashing import Sha256Hash
from atelier2.contracts.host_configuration import ProjectId
from atelier2.contracts.provider_probe_receipts import (
    ProviderProbeReceipt,
    ProviderProbeResult,
    ProviderProbeVectorId,
)
from atelier2.contracts.runs import RunId, WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.when import recorded_instant
from atelier2.host import _claude_subscription_settings, main
from atelier2.host.address import DEFAULT_HOST
from atelier2.host.serving import (
    HostSettings,
    LegacyAgentOpenPrCompletionWithoutReceipt,
    api_limits,
    compose_application,
    event_poll_backoff,
)
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AgentConfigurationRevisionPage,
    AuthProfileRevisionCreated,
)
from atelier2.ports.durable_runs import (
    DurableAgentExecutorBindingUnavailable,
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from tests.integration.test_codex_subscription import codex_subscription_deployment
from tests.integration.test_grok_subscription import (
    HOST_DOCUMENT,
    INTROSPECTING_GROK,
    grok_subscription_deployment,
)
from tests.scenarios.agents import (
    agent_scratch_root,
    claude_subscription_deployment,
    publish_checked_model_registry,
)
from tests.scenarios.api import api_limits as scenario_api_limits
from tests.scenarios.api import durable_ports
from tests.scenarios.api import event_poll_backoff as scenario_event_poll_backoff
from tests.scenarios.open_pr_agent import (
    PR_SPEC,
    complete_run,
    open_pr_agent_executor_factory,
    publish_open_pr_agent_run,
    seed_current_node_attempt,
    seed_workflow_status,
)
from tests.scenarios.projects import (
    declaring_verification,
    git_project,
    write_into_checkout,
)
from tests.scenarios.workflows import ANY_JSON_SCHEMA

INERT_CLAUDE = "raise SystemExit(0)\n"

SAMPLE_PUBLIC_REFERENCE = "run1.cnVu"


def declared_cockpit_paths() -> tuple[str, ...]:
    """The address space the browser declares, read from the browser's own file.

    The client router decides which paths are pages; the server only has to hand
    the application to each of them on a cold load. Repeating that list here is
    what let `/atelier/project` ship unserved: a copied list can only confirm the
    paths someone already thought of.
    """
    declaration = (
        Path(__file__).resolve().parents[2] / "frontend/src/lib/servedPaths.json"
    )
    paths = json.loads(declaration.read_text(encoding="utf-8"))
    return tuple(paths)


@pytest.mark.proves("a-level-opens-from-a-pasted-link-and-survives-a-reload")
def test_the_server_serves_exactly_the_paths_the_browser_declares(
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

    served = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute) and not route.path.startswith("/atelier/api")
    }

    assert served == set(declared_cockpit_paths())


@pytest.mark.parametrize("path", declared_cockpit_paths())
def test_frontend_routes_serve_one_fixed_index_without_catching_api(
    runtime, frontend_dist: Path, path: str
) -> None:
    path = path.replace("{public_ref}", SAMPLE_PUBLIC_REFERENCE).replace(
        "{workflow_name:path}", "catalog/detail"
    )
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
    configured = DbosRuntime(
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
    return durable_ports(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
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


@pytest.mark.parametrize(
    "payload_bytes",
    [
        0,
        1,
        2,
        3,
        4,
        5,
        MAXIMUM_AGENT_OUTPUT_BYTES_V2 - 1,
        MAXIMUM_AGENT_OUTPUT_BYTES_V2,
    ],
)
def test_the_base64_expansion_is_what_the_encoder_actually_produces(
    payload_bytes: int,
) -> None:
    encoded = base64.b64encode(b"x" * payload_bytes)

    assert base64_characters_for(payload_bytes) == len(encoded)


def test_the_host_bounds_admit_exactly_what_the_durable_contract_accepts() -> None:
    limits = api_limits()
    largest_durable_result = b"x" * MAXIMUM_AGENT_OUTPUT_BYTES_V2

    assert limits.maximum_decoded_payload_bytes == MAXIMUM_AGENT_OUTPUT_BYTES_V2
    assert limits.maximum_base64_characters == len(
        base64.b64encode(largest_durable_result)
    )
    limits.require_encoded_payload(largest_durable_result)
    with pytest.raises(ApiLimitExceeded):
        limits.require_encoded_payload(largest_durable_result + b"x")


def served_settings(
    tmp_path: Path,
    claude_subscription: ClaudeSubscriptionSettings | None = None,
    claude_workspace_tools: bool = False,
    claude_start_refusal: str | None = None,
    grok_subscription: GrokSubscriptionSettings | None = None,
    grok_workspace_tools: bool = False,
    host: str = DEFAULT_HOST,
    scratch_root: Path | None = None,
    sqlite_lock_timeout_seconds: float = SQLITE_LOCK_TIMEOUT_SECONDS,
    project_id: ProjectId | None = None,
    project_root: Path | None = None,
    provider_probe_receipt_directory: Path | None = None,
    **tuning: int,
) -> HostSettings:
    frontend = tmp_path / "frontend"
    if not frontend.is_dir():
        (frontend / "assets").mkdir(parents=True)
        (frontend / "index.html").write_text("index")
    billed = claude_subscription is not None or grok_subscription is not None
    return HostSettings(
        database_path=tmp_path / "durable.sqlite",
        effect_store_path=tmp_path / "effects.sqlite",
        effect_adapter_revision="loopback-v1",
        effect_destination="local",
        application_version="composition-test",
        source_commit="c" * 40,
        source_tree="tree",
        # Isolated per test, never the operator's real XDG state directory:
        # a stray real receipt must never make an unrelated test's gate
        # answer depend on what happens to sit on the machine running it.
        provider_probe_receipt_directory=(
            provider_probe_receipt_directory
            if provider_probe_receipt_directory is not None
            else tmp_path / "provider-probes"
        ),
        frontend_dist=frontend,
        host=host,
        agent_scratch_root=(
            scratch_root
            if scratch_root is not None or not billed
            else agent_scratch_root(tmp_path)
        ),
        claude_subscription=claude_subscription,
        claude_workspace_tools=claude_workspace_tools,
        claude_start_refusal=claude_start_refusal,
        grok_subscription=grok_subscription,
        grok_workspace_tools=grok_workspace_tools,
        limits=api_limits(**tuning),
        sqlite_lock_timeout_seconds=sqlite_lock_timeout_seconds,
        project_id=project_id,
        project_root=project_root,
    )


def test_an_undeclared_claude_deployment_offers_no_provider_executor(
    tmp_path: Path,
) -> None:
    _app, runtime = compose_application(served_settings(tmp_path))

    try:
        assert runtime.agent_executor_registry.keys == frozenset()
    finally:
        runtime.close()


def test_provider_free_composition_skips_process_supervision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden_process_authority() -> Never:
        raise AssertionError("provider-free host resolved process authority")

    monkeypatch.setattr(
        dbos_runtime, "delegated_cgroup_root", forbidden_process_authority
    )
    monkeypatch.setattr(
        dbos_runtime, "AgentProcessSupervisor", forbidden_process_authority
    )
    _app, runtime = compose_application(served_settings(tmp_path))
    try:
        assert runtime.agent_executor_registry.keys == frozenset()
        with pytest.raises(
            AgentProcessSupervisorUnavailable,
            match="no LOCAL_PROCESS-carried executor key",
        ):
            _ = runtime.agent_process_supervisor
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


def test_the_workspace_tool_executor_is_served_only_where_it_was_armed(
    tmp_path: Path,
) -> None:
    """Naming a Claude executable grants a tool-free call and nothing more.

    The second executor lets a node's own process read, write and run commands
    as the serving user, so it is a grant of its own: it appears in the registry
    only where the operator armed it, and every run served without it can only
    reach the tool-free one.
    """

    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    settings = served_settings(
        tmp_path,
        claude_subscription=claude_subscription_deployment(deployment, INERT_CLAUDE),
        claude_workspace_tools=True,
    )

    _app, runtime = compose_application(settings)

    try:
        assert runtime.agent_executor_registry.keys == frozenset(
            {CLAUDE_SUBSCRIPTION_EXECUTOR_KEY, CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY}
        )
        assert runtime.agent_executor_registry.declared_capabilities(
            CLAUDE_WORKSPACE_TOOLS_EXECUTOR_KEY
        ) == frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS})
    finally:
        runtime.close()


def test_arming_the_workspace_tools_without_a_claude_deployment_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="second executor"):
        served_settings(tmp_path, claude_workspace_tools=True)


def test_the_grok_workspace_tool_executor_is_served_only_where_it_was_armed(
    tmp_path: Path,
) -> None:
    """Naming a Grok executable grants a tool-free call and nothing more."""

    deployment = tmp_path / "grok-deployment"
    deployment.mkdir()
    settings = served_settings(
        tmp_path,
        grok_subscription=grok_subscription_deployment(deployment, INTROSPECTING_GROK),
        grok_workspace_tools=True,
    )

    _app, runtime = compose_application(settings)

    try:
        assert runtime.agent_executor_registry.keys == frozenset(
            {GROK_SUBSCRIPTION_EXECUTOR_KEY, GROK_WORKSPACE_TOOLS_EXECUTOR_KEY}
        )
        assert runtime.agent_executor_registry.declared_capabilities(
            GROK_WORKSPACE_TOOLS_EXECUTOR_KEY
        ) == frozenset({AgentExecutionCapability.HEADLESS_WITH_TOOLS})
    finally:
        runtime.close()


def test_arming_the_grok_workspace_tools_without_a_grok_deployment_is_refused(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="second executor"):
        served_settings(tmp_path, grok_workspace_tools=True)


def test_arming_grok_workspace_tools_on_the_command_line_without_a_deployment_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refusal:
        main(serve_arguments(tmp_path, "--grok-workspace-tools"))

    assert refusal.value.code == 2
    assert "--grok-workspace-tools" in capsys.readouterr().err


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


def claude_serve_arguments(
    tmp_path: Path, settings: ClaudeSubscriptionSettings, *extra: str
) -> list[str]:
    return serve_arguments(
        tmp_path,
        "--agent-scratch-root",
        str(agent_scratch_root(tmp_path)),
        "--claude-executable",
        str(settings.executable),
        "--claude-credential-directory",
        str(settings.credential_directory),
        *extra,
    )


def test_a_declared_deployment_leases_workspaces_from_the_declared_root(
    tmp_path: Path,
) -> None:
    """A complete configuration binds the exact root the operator named."""

    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    scratch_root = agent_scratch_root(tmp_path)
    settings = served_settings(
        tmp_path,
        claude_subscription=claude_subscription_deployment(deployment, INERT_CLAUDE),
        scratch_root=scratch_root,
    )

    _app, runtime = compose_application(settings)

    try:
        owner = runtime.agent_workspace_owner
        assert owner is not None
        assert owner.scratch_root == scratch_root
    finally:
        runtime.close()


def test_a_provider_deployment_without_a_scratch_root_refuses_to_serve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every attempt runs in a workspace, so serving one needs somewhere to lease."""

    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    settings = claude_subscription_deployment(deployment, INERT_CLAUDE)
    monkeypatch.setenv("PATH", settings.search_path)

    with pytest.raises(SystemExit) as refusal:
        main(
            serve_arguments(
                tmp_path,
                "--claude-executable",
                str(settings.executable),
                "--claude-credential-directory",
                str(settings.credential_directory),
            )
        )

    assert refusal.value.code == 2
    assert "--agent-scratch-root" in capsys.readouterr().err


def a_root_that_is_no_repository(root: Path) -> None:
    root.mkdir(parents=True)
    write_into_checkout(root, declaring_verification(["/bin/true"]))


def a_repository_declaring_no_verification(root: Path) -> None:
    git_project(root, {PROJECT_MANIFEST_NAME: "[project]\nname = 'says nothing'\n"})


PROJECT_ROOT_REFUSALS: tuple[tuple[str, Callable[[Path], None], str], ...] = (
    ("a root that is no repository", a_root_that_is_no_repository, "project source"),
    (
        "a repository declaring no verification",
        a_repository_declaring_no_verification,
        "no verification",
    ),
)


@pytest.mark.parametrize(
    ("label", "build", "refusal_words"),
    PROJECT_ROOT_REFUSALS,
    ids=[label for label, _build, _words in PROJECT_ROOT_REFUSALS],
)
def test_a_project_that_cannot_be_pinned_or_declares_nothing_refuses_to_serve(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    label: str,
    build: Callable[[Path], None],
    refusal_words: str,
) -> None:
    """Every attempt works in a commit of this project, so both facts are asked here."""

    del label
    root = tmp_path / "project"
    build(root)

    with pytest.raises(SystemExit) as refusal:
        main(
            serve_arguments(
                tmp_path, "--project-id", "studio", "--project-root", str(root)
            )
        )

    assert refusal.value.code == 2
    assert refusal_words in capsys.readouterr().err


def test_a_scratch_root_without_any_provider_executor_refuses_to_serve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as refusal:
        main(
            serve_arguments(
                tmp_path, "--agent-scratch-root", str(agent_scratch_root(tmp_path))
            )
        )

    assert refusal.value.code == 2
    assert "serves nothing" in capsys.readouterr().err


def test_a_scratch_root_inside_a_git_worktree_refuses_to_serve_and_says_why(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whoever first points this at a checkout reads the reason, not a traceback."""

    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    settings = claude_subscription_deployment(deployment, INERT_CLAUDE)
    monkeypatch.setenv("PATH", settings.search_path)
    checkout = tmp_path / "checkout"
    git_directory = checkout / ".git"
    git_directory.mkdir(parents=True)
    (git_directory / "HEAD").touch()
    scratch_root = checkout / "scratch"
    scratch_root.mkdir(mode=0o700)

    with pytest.raises(SystemExit) as refusal:
        main(
            serve_arguments(
                tmp_path,
                "--agent-scratch-root",
                str(scratch_root),
                "--claude-executable",
                str(settings.executable),
                "--claude-credential-directory",
                str(settings.credential_directory),
            )
        )

    assert refusal.value.code == 2
    assert "git worktree" in capsys.readouterr().err


def test_a_partly_declared_claude_deployment_refuses_to_serve(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as refusal:
        main(serve_arguments(tmp_path, "--claude-executable", str(tmp_path / "claude")))

    assert refusal.value.code == 2


def _github_credential_directory(tmp_path: Path, token: str | None) -> Path:
    directory = tmp_path / "github-credential"
    directory.mkdir(exist_ok=True)
    if token is not None:
        (directory / "token").write_text(token, encoding="utf-8")
    return directory


def _github_connected_settings(
    tmp_path: Path,
    token: str | None,
    source_address: str = "FlexOr2/atelier-2",
    source_ref: str | None = "main",
) -> HostSettings:
    """Settings whose store has a GitHub record for project 'studio'.

    The defaults are the durable shape `atelier2 connect` writes. Individual
    corruption tests can override a detail because no serve flag carries a
    repository fact; serve must compose or refuse exactly this stored state.
    """

    credential_directory = _github_credential_directory(tmp_path, token)
    root = tmp_path / "operator-project"
    if not root.is_dir():
        git_project(root, declaring_verification(["/bin/true"]))
    engine = dbos_runtime.create_canonical_engine(tmp_path / "durable.sqlite")
    try:
        initialize_schema(engine)
        append_project_root(engine, ProjectId("studio"), root)
        channel = DbosHostConfigurationChannel(engine)
        connected = connect_project_source(
            "studio",
            "github",
            source_address,
            credential_directory,
            "personal-access-token",
            "felix",
            channel,
            channel,
            source_ref=source_ref,
        )
        assert isinstance(connected, ProjectSourceConnectionPublished)
    finally:
        engine.dispose()
    return served_settings(tmp_path, project_id=ProjectId("studio"))


def test_a_connected_project_composes_the_live_open_pr_adapter_from_the_record(
    tmp_path: Path,
) -> None:
    # A token file must exist because the adapter reads it by reference when it
    # opens; a valid one lets the live factory bind without any network call.
    # The adapter carries the credential reference but does no network work at
    # composition; an unmatched readback later enters reconciliation.
    _app, runtime = compose_application(
        _github_connected_settings(tmp_path, "gho_a_test_scenario_token")
    )
    try:
        assert (
            runtime.effect_adapter_binding.operational_identity.value
            == "FlexOr2/atelier-2"
        )
    finally:
        runtime.close()


@pytest.mark.parametrize("token", [None, "", "   \n"])
def test_a_connected_project_without_a_readable_token_refuses_to_serve(
    tmp_path: Path, token: str | None
) -> None:
    # Missing, empty, and whitespace-only token files each fail the whole start
    # rather than serving open-pr silently disabled (`#430`).
    with pytest.raises(GitHubCredentialUnresolvable):
        compose_application(_github_connected_settings(tmp_path, token))


def test_a_corrupt_current_github_record_without_a_ref_refuses_to_serve(
    tmp_path: Path,
) -> None:
    # The platform-neutral application port can represent this malformed row,
    # but the GitHub CLI boundary never writes it and composition refuses it.
    settings = _github_connected_settings(
        tmp_path, "gho_a_test_scenario_token", source_ref=None
    )

    with pytest.raises(
        GitHubConnectionUncomposable, match="owner/name with one base ref"
    ):
        compose_application(settings)


@pytest.mark.parametrize(
    "flag",
    [
        "--github-credential-directory",
        "--github-repository-owner",
        "--github-repository-name",
        "--github-repository-base-branch",
    ],
)
def test_the_superseded_github_flags_are_refused_as_unrecognized_arguments(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], flag: str
) -> None:
    # The flags died with the connection record's arrival (`#567`); argparse's
    # own refusal is the whole acceptance shape, with no tombstone behind it.
    with pytest.raises(SystemExit) as refusal:
        main(serve_arguments(tmp_path, flag, "anything"))

    assert refusal.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


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
        main(claude_serve_arguments(tmp_path, settings, "--host", bind))

    assert refusal.value.code == 2
    assert "loopback" in capsys.readouterr().err


@pytest.mark.parametrize(
    "bind",
    ["0.0.0.0", "::", "192.168.1.10", "localhost"],
)
def test_a_record_composed_live_github_serve_binds_loopback_only(
    tmp_path: Path, bind: str
) -> None:
    # A non-loopback bind would let any network peer open PRs with the
    # operator's token, because starting a run is unauthenticated on this API.
    # The loopback-bound composition of the same record is the live-adapter
    # test above.
    settings = _github_connected_settings(tmp_path, "gho_a_test_scenario_token")

    with pytest.raises(ValueError, match="loopback"):
        compose_application(replace(settings, host=bind))


def test_a_record_composed_start_refuses_a_legacy_agent_completion_without_receipt(
    tmp_path: Path,
) -> None:
    # This is the old checkpoint: the agent had already advanced the sink run
    # to COMPLETED, but its still-recoverable node workflow had not redeemed
    # the grant. Current runs settle that redemption before either advance, so
    # the live-GitHub host refuses this persisted shape before recovery launches.
    settings = _github_connected_settings(tmp_path, "gho_a_test_scenario_token")
    run = RunId("v3/agent-open-pr-owing")
    seeded = DbosRuntime(
        DbosRuntimeSettings(
            tmp_path / "durable.sqlite",
            "composition-test",
            agent_scratch_root=agent_scratch_root(tmp_path),
        ),
        LoopbackEffectAdapterFactory(
            tmp_path / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("local"),
        ),
        (open_pr_agent_executor_factory(PR_SPEC),),
    )
    seeded.initialize_storage()
    try:
        workflow, bindings = publish_open_pr_agent_run(seeded, granted=True)
        started = DbosDurableRunStarter(
            seeded.engine,
            seeded.settings,
            seeded.agent_executor_registry,
        ).start_published(
            StartPublishedRunRequestV2(run, workflow.revision_hash, bindings)
        )
        assert isinstance(started, DurableRunCreated)
        driving_id = seed_current_node_attempt(seeded, run, AGENT_ATTEMPT_ORDINAL)
        complete_run(seeded, run)
        seed_workflow_status(seeded, driving_id, "PENDING")
    finally:
        seeded.close()

    with pytest.raises(LegacyAgentOpenPrCompletionWithoutReceipt, match=run.value):
        compose_application(settings)


def test_an_unconformant_claude_executable_does_not_kill_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pin stays; the house stays. Binding that executor is the later refusal."""

    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    settings = claude_subscription_deployment(
        deployment, INERT_CLAUDE, version="2.1.222"
    )
    monkeypatch.setenv("PATH", settings.search_path)
    captured: dict[str, HostSettings] = {}

    def fake_serve(host_settings: HostSettings) -> None:
        captured["settings"] = host_settings

    monkeypatch.setattr("atelier2.host.serve", fake_serve)

    assert main(claude_serve_arguments(tmp_path, settings)) == 0
    served = captured["settings"]
    assert served.claude_subscription is not None
    assert served.claude_start_refusal is not None
    assert "not 2.1.222" in served.claude_start_refusal


def test_an_unattested_codex_profile_does_not_kill_serve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dirty Codex credentials used to crash-loop systemd. The house stays up."""

    deployment = tmp_path / "codex-deployment"
    deployment.mkdir()
    settings = codex_subscription_deployment(deployment)
    monkeypatch.setenv("PATH", settings.search_path)
    captured: dict[str, HostSettings] = {}

    def fake_serve(host_settings: HostSettings) -> None:
        captured["settings"] = host_settings

    monkeypatch.setattr("atelier2.host.serve", fake_serve)

    assert (
        main(
            serve_arguments(
                tmp_path,
                "--agent-scratch-root",
                str(agent_scratch_root(tmp_path)),
                "--codex-executable",
                str(settings.executable),
                "--codex-credential-directory",
                str(settings.credential_directory),
            )
        )
        == 0
    )
    served = captured["settings"]
    assert served.codex_subscription is not None
    assert served.codex_start_refusal is not None


@pytest.mark.proves("an-unstartable-executor-does-not-kill-serve")
def test_an_unstartable_claude_executor_leaves_the_house_serving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Serve answers health; binding the refused executor is today's named problem."""

    claude_root = tmp_path / "claude-deployment"
    claude_root.mkdir()
    claude = claude_subscription_deployment(
        claude_root, INERT_CLAUDE, version="2.1.222"
    )
    grok_root = tmp_path / "grok-deployment"
    grok_root.mkdir()
    grok = grok_subscription_deployment(grok_root, INTROSPECTING_GROK)
    monkeypatch.setenv("PATH", claude.search_path)
    declared = _claude_subscription_settings(
        argparse.ArgumentParser(),
        argparse.Namespace(
            claude_executable=claude.executable,
            claude_credential_directory=claude.credential_directory,
            claude_workspace_tools=False,
        ),
    )
    assert declared.settings is not None
    assert declared.start_refusal is not None
    assert "not 2.1.222" in declared.start_refusal

    settings = served_settings(
        tmp_path,
        claude_subscription=declared.settings,
        claude_start_refusal=declared.start_refusal,
        grok_subscription=grok,
    )
    app, runtime = compose_application(settings)
    try:
        with TestClient(app) as client:
            health = client.get(API_PREFIX + "/health")
            assert health.status_code == 200
            assert health.json()["status"] == "serving"
        assert runtime.agent_executor_registry.keys == frozenset(
            {CLAUDE_SUBSCRIPTION_EXECUTOR_KEY, GROK_SUBSCRIPTION_EXECUTOR_KEY}
        )
        catalog = DbosAgentConfigurationCatalog(
            runtime.engine, runtime.agent_executor_registry
        )
        auth = AuthProfileRevision(
            "claude-unstartable", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
        configuration = AgentConfigurationRevision(
            "claude-opus-4-1",
            auth.revision_hash,
            CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(configuration),
            AgentConfigurationRevisionCreated,
        )
        grok_auth = AuthProfileRevision(
            "grok-healthy", 1, ProviderId("xai"), AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(grok_auth),
            AuthProfileRevisionCreated,
        )
        grok_configuration = AgentConfigurationRevision(
            "grok-4",
            grok_auth.revision_hash,
            GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(grok_configuration),
            AgentConfigurationRevisionCreated,
        )
        # Pin the real catalog producing the exact pair the reprobe exemption
        # depends on, not a fake standing in for it: before any receipt
        # exists, Claude is unstartable structurally (its factory is
        # unavailable) while Grok is unstartable only evidentially (nothing
        # has proven it live yet) -- the two questions genuinely disagree here,
        # answered by `DbosAgentConfigurationCatalog` itself.
        pre_receipt_listed = catalog.list_agent_configuration_revisions(None, 50)
        assert isinstance(pre_receipt_listed, AgentConfigurationRevisionPage)
        pre_receipt_pairs = {
            item.revision.model: (item.startable, item.structurally_startable)
            for item in pre_receipt_listed.items
        }
        assert pre_receipt_pairs == {
            "claude-opus-4-1": (False, False),
            "grok-4": (False, True),
        }

        assert settings.provider_probe_receipt_directory is not None
        settings.provider_probe_receipt_directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        claude_receipt = ProviderProbeReceipt(
            ProviderProbeVectorId("headless-claude-opus-4-1"),
            configuration.revision_hash,
            WorkflowRevisionHash("b" * 64),
            settings.source_commit,
            recorded_instant(now - timedelta(minutes=1)),
            recorded_instant(now + timedelta(hours=1)),
            ProviderProbeResult.SUCCEEDED,
            RunId("provider-canary/claude-opus-4-1-fixture"),
            terminal_hash=Sha256Hash("e" * 64),
        )
        (
            settings.provider_probe_receipt_directory / "claude-opus-4-1.json"
        ).write_bytes(claude_receipt.canonical_bytes())
        grok_receipt = ProviderProbeReceipt(
            ProviderProbeVectorId("headless-grok-4"),
            grok_configuration.revision_hash,
            WorkflowRevisionHash("b" * 64),
            settings.source_commit,
            recorded_instant(now - timedelta(minutes=1)),
            recorded_instant(now + timedelta(hours=1)),
            ProviderProbeResult.SUCCEEDED,
            RunId("provider-canary/grok-4-fixture"),
            terminal_hash=Sha256Hash("d" * 64),
        )
        (settings.provider_probe_receipt_directory / "grok-4.json").write_bytes(
            grok_receipt.canonical_bytes()
        )
        # Even with a valid, matching receipt now on file, Claude's own
        # declared start refusal (the version mismatch above) keeps its
        # factory unavailable -- `is_startable` refuses on that structural
        # ground alone, proving the refusal is not merely "no receipt yet".
        assert not runtime.agent_executor_registry.is_startable(
            CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
            AgentExecutionCapability.HEADLESS,
            configuration.revision_hash,
        )
        listed = catalog.list_agent_configuration_revisions(None, 50)
        assert isinstance(listed, AgentConfigurationRevisionPage)
        startability = {item.revision.model: item.startable for item in listed.items}
        assert startability == {"claude-opus-4-1": False, "grok-4": True}
        assert runtime.agent_executor_registry.is_startable(
            GROK_SUBSCRIPTION_EXECUTOR_KEY,
            AgentExecutionCapability.HEADLESS,
            grok_configuration.revision_hash,
        )
        publish_checked_model_registry(
            runtime.engine, ProviderId("anthropic"), (configuration,)
        )
        DbosCatalogStore(runtime.engine).publish_revision(ANY_JSON_SCHEMA)
        workflow = WorkflowRevision(HOST_DOCUMENT)
        DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
        starter = DbosDurableRunStarter(
            runtime.engine,
            runtime.settings,
            runtime.agent_executor_registry,
        )
        refused = starter.start_published(
            StartPublishedRunRequestV2(
                RunId("claude/unstartable"),
                workflow.revision_hash,
                AgentBindingSet(
                    (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
                ),
            )
        )
        assert isinstance(refused, DurableAgentExecutorBindingUnavailable)
        with runtime.engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(runs)) == 0
            assert (
                connection.scalar(
                    sa.select(sa.func.count()).select_from(agent_attempts)
                )
                == 0
            )
    finally:
        runtime.close()


def test_a_claude_deployment_without_bubblewrap_refuses_to_serve(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server's own search path is what the launched provider receives."""

    deployment = tmp_path / "claude-deployment"
    deployment.mkdir()
    settings = claude_subscription_deployment(deployment, INERT_CLAUDE)
    monkeypatch.setenv("PATH", str(tmp_path / "no-tools-here"))

    with pytest.raises(SystemExit) as refusal:
        main(claude_serve_arguments(tmp_path, settings))

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


@pytest.mark.proves("the-served-project-door-reveals-only-its-public-reference")
def test_real_console_launcher_serves_one_opaque_project_resource(
    tmp_path: Path,
) -> None:
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("<main>project launcher</main>")
    project_root = tmp_path / "operator-project"
    git_project(project_root, declaring_verification(["/bin/true"]))
    port = free_port()
    project_id = ProjectId("studio")
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
        "project-launcher-test",
        "--application-version",
        "project-launcher-v1",
        "--source-commit",
        "exact-commit",
        "--source-tree",
        "exact-tree",
        "--frontend-dist",
        str(frontend),
        "--project-id",
        project_id.value,
        "--project-root",
        str(project_root),
        "--port",
        str(port),
    ]

    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        wait_for_health(port)
        with urlopen(f"http://127.0.0.1:{port}{PROJECTS_PATH}", timeout=2) as response:
            listed = json.load(response)
        (project,) = listed["items"]
        public_reference = project["public_project_reference"]
        with urlopen(
            "http://127.0.0.1:"
            f"{port}{PROJECT_PATH.format(public_project_reference=public_reference)}",
            timeout=2,
        ) as response:
            detailed = json.load(response)
        unknown_reference = encode_public_project_reference(ProjectId("other"))
        with pytest.raises(HTTPError) as refusal:
            urlopen(
                "http://127.0.0.1:"
                f"{port}{PROJECT_PATH.format(public_project_reference=unknown_reference)}",
                timeout=2,
            )
        unknown = json.load(refusal.value)
    finally:
        child_error = stop_child_process(child)

    assert child.returncode == 0, child_error.decode(errors="replace")
    assert listed == {"items": [{"public_project_reference": public_reference}]}
    assert detailed == project
    assert unknown["type"].endswith("project-unknown")
    exposed = repr((listed, detailed))
    assert project_id.value not in exposed
    assert str(project_root.resolve()) not in exposed


def stop_child_process(child: subprocess.Popen[bytes]) -> bytes:
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


@pytest.mark.proves("an-instance-value-is-set-where-the-instance-is-configured")
def test_a_served_instance_reads_the_page_size_it_was_configured_with(
    tmp_path: Path,
) -> None:
    """A knob is a knob only if setting it changes what the instance does.

    Every one of these values was a module constant the host baked in: correct
    for one machine, unreachable from any other. What makes this a channel rather
    than a rename is that the value an operator sets is the value the composed
    API enforces.
    """
    app, runtime = compose_application(served_settings(tmp_path, event_page_size=7))

    try:
        # Asked of the composed application, not of the record that carries the
        # number: a settings field nobody reads would satisfy the record and
        # leave the served instance on its old value.
        assert app.state.api_context.limits.event_page_size.value == 7
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("flag", "value", "refusal"),
    [
        # One case per owner that holds a rule, because a single case only proves
        # the owner it happens to reach. The last two travelled a different path
        # and escaped as a traceback until the record asked their owner early.
        (
            "--event-poll-delay-multiplier",
            "1.0",
            "multiplier must be greater than one",
        ),
        ("--event-page-size", "0", "page limit must be an integer from 1 to 100"),
        (
            "--sqlite-lock-timeout-seconds",
            "-1",
            "SQLite lock timeout must be positive",
        ),
        (
            "--agent-termination-grace-seconds",
            "0",
            "agent termination grace must be positive",
        ),
    ],
)
@pytest.mark.proves("an-instance-value-outside-its-range-is-refused-by-name")
def test_the_operator_reads_the_owners_words_when_a_knob_is_out_of_range(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
    refusal: str,
) -> None:
    """A refusal is only a refusal if it reaches the person who typed the value."""
    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("index")

    with pytest.raises(SystemExit):
        main(
            [
                "serve",
                "--database",
                str(tmp_path / "d.sqlite"),
                "--effect-store",
                str(tmp_path / "e.sqlite"),
                "--effect-adapter-revision",
                "loopback-v1",
                "--effect-destination",
                "local",
                "--application-version",
                "t",
                "--source-commit",
                "c",
                "--source-tree",
                "t",
                "--frontend-dist",
                str(frontend),
                flag,
                value,
            ]
        )

    printed = capsys.readouterr().err
    assert refusal in printed
    assert "Traceback" not in printed


@pytest.mark.proves("an-instance-value-is-set-where-the-instance-is-configured")
def test_the_store_waits_as_long_as_this_instance_was_configured_to_wait(
    tmp_path: Path,
) -> None:
    """The same claim on the store side, asked of the runtime that was built."""
    _app, runtime = compose_application(
        served_settings(tmp_path, sqlite_lock_timeout_seconds=3.5)
    )

    try:
        assert runtime.settings.sqlite_lock_timeout_seconds == 3.5
    finally:
        runtime.close()
