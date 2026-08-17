from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.schema import runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.grok_subscription import (
    CONFORMANT_GROK_VERSIONS,
    GROK_SUBSCRIPTION_EXECUTOR_KEY,
    GROK_SUBSCRIPTION_FRAME_BYTES,
    GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY,
    GrokContainmentUnattested,
    GrokExecutableUnsupported,
    GrokSubscriptionAuthModeUnsupported,
    GrokSubscriptionExecutorFactory,
    GrokSubscriptionSettings,
    verify_grok_capability,
)
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import RunId, WorkflowRevision, WorkflowRevisionHash
from atelier2.host import _grok_subscription_settings
from atelier2.host.serving import HostSettings, compose_application
from atelier2.ports.agent_configurations import (
    AgentConfigurationRevisionCreated,
    AuthProfileRevisionCreated,
)
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from atelier2.ports.durable_runs import (
    DurableAgentExecutorCapabilityUnavailable,
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_scratch_root,
    leased_directory_identity,
)

MEASURED_GROK_VERSION = "1.0.4"
HOST_DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
"""
INTROSPECTING_GROK = """
import json, os, sys, tomllib
from pathlib import Path
if "--version" in sys.argv:
    print("grok 1.0.4 (d846eb93d9) [stable]")
    raise SystemExit(0)
if "inspect" in sys.argv:
    home = Path(os.environ["GROK_HOME"])
    compatibility = tomllib.loads((home / "config.toml").read_text())["compat"]
    cells = [
        {
            "vendor": vendor,
            "surface": surface,
            "enabled": compatibility[vendor].get(surface) is not False,
            "source": "config",
        }
        for vendor, surfaces in (
            ("cursor", ("skills", "rules", "agents", "mcps", "hooks", "sessions")),
            ("claude", ("skills", "rules", "agents", "mcps", "hooks", "sessions")),
            ("codex", ("sessions",)),
        )
        for surface in surfaces
    ]
    json.dump(
        {
            "plugins": [], "hooks": [], "mcpServers": [], "skills": [],
            "marketplaces": [], "lspServers": [], "projectInstructions": [],
            "configSources": {"layers": [{"role": "user", "path": str(home / "config.toml")}]},
            "permissions": {"sources": []},
            "agents": [{"name": "general-purpose", "source": {"type": "builtin"}}],
            "externalCompat": {"remoteSettingsLoaded": False, "cells": cells},
        },
        sys.stdout,
    )
    raise SystemExit(0)
prompt_file = None
args = sys.argv[1:]
for index, argument in enumerate(args):
    if argument == "--prompt-file" and index + 1 < len(args):
        prompt_file = args[index + 1]
job = Path(prompt_file).read_bytes() if prompt_file else b""
session = Path(os.environ["GROK_HOME"]) / "sessions" / "headless"
session.mkdir(parents=True)
(session / "updates.jsonl").write_bytes(job + b"\\nprovider response")
json.dump(
    {
        "text": json.dumps(
            {
                "arguments": sys.argv,
                "working_directory": os.getcwd(),
                "environment": dict(os.environ),
                "job": job.decode("utf-8"),
                "stdin": sys.stdin.buffer.read().decode("utf-8"),
            }
        )
    },
    sys.stdout,
)
"""


def _write_executable(path: Path, source: str) -> Path:
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def grok_subscription_deployment(
    tmp_path: Path, source: str
) -> GrokSubscriptionSettings:
    executable = _write_executable(tmp_path / "grok", source)
    workspace = tmp_path / "workspace"
    credentials = tmp_path / "grok-home"
    workspace.mkdir()
    credentials.mkdir()
    authentication = credentials / "auth.json"
    authentication.write_bytes(b"{}")
    authentication.chmod(0o600)
    return GrokSubscriptionSettings(
        executable, workspace, credentials, os.environ.get("PATH", "/usr/bin")
    )


def subscription_request(
    model: str = "grok-4",
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION,
    job: bytes = b"Reply with the single word pong",
) -> AgentExecutionRequestV2:
    auth = AuthProfileRevision("grok-primary", 1, ProviderId("xai"), auth_mode)
    configuration = AgentConfigurationRevision(
        model,
        auth.revision_hash,
        GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    run_id = RunId("run-grok")
    revision_hash = WorkflowRevisionHash("3" * 64)
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, "build"),
        run_id,
        revision_hash,
        "build",
        ResolvedAgentBinding(AgentRole("builder"), configuration, auth),
        GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY,
        job,
    )


def leased_workspace(root: Path, name: str = "attempt-workspace") -> Path:
    """The blank directory an attempt's lease starts this provider in."""

    workspace = root / name
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def leased(command: AgentProcessCommand, workspace: Path) -> AgentProcessInvocation:
    """One invocation for a test that houses the leased directory itself."""

    return AgentProcessInvocation(
        command,
        leased_directory_identity(
            agent_attempt_execution(subscription_request()).attempt_id, workspace
        ),
    )


