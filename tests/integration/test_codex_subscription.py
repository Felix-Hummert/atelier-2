from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from atelier2.adapters.codex_subscription import (
    CODEX_SUBSCRIPTION_EXECUTOR_KEY,
    CODEX_SUBSCRIPTION_FRAME_BYTES,
    CODEX_SUBSCRIPTION_OPERATIONAL_IDENTITY,
    CONFORMANT_CODEX_VERSIONS,
    CodexContainmentUnattested,
    CodexExecutableUnsupported,
    CodexSandboxMode,
    CodexSubscriptionAuthModeUnsupported,
    CodexSubscriptionExecutorFactory,
    CodexSubscriptionSettings,
    attest_codex_containment,
    verify_codex_capability,
)
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agents import (
    MAXIMUM_AGENT_OUTPUT_BYTES_V2,
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
from atelier2.contracts.runs import RunId, WorkflowRevisionHash
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentProcessCompletion,
    AgentProcessInvocation,
)

MEASURED_CODEX_VERSION = "0.147.0"

# Stands in for codex-cli 0.147.0: it answers the three surfaces this executor
# uses -- `--version`, `doctor --json`, `sandbox` -- and writes what `exec`
# observed into the file named by `--output-last-message`, which is where the
# real CLI writes the agent's last message.
INTROSPECTING_CODEX = """
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
if "--version" in args or "-V" in args:
    print("codex-cli 0.147.0")
    raise SystemExit(0)


def flag_value(name):
    return args[args.index(name) + 1] if name in args else None


if args and args[0] == "doctor":
    report = json.loads(os.environ.get("FAKE_DOCTOR_REPORT", "{}"))
    json.dump(report, sys.stdout)
    raise SystemExit(int(os.environ.get("FAKE_DOCTOR_EXIT", "0")))

if args and args[0] == "sandbox":
    raise SystemExit(int(os.environ.get("FAKE_SANDBOX_EXIT", "0")))

answer_path = flag_value("--output-last-message")
observed = json.dumps(
    {
        "arguments": sys.argv,
        "working_directory": os.getcwd(),
        "environment": dict(os.environ),
        "stdin": sys.stdin.buffer.read().decode("utf-8"),
    }
)
answer = os.environ.get("FAKE_ANSWER", observed)
if os.environ.get("FAKE_WITHHOLD_ANSWER") != "1":
    Path(answer_path).write_text(answer)
sys.stdout.write(os.environ.get("FAKE_STDOUT", ""))
raise SystemExit(int(os.environ.get("FAKE_EXIT", "0")))
"""


def _contained_doctor_report(settings_home: Path, workspace: Path) -> dict[str, object]:
    """What `codex doctor --json` reports for a profile that loads nothing."""

    return {
        "schemaVersion": 1,
        "codexVersion": MEASURED_CODEX_VERSION,
        "checks": {
            "config.load": {
                "status": "ok",
                "details": {
                    "CODEX_HOME": str(settings_home),
                    "config.toml": [str(settings_home / "config.toml"), "missing"],
                    "cwd": str(workspace),
                    "mcp servers": "0",
                },
            },
            "mcp.config": {"status": "ok", "details": {}},
        },
    }


def _write_executable(path: Path, source: str) -> Path:
    path.write_text("#!/usr/bin/env python3\n" + source)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


def codex_subscription_deployment(
    tmp_path: Path,
    source: str = INTROSPECTING_CODEX,
    sandbox: CodexSandboxMode = CodexSandboxMode.READ_ONLY,
) -> CodexSubscriptionSettings:
    executable = _write_executable(tmp_path / "codex", source)
    workspace = tmp_path / "workspace"
    credentials = tmp_path / "codex-home"
    workspace.mkdir()
    credentials.mkdir()
    return CodexSubscriptionSettings(
        executable,
        workspace,
        credentials,
        os.environ.get("PATH", "/usr/bin"),
        sandbox,
    )


def subscription_request(
    model: str = "gpt-5.3-codex",
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION,
    job: bytes = b"Reply with the single word pong",
) -> AgentExecutionRequestV2:
    auth = AuthProfileRevision("codex-primary", 1, ProviderId("openai"), auth_mode)
    configuration = AgentConfigurationRevision(
        model,
        auth.revision_hash,
        CODEX_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
        AgentExecutionCapability.HEADLESS,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    run_id = RunId("run-codex")
    revision_hash = WorkflowRevisionHash("4" * 64)
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, revision_hash, "review"),
        run_id,
        revision_hash,
        "review",
        ResolvedAgentBinding(AgentRole("reviewer"), configuration, auth),
        CODEX_SUBSCRIPTION_OPERATIONAL_IDENTITY,
        job,
    )


def launched(
    invocation: AgentProcessInvocation, **provider_behaviour: str
) -> AgentProcessCompletion:
    completed = subprocess.run(
        invocation.arguments,
        cwd=invocation.working_directory,
        env=dict(invocation.environment) | provider_behaviour,
        input=invocation.standard_input,
        capture_output=True,
        check=False,
    )
    return AgentProcessCompletion(
        completed.returncode, completed.stdout, completed.stderr
    )


