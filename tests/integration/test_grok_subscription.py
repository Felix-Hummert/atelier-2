from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from atelier2.adapters.grok_subscription import (
    CONFORMANT_GROK_VERSIONS,
    GROK_SUBSCRIPTION_EXECUTOR_KEY,
    GROK_SUBSCRIPTION_FRAME_BYTES,
    GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY,
    GrokExecutableUnsupported,
    GrokSubscriptionAuthModeUnsupported,
    GrokSubscriptionExecutorFactory,
    GrokSubscriptionSettings,
    verify_grok_capability,
)
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agents import (
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

MEASURED_GROK_VERSION = "1.0.4"
INTROSPECTING_GROK = """
import json, os, sys
from pathlib import Path
if "--version" in sys.argv:
    print("grok 1.0.4 (d846eb93d9) [stable]")
    raise SystemExit(0)
prompt_file = None
args = sys.argv[1:]
for index, argument in enumerate(args):
    if argument == "--prompt-file" and index + 1 < len(args):
        prompt_file = args[index + 1]
job = Path(prompt_file).read_bytes() if prompt_file else b""
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
    path.write_text("#!/usr/bin/env python3\n" + source)
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


def launched(invocation: AgentProcessInvocation) -> AgentProcessCompletion:
    completed = subprocess.run(
        invocation.arguments,
        cwd=invocation.working_directory,
        env=dict(invocation.environment),
        input=invocation.standard_input,
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

    invocation = executor.prepare_process(request)
    job_file = (
        settings.workspace / f"atelier2-grok-job-{request.node_execution_id.value}"
    )

    assert invocation.arguments == (
        str(settings.executable),
        "-p",
        "--output-format",
        "json",
        "--model",
        "grok-4",
        "--prompt-file",
        str(job_file),
        "--tools=",
        "--permission-mode",
        "dontAsk",
        "--max-turns",
        "1",
    )
    assert invocation.working_directory == settings.workspace
    assert invocation.environment == (
        ("GROK_HOME", str(settings.credential_directory)),
        ("PATH", settings.search_path),
    )
    assert invocation.standard_input == b""
    assert invocation.standard_output_frame_bytes == GROK_SUBSCRIPTION_FRAME_BYTES
    assert b"draw the owl" not in " ".join(invocation.arguments).encode()
    result = executor.decode_process_completion(launched(invocation))
    assert isinstance(result, AgentExecutionResult)
    observed = json.loads(result.output_bytes)
    assert observed["arguments"][0] == str(settings.executable)
    assert observed["job"] == "draw the owl"
    assert observed["stdin"] == ""
    assert observed["environment"]["GROK_HOME"] == str(settings.credential_directory)
    assert observed["environment"]["PATH"] == settings.search_path
    assert "HOME" not in observed["environment"]
    assert "XAI_API_KEY" not in observed["environment"]


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

    assert executor.decode_process_completion(
        AgentProcessCompletion(1, b'{"text":"no"}', b"")
    ) == AgentExecutionFailure(AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY)
    assert executor.decode_process_completion(
        AgentProcessCompletion(0, b"not-json", b"")
    ) == AgentExecutionFailure(AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY)
    assert executor.decode_process_completion(
        AgentProcessCompletion(0, b'{"result":"wrong field"}', b"")
    ) == AgentExecutionFailure(AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY)


def test_only_the_measured_grok_release_is_admitted(tmp_path: Path) -> None:
    assert CONFORMANT_GROK_VERSIONS == {(1, 0, 4)}
    other = grok_subscription_deployment(
        tmp_path,
        "import sys\nprint('grok 1.0.3 (old) [stable]')\n",
    )
    with pytest.raises(GrokExecutableUnsupported, match="1.0.4"):
        verify_grok_capability(other.executable)