def launched(command: AgentProcessCommand, workspace: Path) -> AgentProcessCompletion:
    """Run one prepared command exactly where the attempt leased its ground."""

    completed = subprocess.run(
        command.arguments,
        cwd=workspace,
        env=dict(command.environment),
        input=command.standard_input,
        capture_output=True,
        check=False,
    )
    return AgentProcessCompletion(
        completed.returncode, completed.stdout, completed.stderr
    )


def test_a_headless_run_carries_the_bound_model_job_and_only_the_credential_boundary(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    request = subscription_request(model="grok-4", job=b"draw the owl")

    workspace = leased_workspace(tmp_path)
    command = executor.prepare_process(request)
    invocation = leased(command, workspace)
    # The job path is private per execution, so it is read back from the vector
    # rather than recomputed: a predictable name is what a symlink preys on.
    job_file = Path(command.arguments[command.arguments.index("--prompt-file") + 1])

    assert job_file.parent.parent == settings.workspace
    assert command.arguments == (
        str(settings.executable),
        "--output-format",
        "json",
        "--model",
        "grok-4",
        "--prompt-file",
        str(job_file),
        "--tools=",
        "--permission-mode",
        "dontAsk",
        "--no-memory",
        "--no-subagents",
        "--disable-web-search",
        "--max-turns",
        "1",
    )
    invocation_home = Path(dict(command.environment)["GROK_HOME"])
    # The secret channel keeps its own directory; the ground the provider stands
    # on is the attempt's lease, and the two are deliberately not one place.
    assert invocation.lease.working_directory == workspace
    assert invocation_home != workspace
    assert invocation_home != settings.credential_directory
    assert invocation_home.parent == settings.workspace
    assert command.environment == (
        # HOME is named on purpose: measured on grok 1.0.4, a child without it
        # resolves the invoking account's home and loads that profile's
        # plugins, hooks and skills.
        ("HOME", str(invocation_home)),
        ("GROK_HOME", str(invocation_home)),
        ("PATH", settings.search_path),
    )
    assert command.standard_input == b""
    assert command.standard_output_frame_bytes == GROK_SUBSCRIPTION_FRAME_BYTES
    assert b"draw the owl" not in " ".join(command.arguments).encode()
    result = executor.decode_process_completion(
        invocation, launched(command, workspace)
    )
    assert isinstance(result, AgentExecutionResult)
    observed = json.loads(result.output_bytes)
    assert observed["arguments"][0] == str(settings.executable)
    assert observed["job"] == "draw the owl"
    assert observed["stdin"] == ""
    assert observed["environment"]["GROK_HOME"] == str(invocation_home)
    assert observed["environment"]["PATH"] == settings.search_path
    assert observed["environment"]["HOME"] == str(invocation_home)
    assert "XAI_API_KEY" not in observed["environment"]
    assert (invocation_home / "sessions" / "headless" / "updates.jsonl").exists()
    executor.release_credential_channel(command)
    assert not invocation_home.exists()
    assert (settings.credential_directory / "auth.json").read_bytes() == b"{}"


def test_a_non_subscription_profile_is_refused_before_any_invocation(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    request = subscription_request(auth_mode=AuthMode.API_KEY)

    with pytest.raises(GrokSubscriptionAuthModeUnsupported):
        executor.prepare_process(request)


def test_an_unusable_envelope_is_a_typed_process_failure(tmp_path: Path) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        AgentProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        ),
        tmp_path,
    )

    assert executor.decode_process_completion(
        invocation, AgentProcessCompletion(1, b'{"text":"no"}', b"")
    ) == AgentExecutionFailure(AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY)
    assert executor.decode_process_completion(
        invocation, AgentProcessCompletion(0, b"not-json", b"")
    ) == AgentExecutionFailure(AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY)
    assert executor.decode_process_completion(
        invocation, AgentProcessCompletion(0, b'{"result":"wrong field"}', b"")
    ) == AgentExecutionFailure(AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY)


