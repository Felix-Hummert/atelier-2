from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from atelier2.adapters import codex_subscription
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
    CodexSubscriptionProcessCommand,
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
    AgentProcessCommand,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    leased_directory_identity,
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


def _contained_doctor_report(settings_home: Path) -> dict[str, object]:
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
    credentials = tmp_path / "codex-home"
    credentials.mkdir()
    authentication = credentials / "auth.json"
    authentication.write_bytes(b"{}")
    authentication.chmod(0o600)
    return CodexSubscriptionSettings(
        executable, credentials, os.environ.get("PATH", "/usr/bin"), sandbox
    )


def leased_workspace(root: Path) -> Path:
    """The blank directory an attempt's lease starts this provider in."""

    workspace = root / "attempt-workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def leased(
    request: AgentExecutionRequestV2, command: AgentProcessCommand, workspace: Path
) -> AgentProcessInvocation:
    """One invocation for a test that houses the leased directory itself."""

    return AgentProcessInvocation(
        command,
        leased_directory_identity(
            agent_attempt_execution(request).attempt_id, workspace
        ),
    )


def subscription_request(
    model: str = "gpt-5.3-codex",
    auth_mode: AuthMode = AuthMode.SUBSCRIPTION,
    job: bytes = b"Reply with the single word pong",
    declared_output_schema: bytes | None = None,
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
        declared_output_schema,
    )