def test_a_headless_run_carries_the_bound_model_and_sandbox_with_the_job_off_argv(
    tmp_path: Path,
) -> None:
    settings = codex_subscription_deployment(
        tmp_path, sandbox=CodexSandboxMode.WORKSPACE_WRITE
    )
    executor = CodexSubscriptionExecutorFactory(settings).open()
    request = subscription_request(model="gpt-5.3-codex", job=b"draw the owl")

    invocation = executor.prepare_process(request)
    # The answer path is private per execution, so it is read back from the
    # vector rather than recomputed: a predictable name is what a symlink
    # preys on.
    answer_file = Path(
        invocation.arguments[invocation.arguments.index("--output-last-message") + 1]
    )

    assert answer_file.parent.parent == settings.workspace
    assert invocation.arguments == (
        str(settings.executable),
        "exec",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color",
        "never",
        "--model",
        "gpt-5.3-codex",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(settings.workspace),
        "--output-last-message",
        str(answer_file),
        "-",
    )
    assert invocation.working_directory == settings.workspace
    assert invocation.environment == (
        # HOME is containment, not convenience: a child without it resolves the
        # invoking account's own profile and loads that profile's trust.
        ("HOME", str(settings.credential_directory)),
        ("CODEX_HOME", str(settings.credential_directory)),
        ("PATH", settings.search_path),
    )
    # `codex exec` takes its prompt on stdin, so the job never reaches the
    # argument vector, where any account on the host could read it.
    assert invocation.standard_input == b"draw the owl"
    assert b"draw the owl" not in " ".join(invocation.arguments).encode()
    assert invocation.standard_output_frame_bytes == CODEX_SUBSCRIPTION_FRAME_BYTES

    result = executor.decode_process_completion(launched(invocation))

    assert isinstance(result, AgentExecutionResult)
    observed = json.loads(result.output_bytes)
    assert observed["arguments"][0] == str(settings.executable)
    assert observed["stdin"] == "draw the owl"
    assert observed["working_directory"] == str(settings.workspace)
    assert observed["environment"]["CODEX_HOME"] == str(settings.credential_directory)
    assert observed["environment"]["HOME"] == str(settings.credential_directory)
    assert "OPENAI_API_KEY" not in observed["environment"]


def test_a_non_subscription_profile_is_refused_before_any_invocation(
    tmp_path: Path,
) -> None:
    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()

    with pytest.raises(CodexSubscriptionAuthModeUnsupported):
        executor.prepare_process(subscription_request(auth_mode=AuthMode.API_KEY))


def test_the_executor_declares_its_provider_key_and_headless_capability(
    tmp_path: Path,
) -> None:
    factory = CodexSubscriptionExecutorFactory(codex_subscription_deployment(tmp_path))

    assert factory.key == CODEX_SUBSCRIPTION_EXECUTOR_KEY
    assert factory.key.provider_id == ProviderId("openai")
    assert factory.operational_identity == CODEX_SUBSCRIPTION_OPERATIONAL_IDENTITY
    assert factory.declared_capabilities == frozenset(
        {AgentExecutionCapability.HEADLESS}
    )


def test_only_the_measured_codex_release_is_admitted(tmp_path: Path) -> None:
    conformant = codex_subscription_deployment(tmp_path)

    assert (
        verify_codex_capability(conformant.executable, conformant.search_path)
        in CONFORMANT_CODEX_VERSIONS
    )

    other = tmp_path / "other"
    other.mkdir()
    unmeasured = _write_executable(
        other / "codex",
        '\nprint("codex-cli 0.146.0")\n',
    )
    with pytest.raises(CodexExecutableUnsupported):
        verify_codex_capability(unmeasured, conformant.search_path)


def test_the_version_probe_carries_the_interpreter_a_shim_needs(
    tmp_path: Path,
) -> None:
    # Measured on this host: the installed `codex` is a `#!/usr/bin/env node`
    # shim, so a probe whose child environment carries no PATH cannot run it at
    # all -- it exits 127 before reporting any version. The probe therefore
    # carries the same executable search path the served child gets.
    interpreter_directory = tmp_path / "interpreter"
    interpreter_directory.mkdir()
    _write_executable(
        interpreter_directory / "codex-runner",
        f'\nprint("codex-cli {MEASURED_CODEX_VERSION}")\n',
    )
    shim = tmp_path / "codex"
    shim.write_text('#!/bin/sh\nexec codex-runner "$@"\n')
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

    reachable = f"{interpreter_directory}:{os.environ.get('PATH', '/usr/bin')}"

    assert verify_codex_capability(shim, reachable) in CONFORMANT_CODEX_VERSIONS

    with pytest.raises(CodexExecutableUnsupported):
        verify_codex_capability(shim, str(tmp_path / "empty"))