def test_only_the_measured_grok_release_is_admitted(tmp_path: Path) -> None:
    assert CONFORMANT_GROK_VERSIONS == {(1, 0, 4)}
    other = grok_subscription_deployment(
        tmp_path,
        "import sys\nprint('grok 1.0.3 (old) [stable]')\n",
    )
    with pytest.raises(GrokExecutableUnsupported, match="1.0.4"):
        verify_grok_capability(other.executable)


ATTESTING_GROK = """
import json, os, sys
from pathlib import Path
if "--version" in sys.argv:
    print("grok 1.0.4 (d846eb93d9) [stable]")
    raise SystemExit(0)
if "inspect" in sys.argv:
    inspected = dict(INSPECTED)
    if inspected.get("configSources") == "exact-job-config":
        inspected["configSources"] = {
            "layers": [
                {
                    "role": "user",
                    "path": str(Path(os.environ["GROK_HOME"]) / "config.toml"),
                }
            ]
        }
    print(json.dumps(inspected))
    raise SystemExit(0)
raise SystemExit(3)
"""

INERT_PROFILE = {
    "grokVersion": MEASURED_GROK_VERSION,
    "plugins": [],
    "hooks": [],
    "mcpServers": [],
    "skills": [],
    "marketplaces": [],
    "lspServers": [],
    "projectInstructions": [],
    "configSources": "exact-job-config",
    "permissions": {"sources": []},
    "agents": [{"name": "general-purpose", "source": {"type": "builtin"}}],
    "externalCompat": {
        "remoteSettingsLoaded": False,
        "cells": [
            {
                "vendor": vendor,
                "surface": surface,
                "enabled": False,
                "source": "config",
            }
            for vendor, surfaces in (
                (
                    "cursor",
                    ("skills", "rules", "agents", "mcps", "hooks", "sessions"),
                ),
                (
                    "claude",
                    ("skills", "rules", "agents", "mcps", "hooks", "sessions"),
                ),
                ("codex", ("sessions",)),
            )
            for surface in surfaces
        ],
    },
}


def attesting_deployment(
    tmp_path: Path, profile: dict[str, object]
) -> GrokSubscriptionSettings:
    source = f"INSPECTED = {profile!r}\n" + ATTESTING_GROK
    return grok_subscription_deployment(tmp_path, source)