def launched(
    command: AgentProcessCommand, workspace: Path, **provider_behaviour: str
) -> AgentProcessCompletion:
    """Run one prepared command exactly where the attempt leased its ground."""

    completed = subprocess.run(
        command.arguments,
        cwd=workspace,
        env=dict(command.environment) | provider_behaviour,
        input=command.standard_input,
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

    workspace = leased_workspace(tmp_path)
    command = executor.prepare_process(request)
    invocation = leased(request, command, workspace)

    assert command.arguments == (
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
        # A bare name, not a path: the CLI writes it into the directory it is
        # started in, which is the attempt's lease and nowhere else.
        "--output-last-message",
        "last-message",
        "-",
    )
    job_home = dict(command.environment)["CODEX_HOME"]
    # HOME is containment, not convenience: a child without it resolves the
    # invoking account's own profile and loads that profile's trust. Both
    # name one private, disposable directory prepared for this call alone --
    # never the operator's own credential directory.
    assert command.environment == (
        ("HOME", job_home),
        ("CODEX_HOME", job_home),
        ("PATH", settings.search_path),
    )
    assert job_home != str(settings.credential_directory)
    # `codex exec` takes its prompt on stdin, so the job never reaches the
    # argument vector, where any account on the host could read it.
    assert command.standard_input == b"draw the owl"
    assert b"draw the owl" not in " ".join(command.arguments).encode()
    assert command.standard_output_frame_bytes == CODEX_SUBSCRIPTION_FRAME_BYTES

    result = executor.decode_process_completion(
        invocation, launched(command, workspace)
    )

    assert isinstance(result, AgentExecutionResult)
    observed = json.loads(result.output_bytes)
    assert observed["arguments"][0] == str(settings.executable)
    assert observed["stdin"] == "draw the owl"
    assert observed["working_directory"] == str(workspace)
    assert observed["environment"]["CODEX_HOME"] == job_home
    assert observed["environment"]["HOME"] == job_home
    assert "OPENAI_API_KEY" not in observed["environment"]

    executor.release_credential_channel(command)
    assert not Path(job_home).exists()


def test_the_private_credential_home_carries_the_source_auth_bytes_while_live(
    tmp_path: Path,
) -> None:
    """The copy this call needs exists while the call is live, nothing more.

    `config.toml` is deliberately absent from the copy: this executor's own
    containment attestation (`attest_codex_containment`) requires a served
    profile to resolve no user `config.toml`, so a copy of one would be
    exactly the pollution the attestation exists to catch.
    """

    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()

    command = executor.prepare_process(subscription_request())
    job_home = Path(dict(command.environment)["CODEX_HOME"])
    copied_auth = job_home / "auth.json"

    assert (
        copied_auth.read_bytes()
        == (settings.credential_directory / "auth.json").read_bytes()
    )
    assert stat.S_IMODE(copied_auth.stat().st_mode) == 0o400
    assert not (job_home / "config.toml").exists()

    executor.release_credential_channel(command)


def test_the_private_credential_home_outlives_neither_a_completion_nor_a_close(
    tmp_path: Path,
) -> None:
    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()
    workspace = leased_workspace(tmp_path)

    request = subscription_request()
    command = executor.prepare_process(request)
    job_home = Path(dict(command.environment)["CODEX_HOME"])
    assert job_home.exists()
    assert job_home != settings.credential_directory

    executor.decode_process_completion(
        leased(request, command, workspace), launched(command, workspace)
    )
    executor.release_credential_channel(command)

    assert not job_home.exists()

    second_request = subscription_request()
    second_command = executor.prepare_process(second_request)
    abandoned_home = Path(dict(second_command.environment)["CODEX_HOME"])
    assert abandoned_home.exists()

    executor.close()

    assert not abandoned_home.exists()


def test_the_private_credential_home_is_removed_after_a_failing_run(
    tmp_path: Path,
) -> None:
    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()
    workspace = leased_workspace(tmp_path)
    request = subscription_request()
    command = executor.prepare_process(request)
    job_home = Path(dict(command.environment)["CODEX_HOME"])

    executor.decode_process_completion(
        leased(request, command, workspace),
        launched(command, workspace, FAKE_EXIT="1"),
    )
    executor.release_credential_channel(command)

    assert not job_home.exists()


def test_a_declared_schema_reaches_codex_as_exact_private_file_bytes(
    tmp_path: Path,
) -> None:
    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()
    declared_schema = b'{"type": "object", "additionalProperties": false}'
    command = executor.prepare_process(
        subscription_request(declared_output_schema=declared_schema)
    )

    assert isinstance(command, CodexSubscriptionProcessCommand)
    assert command.output_schema_path is not None
    assert command.output_schema_path.read_bytes() == declared_schema
    assert stat.S_IMODE(command.output_schema_path.stat().st_mode) == 0o600
    assert command.arguments[command.arguments.index("--output-schema") + 1] == str(
        command.output_schema_path
    )
    job_home = Path(dict(command.environment)["CODEX_HOME"])
    assert job_home.exists()

    executor.release_credential_channel(command)

    assert not command.output_schema_path.exists()
    assert not job_home.exists()


def test_executor_close_removes_an_unreleased_output_schema_file(
    tmp_path: Path,
) -> None:
    executor = CodexSubscriptionExecutorFactory(
        codex_subscription_deployment(tmp_path)
    ).open()
    command = executor.prepare_process(
        subscription_request(declared_output_schema=b'{"type":"object"}')
    )

    assert isinstance(command, CodexSubscriptionProcessCommand)
    assert command.output_schema_path is not None
    assert command.output_schema_path.exists()
    job_home = Path(dict(command.environment)["CODEX_HOME"])
    assert job_home.exists()

    executor.close()

    assert not command.output_schema_path.exists()
    assert not job_home.exists()


def test_a_failing_schema_write_leaves_no_private_home_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_open_job_directory` runs before `_write_output_schema` can fail.

    A private home already holding a copy of `auth.json` is minted first;
    `_write_output_schema` (a full disk, a rejected tmp permission) can still
    raise afterwards. That home must not survive the refusal -- mirroring
    `claude_subscription`'s shape, where state-directory creation and every
    later failure both live inside the same `try`.
    """

    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()
    opened_directories: list[Path] = []
    real_open_job_directory = codex_subscription._open_job_directory

    def recording_open_job_directory(
        deployment_settings: CodexSubscriptionSettings,
    ) -> Path:
        directory = real_open_job_directory(deployment_settings)
        opened_directories.append(directory)
        return directory

    def failing_write_output_schema(document: bytes) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(
        codex_subscription, "_open_job_directory", recording_open_job_directory
    )
    monkeypatch.setattr(
        codex_subscription, "_write_output_schema", failing_write_output_schema
    )

    with pytest.raises(OSError, match="disk full"):
        executor.prepare_process(
            subscription_request(declared_output_schema=b'{"type":"object"}')
        )

    assert len(opened_directories) == 1
    assert not opened_directories[0].exists()


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
    workspace = leased_workspace(tmp_path)
    request = subscription_request()
    command = executor.prepare_process(request)

    result = executor.decode_process_completion(
        leased(request, command, workspace), launched(command, workspace, **broken)
    )

    assert result == AgentExecutionFailure(
        AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
    )

    executor.close()


def test_the_answer_is_the_exact_bytes_the_provider_wrote(tmp_path: Path) -> None:
    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()
    workspace = leased_workspace(tmp_path)
    request = subscription_request()
    command = executor.prepare_process(request)

    result = executor.decode_process_completion(
        leased(request, command, workspace),
        launched(command, workspace, FAKE_ANSWER="  pong  \n"),
    )

    assert result == AgentExecutionResult(b"  pong  \n")

    executor.close()


def test_the_answer_lands_in_the_directory_the_process_was_started_in(
    tmp_path: Path,
) -> None:
    """The whole reason the command may name the answer without a path.

    `--output-last-message last-message` is relative, so where it lands is
    decided by the directory the process is started in -- the attempt's lease.
    If the CLI resolved it against its credential home, or against whatever
    directory the server happens to run in, the answer would leave the lease and
    survive the attempt that owns it.
    """

    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()
    workspace = leased_workspace(tmp_path)
    request = subscription_request()
    command = executor.prepare_process(request)

    result = executor.decode_process_completion(
        leased(request, command, workspace),
        launched(command, workspace, FAKE_ANSWER="pong"),
    )

    assert result == AgentExecutionResult(b"pong")
    assert (workspace / "last-message").read_bytes() == b"pong"
    assert not (settings.credential_directory / "last-message").exists()
    assert not (tmp_path / "last-message").exists()

    executor.close()


def test_the_executor_writes_nothing_outside_the_leased_directory(
    tmp_path: Path,
) -> None:
    """Answer bytes outlive no attempt, because the lease is what removes them.

    The executor keeps no directory of its own: everything it produces lands
    inside the leased directory, and `AgentAttemptWorkspaceOwner.release` takes
    that directory and its contents away. One directory regime, and the owner of
    the ground is the owner of the cleanup.
    """

    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()
    workspace = leased_workspace(tmp_path)
    request = subscription_request()
    command = executor.prepare_process(request)
    before = sorted(entry.name for entry in tmp_path.iterdir())

    executor.decode_process_completion(
        leased(request, command, workspace), launched(command, workspace)
    )
    executor.close()

    assert sorted(entry.name for entry in tmp_path.iterdir()) == before
    assert [entry.name for entry in workspace.iterdir()] == ["last-message"]


def test_two_overlapping_attempts_each_decode_their_own_answer(
    tmp_path: Path,
) -> None:
    # The runtime opens one executor per registry key and hands that same object
    # to every attempt, so two attempts overlap between prepare and decode. Each
    # must read the answer its own invocation named.
    settings = codex_subscription_deployment(tmp_path)
    executor = CodexSubscriptionExecutorFactory(settings).open()
    first_request = subscription_request(job=b"attempt one")
    second_request = subscription_request(job=b"attempt two")
    first = leased(
        first_request,
        executor.prepare_process(first_request),
        leased_workspace(tmp_path / "one"),
    )
    second = leased(
        second_request,
        executor.prepare_process(second_request),
        leased_workspace(tmp_path / "two"),
    )
    (first.lease.working_directory / "last-message").write_bytes(
        b"ANSWER-FOR-ATTEMPT-ONE"
    )
    (second.lease.working_directory / "last-message").write_bytes(
        b"ANSWER-FOR-ATTEMPT-TWO"
    )

    answered = AgentProcessCompletion(0, b"", b"")

    assert executor.decode_process_completion(first, answered) == AgentExecutionResult(
        b"ANSWER-FOR-ATTEMPT-ONE"
    )
    assert executor.decode_process_completion(second, answered) == AgentExecutionResult(
        b"ANSWER-FOR-ATTEMPT-TWO"
    )

    executor.close()


def test_a_contained_profile_attests(tmp_path: Path) -> None:
    settings = codex_subscription_deployment(tmp_path)
    report = _contained_doctor_report(settings.credential_directory)

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
    report = _contained_doctor_report(settings.credential_directory)
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
    report = _contained_doctor_report(settings.credential_directory)

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
    report = _contained_doctor_report(settings.credential_directory)

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
    report = _contained_doctor_report(settings.credential_directory)

    attest_codex_containment(
        settings,
        environment_overrides={
            "FAKE_DOCTOR_REPORT": json.dumps(report),
            "FAKE_SANDBOX_EXIT": "1",
        },
    )