@pytest.mark.parametrize(
    "broken",
    [
        {"FAKE_EXIT": "1"},
        {"FAKE_WITHHOLD_ANSWER": "1"},
        {"FAKE_ANSWER": "x" * (MAXIMUM_AGENT_OUTPUT_BYTES_V2 + 1)},
    ],
)
def test_a_broken_provider_answer_is_a_typed_refusal_not_an_invented_result(
    tmp_path: Path, broken: dict[str, str]
) -> None:
    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()
    invocation = executor.prepare_process(subscription_request())

    result = executor.decode_process_completion(launched(invocation, **broken))

    assert result == AgentExecutionFailure(
        AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
    )


def test_the_answer_is_the_exact_bytes_the_provider_wrote(tmp_path: Path) -> None:
    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()
    invocation = executor.prepare_process(subscription_request())

    result = executor.decode_process_completion(
        launched(invocation, FAKE_ANSWER="  pong  \n")
    )

    assert result == AgentExecutionResult(b"  pong  \n")


def test_job_and_answer_bytes_outlive_no_execution(tmp_path: Path) -> None:
    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()
    invocation = executor.prepare_process(subscription_request())
    answer_file = Path(
        invocation.arguments[invocation.arguments.index("--output-last-message") + 1]
    )
    launched(invocation)

    assert answer_file.parent.exists()
    executor.close()

    assert not answer_file.parent.exists()
    assert list(settings.workspace.iterdir()) == []


def test_a_contained_profile_attests(tmp_path: Path) -> None:
    settings = codex_subscription_deployment(tmp_path)
    report = _contained_doctor_report(settings.credential_directory, settings.workspace)

    attest_codex_containment(
        settings, environment_overrides={"FAKE_DOCTOR_REPORT": json.dumps(report)}
    )


@pytest.mark.parametrize(
    "surface",
    [
        "loaded_user_config",
        "mcp_servers",
        "foreign_home",
        "config_check_failed",
        "unreadable_report",
    ],
)
def test_a_profile_that_loads_foreign_trust_is_refused_before_serving(
    tmp_path: Path, surface: str
) -> None:
    settings = codex_subscription_deployment(tmp_path)
    report = _contained_doctor_report(settings.credential_directory, settings.workspace)
    configuration = report["checks"]["config.load"]  # type: ignore[index]
    details = configuration["details"]
    overrides = {}
    if surface == "loaded_user_config":
        details["config.toml"] = str(settings.credential_directory / "config.toml")
    elif surface == "mcp_servers":
        details["mcp servers"] = "2"
    elif surface == "foreign_home":
        details["CODEX_HOME"] = str(tmp_path / "somebody-elses-home")
    elif surface == "config_check_failed":
        configuration["status"] = "fail"
    overrides["FAKE_DOCTOR_REPORT"] = (
        "not json at all" if surface == "unreadable_report" else json.dumps(report)
    )

    with pytest.raises(CodexContainmentUnattested):
        attest_codex_containment(settings, environment_overrides=overrides)


def test_a_contained_profile_attests_even_when_unrelated_checks_fail(
    tmp_path: Path,
) -> None:
    # Measured on codex-cli 0.147.0: `doctor` exits 1 whenever any check fails,
    # and on a contained deployment the failing ones are auth and network
    # reachability. Neither is containment, so an offline host still serves.
    settings = codex_subscription_deployment(tmp_path)
    report = _contained_doctor_report(settings.credential_directory, settings.workspace)

    attest_codex_containment(
        settings,
        environment_overrides={
            "FAKE_DOCTOR_REPORT": json.dumps(report),
            "FAKE_DOCTOR_EXIT": "1",
        },
    )


def test_a_sandboxed_mode_whose_sandbox_cannot_run_here_is_refused(
    tmp_path: Path,
) -> None:
    settings = codex_subscription_deployment(
        tmp_path, sandbox=CodexSandboxMode.WORKSPACE_WRITE
    )
    report = _contained_doctor_report(settings.credential_directory, settings.workspace)

    # Measured on this host: `codex sandbox -- /bin/true` exits 1 with
    # `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted` where the
    # namespaces bubblewrap needs are unavailable. A sandbox that cannot start
    # is not a sandbox, so the executor refuses to serve rather than discover
    # it at attempt time.
    with pytest.raises(CodexContainmentUnattested):
        attest_codex_containment(
            settings,
            environment_overrides={
                "FAKE_DOCTOR_REPORT": json.dumps(report),
                "FAKE_SANDBOX_EXIT": "1",
            },
        )


def test_full_access_does_not_claim_a_sandbox_it_never_asked_for(
    tmp_path: Path,
) -> None:
    settings = codex_subscription_deployment(
        tmp_path, sandbox=CodexSandboxMode.DANGER_FULL_ACCESS
    )
    report = _contained_doctor_report(settings.credential_directory, settings.workspace)

    attest_codex_containment(
        settings,
        environment_overrides={
            "FAKE_DOCTOR_REPORT": json.dumps(report),
            "FAKE_SANDBOX_EXIT": "1",
        },
    )