def test_a_preplaced_symlink_at_the_job_path_receives_no_job_bytes(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    victim = tmp_path / "victim"
    victim.write_bytes(b"the operator's bytes")
    request = subscription_request(job=b"attacker payload")
    predictable = (
        settings.workspace / f"atelier2-grok-job-{request.node_execution_id.value}"
    )
    predictable.symlink_to(victim)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    invocation = executor.prepare_process(request)

    assert victim.read_bytes() == b"the operator's bytes"
    job_file = Path(
        invocation.arguments[invocation.arguments.index("--prompt-file") + 1]
    )
    assert job_file.read_bytes() == b"attacker payload"
    assert not job_file.is_symlink()
    assert stat.S_IMODE(job_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(job_file.parent.stat().st_mode) == 0o700
    executor.close()


def test_job_bytes_outlive_neither_a_completion_nor_a_close(tmp_path: Path) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    invocation = executor.prepare_process(subscription_request())
    job_file = Path(
        invocation.arguments[invocation.arguments.index("--prompt-file") + 1]
    )
    assert job_file.exists()

    executor.decode_process_completion(
        leased(invocation, tmp_path), AgentProcessCompletion(1, b"", b"")
    )
    executor.release_credential_channel(invocation)

    assert not job_file.exists()
    assert not job_file.parent.exists()

    second = executor.prepare_process(subscription_request())
    abandoned = Path(second.arguments[second.arguments.index("--prompt-file") + 1])
    assert abandoned.exists()

    executor.close()

    assert not abandoned.exists()
    assert list(settings.workspace.iterdir()) == []


def test_releasing_one_concurrent_invocation_preserves_the_other(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    first = executor.prepare_process(subscription_request(job=b"first"))
    second = executor.prepare_process(subscription_request(job=b"second"))
    first_home = Path(dict(first.environment)["GROK_HOME"])
    second_home = Path(dict(second.environment)["GROK_HOME"])

    executor.release_credential_channel(first)

    assert not first_home.exists()
    assert second_home.is_dir()
    assert (
        Path(second.arguments[second.arguments.index("--prompt-file") + 1]).read_bytes()
        == b"second"
    )
    executor.release_credential_channel(second)
    assert list(settings.workspace.iterdir()) == []


def test_any_enabled_external_compatibility_cell_refuses_the_exact_launch(
    tmp_path: Path,
) -> None:
    profile = dict(INERT_PROFILE)
    compatibility = dict(INERT_PROFILE["externalCompat"])
    cells = [dict(cell) for cell in compatibility["cells"]]
    cells[0]["enabled"] = True
    compatibility["cells"] = cells
    profile["externalCompat"] = compatibility
    settings = attesting_deployment(tmp_path, profile)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    with pytest.raises(GrokContainmentUnattested, match="externalCompat"):
        executor.prepare_process(subscription_request())

    assert list(settings.workspace.iterdir()) == []


def test_real_host_runtime_supervisor_executes_and_cleans_without_a_billed_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deployment = tmp_path / "deployment"
    deployment.mkdir()
    candidate = grok_subscription_deployment(deployment, INTROSPECTING_GROK)
    monkeypatch.setenv("PATH", candidate.search_path)
    parser = argparse.ArgumentParser()
    resolved = _grok_subscription_settings(
        parser,
        argparse.Namespace(
            grok_executable=candidate.executable,
            grok_workspace=candidate.workspace,
            grok_credential_directory=candidate.credential_directory,
        ),
    )
    assert resolved == candidate

    frontend = tmp_path / "frontend"
    (frontend / "assets").mkdir(parents=True)
    (frontend / "index.html").write_text("index", encoding="utf-8")
    common = {
        "database_path": tmp_path / "durable.sqlite",
        "effect_store_path": tmp_path / "effects.sqlite",
        "effect_adapter_revision": "loopback-v1",
        "effect_destination": "grok-host-proof",
        "application_version": "grok-host-proof",
        "source_commit": "proof",
        "source_tree": "proof",
        "frontend_dist": frontend,
        "agent_scratch_root": agent_scratch_root(tmp_path),
        "grok_subscription": resolved,
    }
    with pytest.raises(ValueError, match="loopback"):
        HostSettings(**common, host="0.0.0.0")

    _app, runtime = compose_application(HostSettings(**common))
    runtime.initialize_storage()
    try:
        assert runtime.agent_executor_registry.keys == frozenset(
            {GROK_SUBSCRIPTION_EXECUTOR_KEY}
        )
        catalog = DbosAgentConfigurationCatalog(
            runtime.engine, runtime.agent_executor_registry
        )
        auth = AuthProfileRevision(
            "grok-host", 1, ProviderId("xai"), AuthMode.SUBSCRIPTION
        )
        assert isinstance(
            catalog.publish_auth_profile_revision(auth), AuthProfileRevisionCreated
        )
        interactive = AgentConfigurationRevision(
            "grok-4",
            auth.revision_hash,
            GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.INTERACTIVE,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        headless = AgentConfigurationRevision(
            "grok-4",
            auth.revision_hash,
            GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.HEADLESS,
            AgentConfigurationRevisionFormatVersion.V2,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(interactive),
            AgentConfigurationRevisionCreated,
        )
        assert isinstance(
            catalog.publish_agent_configuration_revision(headless),
            AgentConfigurationRevisionCreated,
        )
        workflow = WorkflowRevision(HOST_DOCUMENT)
        DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
        starter = DbosDurableRunStarter(
            runtime.engine, runtime.settings, runtime.agent_executor_registry
        )
        refused = starter.start_published(
            StartPublishedRunRequestV2(
                RunId("grok/interactive-refused"),
                workflow.revision_hash,
                AgentBindingSet(
                    (AgentBinding(AgentRole("builder"), interactive.revision_hash),)
                ),
            )
        )
        assert isinstance(refused, DurableAgentExecutorCapabilityUnavailable)
        started = starter.start_published(
            StartPublishedRunRequestV2(
                RunId("grok/headless"),
                workflow.revision_hash,
                AgentBindingSet(
                    (AgentBinding(AgentRole("builder"), headless.revision_hash),)
                ),
            )
        )
        assert isinstance(started, DurableRunCreated)
        runtime.launch()
        deadline = time.monotonic() + 10
        state = ""
        while time.monotonic() < deadline:
            with runtime.engine.connect() as connection:
                state = str(
                    connection.scalar(
                        runs.select()
                        .with_only_columns(runs.c.state)
                        .where(runs.c.run_id == "grok/headless")
                    )
                )
            if state == "COMPLETED":
                break
            time.sleep(0.02)
        assert state == "COMPLETED"
        assert list(candidate.workspace.iterdir()) == []
        assert (candidate.credential_directory / "auth.json").read_bytes() == b"{}"
    finally:
        runtime.close()


def test_a_headless_run_leaves_memory_subagents_and_web_search_disabled(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    invocation = executor.prepare_process(subscription_request())

    assert "--no-memory" in invocation.arguments
    assert "--no-subagents" in invocation.arguments
    assert "--disable-web-search" in invocation.arguments
    executor.close()


def test_an_inert_profile_is_attested(tmp_path: Path) -> None:
    settings = attesting_deployment(tmp_path, INERT_PROFILE)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    invocation = executor.prepare_process(subscription_request())

    executor.release_credential_channel(invocation)


@pytest.mark.parametrize(
    ("surface", "discovered"),
    [
        ("plugins", [{"name": "marketplace-plugin"}]),
        ("hooks", [{"event": "PreToolUse"}]),
        ("mcpServers", [{"name": "filesystem"}]),
        ("skills", [{"name": "deploy"}]),
        ("marketplaces", [{"name": "community"}]),
        ("lspServers", [{"name": "pyright"}]),
        ("projectInstructions", [{"path": "AGENTS.md"}]),
        ("configSources", [{"path": "/home/operator/.grok/config.toml"}]),
    ],
)
def test_a_discovered_trust_surface_refuses_to_serve(
    tmp_path: Path, surface: str, discovered: list[object]
) -> None:
    profile = dict(INERT_PROFILE)
    profile[surface] = discovered

    settings = attesting_deployment(tmp_path, profile)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    with pytest.raises(GrokContainmentUnattested, match=surface):
        executor.prepare_process(subscription_request())

    assert list(settings.workspace.iterdir()) == []


def test_a_discovered_permission_source_refuses_to_serve(tmp_path: Path) -> None:
    profile = dict(INERT_PROFILE)
    profile["permissions"] = {"sources": ["/etc/claude-code/managed-settings.json"]}

    settings = attesting_deployment(tmp_path, profile)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    with pytest.raises(GrokContainmentUnattested, match="permissions.sources"):
        executor.prepare_process(subscription_request())

    assert list(settings.workspace.iterdir()) == []


def test_a_non_builtin_agent_refuses_to_serve(tmp_path: Path) -> None:
    profile = dict(INERT_PROFILE)
    profile["agents"] = [{"name": "deployer", "source": {"type": "project"}}]

    settings = attesting_deployment(tmp_path, profile)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    with pytest.raises(GrokContainmentUnattested, match="agents"):
        executor.prepare_process(subscription_request())

    assert list(settings.workspace.iterdir()) == []


def test_an_unreadable_attestation_refuses_rather_than_assuming_containment(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(
        tmp_path, "import sys\nsys.exit(0 if '--version' in sys.argv else 9)\n"
    )

    executor = GrokSubscriptionExecutorFactory(settings).open()

    with pytest.raises(GrokContainmentUnattested):
        executor.prepare_process(subscription_request())

    assert list(settings.workspace.iterdir()) == []


def test_the_credential_channel_dies_while_the_leased_workspace_stands(
    tmp_path: Path,
) -> None:
    """Two owners, two disciplines, and the secret's is the shorter one.

    The attempt's workspace keeps what a provider left behind until the attempt
    is durably terminal, because that is evidence. The private home holding a
    copy of the operator's `auth.json` is not evidence, and it falls on every
    path -- after a decoded answer and after a refused one alike -- while the
    workspace it ran in is untouched.
    """

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    workspace = leased_workspace(tmp_path)

    for outcome in (AgentProcessCompletion(0, b'{"text":"ok"}', b""), None):
        command = executor.prepare_process(subscription_request())
        channel = Path(dict(command.environment)["GROK_HOME"])
        (workspace / "provider-left-this").write_text("kept", encoding="utf-8")
        assert channel.is_dir()
        assert (channel / "auth.json").exists()

        if outcome is not None:
            executor.decode_process_completion(leased(command, workspace), outcome)
        executor.release_credential_channel(command)

        assert not channel.exists()
        assert (workspace / "provider-left-this").read_text() == "kept"
    executor.close()
