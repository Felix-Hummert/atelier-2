from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Iterable, Sequence
from pathlib import Path

import pytest
import sqlalchemy as sa
from dbos import DBOSClient

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.schema import agent_receipts_v2, run_agent_bindings, runs
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.grok_subscription import (
    CONFORMANT_GROK_VERSIONS,
    GROK_SUBSCRIPTION_EXECUTOR_KEY,
    GROK_SUBSCRIPTION_FRAME_BYTES,
    GROK_SUBSCRIPTION_OPERATIONAL_IDENTITY,
    GROK_WORKSPACE_TOOLS_EXECUTOR_KEY,
    GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY,
    WORKSPACE_ALLOW_RULES,
    WORKSPACE_TOOLS,
    GrokContainmentUnattested,
    GrokExecutableUnsupported,
    GrokProviderEndedWithoutFinalMessage,
    GrokSubscriptionAuthModeUnsupported,
    GrokSubscriptionExecutorFactory,
    GrokSubscriptionProcessCommand,
    GrokSubscriptionSettings,
    GrokWorkspaceToolExecutorFactory,
    attest_grok_workspace_tool_invocation,
    verify_grok_capability,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode
from atelier2.contracts.agent_transcripts import AssistantTurn, AttemptTranscript
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ProviderId,
    ResolvedAgentBinding,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import AgentAttemptExecution, NodeExecutionId
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import RunId, WorkflowRevision, WorkflowRevisionHash
from atelier2.host import _grok_subscription_settings
from atelier2.host.serving import HostSettings, compose_application
from atelier2.ports.agent_attempts import AgentAttemptSucceeded
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
    DurablePublishedRunResult,
    DurableRunCreated,
    StartPublishedRunRequestV2,
)
from tests.scenarios.agents import (
    agent_attempt_execution,
    agent_scratch_root,
    leased_directory_identity,
    runtime_workspace_owner,
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


def grok_named_deployment(
    root: Path, name: str, source: str
) -> GrokSubscriptionSettings:
    directory = root / name
    directory.mkdir()
    return grok_subscription_deployment(directory, source)


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
    declared_output_schema: bytes | None = None,
    maximum_assistant_turns: int | None = None,
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
        declared_output_schema,
        maximum_assistant_turns=maximum_assistant_turns,
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


def measured_headless_json_envelope(
    *,
    text: str,
    thought: str,
    structured_output: object | None = None,
) -> bytes:
    """One grok 1.0.4 `--output-format json` completion, local scenario values.

    `structured_output` is scenario data for the `structuredOutput` field the
    CLI adds when `--json-schema` is set. It is not a second contract: the
    decoder still reads `text`.
    """

    envelope: dict[str, object] = {
        "text": text,
        "thought": thought,
        "stopReason": "end_turn",
        "sessionId": "00000000-0000-4000-8000-000000000001",
        "requestId": "00000000-0000-4000-8000-000000000002",
        "usage": {
            "input_tokens": 1,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 1,
            "reasoning_tokens": 0,
            "total_tokens": 2,
        },
        "num_turns": 1,
        "total_cost_usd": 0.0,
        "total_cost_usd_ticks": 0,
        "modelUsage": {},
    }
    if structured_output is not None:
        envelope["structuredOutput"] = structured_output
    return json.dumps(envelope).encode()


def recorded_grok_json_values(*values: object) -> bytes:
    """The JSON values grok 1.0.4 concatenated without a record separator."""

    return b"".join(json.dumps(value, ensure_ascii=False).encode() for value in values)


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
    # Distinctive spacing so a json.loads/dumps rewrite of the published
    # document fails this pin rather than silently agreeing. An object
    # schema: a bare string no longer travels as `--json-schema`.
    declared_schema = b'{"type": "object", "additionalProperties": false}'
    request = subscription_request(
        model="grok-4", job=b"draw the owl", declared_output_schema=declared_schema
    )

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
        "--json-schema",
        '{"type": "object", "additionalProperties": false}',
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
        "16",
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
    assert (
        observed["arguments"][observed["arguments"].index("--json-schema") + 1]
        == '{"type": "object", "additionalProperties": false}'
    )
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


def test_a_bare_string_schema_does_not_travel_as_json_schema(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    request = subscription_request(
        declared_output_schema=b'{"type": "string", "minLength": 1}'
    )

    command = executor.prepare_process(request)

    assert "--json-schema" not in command.arguments
    executor.release_credential_channel(command)


def test_a_bare_string_schema_answer_is_serialized_as_a_json_string(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    request = subscription_request(declared_output_schema=b'{"type":"string"}')
    command = executor.prepare_process(request)
    invocation = leased(command, leased_workspace(tmp_path))
    prose = "Befund 1: der Diff nennt die apt-Lock-Heilung."

    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text=prose,
                thought="Ich prüfe zuerst die Regeln.",
            ),
            b"",
        ),
    )

    assert isinstance(result, AgentExecutionResult)
    assert result == AgentExecutionResult(
        json.dumps(prose, ensure_ascii=False).encode()
    )
    assert json.loads(result.output_bytes) == prose
    executor.release_credential_channel(command)


def test_a_bare_string_schema_marks_the_command_not_an_executor_set(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    command = executor.prepare_process(
        subscription_request(declared_output_schema=b'{"type":"string"}')
    )

    assert isinstance(command, GrokSubscriptionProcessCommand)
    assert command.serialize_free_text_as_json_string is True
    executor.release_credential_channel(command)


def test_canonicalization_reads_the_command_mark_not_home(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    prose = "Befund 1: der Diff nennt die apt-Lock-Heilung."
    command = GrokSubscriptionProcessCommand(
        ("grok",),
        (),
        standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        serialize_free_text_as_json_string=True,
    )

    result = executor.decode_process_completion(
        leased(command, leased_workspace(tmp_path)),
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text=prose,
                thought="Ich prüfe zuerst die Regeln.",
            ),
            b"",
        ),
    )

    assert isinstance(result, AgentExecutionResult)
    assert result == AgentExecutionResult(
        json.dumps(prose, ensure_ascii=False).encode()
    )


def test_an_unmarked_command_does_not_quote_free_text(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    prose = "Befund 1: der Diff nennt die apt-Lock-Heilung."
    command = GrokSubscriptionProcessCommand(
        ("grok",),
        (),
        standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        serialize_free_text_as_json_string=False,
    )

    result = executor.decode_process_completion(
        leased(command, leased_workspace(tmp_path)),
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text=prose,
                thought="Ich prüfe zuerst die Regeln.",
            ),
            b"",
        ),
    )

    assert isinstance(result, AgentExecutionResult)
    assert result == AgentExecutionResult(prose.encode())


def test_an_object_schema_does_not_mark_the_command_for_quoting(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    command = executor.prepare_process(
        subscription_request(
            declared_output_schema=b'{"type": "object", "additionalProperties": false}'
        )
    )

    assert isinstance(command, GrokSubscriptionProcessCommand)
    assert command.serialize_free_text_as_json_string is False
    assert "--json-schema" in command.arguments
    executor.release_credential_channel(command)


def test_a_node_without_a_declared_schema_does_not_invent_a_json_schema_flag(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()

    command = executor.prepare_process(subscription_request())

    assert "--json-schema" not in command.arguments
    executor.release_credential_channel(command)


def test_a_non_subscription_profile_is_refused_before_any_invocation(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    request = subscription_request(auth_mode=AuthMode.API_KEY)

    with pytest.raises(GrokSubscriptionAuthModeUnsupported):
        executor.prepare_process(request)


def test_concatenated_grok_progress_values_stay_in_the_transcript(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        AgentProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        ),
        tmp_path,
    )
    first_finding = f"{'Befund'} {1}"
    progress = (
        (
            f'"{first_finding}" is the required first token; I am gathering '
            "the surrounding contract, callers, and tests."
        ),
        (
            f'"{first_finding}" stays first in the final reply; I am comparing '
            "the empty-file gate with its host-init twin."
        ),
    )
    answer = '{"verdict":"pass"}'
    final_envelope = json.loads(
        measured_headless_json_envelope(
            text=answer,
            thought="I have enough evidence to answer now.",
        )
    )

    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(
            0,
            recorded_grok_json_values(*progress, final_envelope),
            b"",
        ),
    )

    assert result == AgentExecutionResult(
        answer.encode(),
        AttemptTranscript.of([AssistantTurn(message) for message in progress]),
    )


def test_grok_ending_after_concatenated_progress_has_no_final_message(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        AgentProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        ),
        tmp_path,
    )
    first_finding = f"{'Befund'} {1}"
    progress = (
        (
            f'"{first_finding}" is the required first token; I am gathering '
            "the surrounding contract, callers, and tests."
        ),
        (
            f'"{first_finding}" stays first in the final reply; I am comparing '
            "the empty-file gate with its host-init twin."
        ),
    )

    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(0, recorded_grok_json_values(*progress), b""),
    )

    assert result == GrokProviderEndedWithoutFinalMessage(
        AttemptTranscript.of([AssistantTurn(message) for message in progress])
    )


def test_the_final_answer_reaches_the_output_seam_not_the_turn_narration(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        AgentProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        ),
        tmp_path,
    )
    narration = "Ich prüfe zuerst die Dateien und die Werkzeuge."
    answer = '{"verdict":"pass"}'

    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(text=answer, thought=narration),
            b"",
        ),
    )

    assert result == AgentExecutionResult(answer.encode())


def test_a_schema_bearing_envelope_yields_text_not_the_parsed_twin(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokSubscriptionExecutorFactory(settings).open()
    invocation = leased(
        AgentProcessCommand(
            ("grok",),
            standard_output_frame_bytes=GROK_SUBSCRIPTION_FRAME_BYTES,
        ),
        tmp_path,
    )
    narration = "Ich prüfe zuerst die Dateien und die Werkzeuge."
    answer = '"pass-token"'

    result = executor.decode_process_completion(
        invocation,
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text=answer,
                thought=narration,
                structured_output="pass-token",
            ),
            b"",
        ),
    )

    assert result == AgentExecutionResult(answer.encode())
    assert result != AgentExecutionResult(b"pass-token")
    assert result != AgentExecutionResult(narration.encode())


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
    refusal = AgentExecutionFailure(
        AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
    )
    narration = "Ich prüfe zuerst die Dateien und die Werkzeuge."

    failed_with_output = executor.decode_process_completion(
        invocation, AgentProcessCompletion(1, b'{"text":"no"}', b"")
    )
    assert isinstance(failed_with_output, AgentExecutionFailure)
    assert failed_with_output.code == refusal.code
    assert failed_with_output.transcript is not None
    assert (
        executor.decode_process_completion(
            invocation, AgentProcessCompletion(0, b"not-json", b"")
        )
        == refusal
    )
    assert (
        executor.decode_process_completion(
            invocation, AgentProcessCompletion(0, b'{"result":"wrong field"}', b"")
        )
        == refusal
    )
    assert (
        executor.decode_process_completion(
            invocation, AgentProcessCompletion(0, b"", b"")
        )
        == GrokProviderEndedWithoutFinalMessage()
    )
    for completion in (
        AgentProcessCompletion(0, b"{}", b""),
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(text="", thought=narration),
            b"",
        ),
        AgentProcessCompletion(
            0,
            measured_headless_json_envelope(
                text="",
                thought=narration,
                structured_output="pass-token",
            ),
            b"",
        ),
    ):
        result = executor.decode_process_completion(invocation, completion)
        assert isinstance(result, GrokProviderEndedWithoutFinalMessage)


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
    declared = _grok_subscription_settings(
        parser,
        argparse.Namespace(
            grok_executable=candidate.executable,
            grok_workspace=candidate.workspace,
            grok_credential_directory=candidate.credential_directory,
            grok_workspace_tools=False,
        ),
    )
    assert declared.settings == candidate
    assert declared.start_refusal is None

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
        "grok_subscription": declared.settings,
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
            runtime.engine,
            runtime.settings,
            runtime.agent_executor_registry,
            effect_adapter_proves_absence=True,
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


WORKSPACE_ARTEFACT_NAME = "artefact"
WORKSPACE_ARTEFACT_LINE = "from the lease"

TOOL_USING_GROK = f"""
import json, os, sys, tomllib
from pathlib import Path
if "--version" in sys.argv:
    print("grok 1.0.4 (d846eb93d9) [stable]")
    raise SystemExit(0)
if "inspect" in sys.argv:
    home = Path(os.environ["GROK_HOME"])
    compatibility = tomllib.loads((home / "config.toml").read_text())["compat"]
    cells = [
        {{
            "vendor": vendor,
            "surface": surface,
            "enabled": compatibility[vendor].get(surface) is not False,
            "source": "config",
        }}
        for vendor, surfaces in (
            ("cursor", ("skills", "rules", "agents", "mcps", "hooks", "sessions")),
            ("claude", ("skills", "rules", "agents", "mcps", "hooks", "sessions")),
            ("codex", ("sessions",)),
        )
        for surface in surfaces
    ]
    json.dump(
        {{
            "plugins": [], "hooks": [], "mcpServers": [], "skills": [],
            "marketplaces": [], "lspServers": [], "projectInstructions": [],
            "configSources": {{"layers": [{{"role": "user", "path": str(home / "config.toml")}}]}},
            "permissions": {{"sources": []}},
            "agents": [{{"name": "general-purpose", "source": {{"type": "builtin"}}}}],
            "externalCompat": {{"remoteSettingsLoaded": False, "cells": cells}},
        }},
        sys.stdout,
    )
    raise SystemExit(0)
written = os.path.join(os.getcwd(), {WORKSPACE_ARTEFACT_NAME!r})
with open(written, "w", encoding="utf-8") as artefact:
    artefact.write({WORKSPACE_ARTEFACT_LINE!r})
with open(written, encoding="utf-8") as artefact:
    read_back = artefact.read()
json.dump({{"text": json.dumps({{"wrote": written, "read_back": read_back}})}}, sys.stdout)
"""


def grok_subscription_runtime(
    root: Path,
    settings: GrokSubscriptionSettings,
    *,
    workspace_tools: bool = False,
) -> DbosRuntime:
    """The production runtime, serving the Grok executors this scenario arms."""

    return DbosRuntime(
        DbosRuntimeSettings(
            root / "atelier.sqlite",
            "grok-subscription-test",
            agent_scratch_root=agent_scratch_root(root),
        ),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("grok-subscription-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (
            GrokSubscriptionExecutorFactory(settings),
            *((GrokWorkspaceToolExecutorFactory(settings),) if workspace_tools else ()),
        ),
    )


def grok_subscription_start(
    runtime: DbosRuntime,
    run_name: str,
    requested_capability: AgentExecutionCapability,
    executor_revision: AgentExecutorRevision,
) -> tuple[DurablePublishedRunResult, WorkflowRevision]:
    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision(
        "grok-tools", 1, ProviderId("xai"), AuthMode.SUBSCRIPTION
    )
    catalog.publish_auth_profile_revision(auth)
    configuration = AgentConfigurationRevision(
        "grok-4",
        auth.revision_hash,
        executor_revision,
        requested_capability,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    catalog.publish_agent_configuration_revision(configuration)
    workflow = WorkflowRevision(HOST_DOCUMENT)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    started = DbosDurableRunStarter(
        runtime.engine,
        runtime.settings,
        runtime.agent_executor_registry,
        effect_adapter_proves_absence=True,
    ).start_published(
        StartPublishedRunRequestV2(
            RunId(run_name),
            workflow.revision_hash,
            AgentBindingSet(
                (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
            ),
        )
    )
    return started, workflow


def grok_subscription_attempt(
    runtime: DbosRuntime,
    run_name: str,
    requested_capability: AgentExecutionCapability,
    executor_revision: AgentExecutorRevision,
    operational_identity: AgentExecutorOperationalIdentity,
) -> AgentAttemptExecution:
    run_id = RunId(run_name)
    started, workflow = grok_subscription_start(
        runtime, run_name, requested_capability, executor_revision
    )
    assert isinstance(started, DurableRunCreated)
    assert isinstance(started.run, RunV2)
    return agent_attempt_execution(
        AgentExecutionRequestV2(
            NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
            run_id,
            workflow.revision_hash,
            "build",
            started.run.agent_bindings[0],
            operational_identity,
            b"build",
        )
    )


def argument_after(arguments: Sequence[str], flag: str) -> str:
    return arguments[arguments.index(flag) + 1]


def workspace_tool_flags(settings: GrokSubscriptionSettings) -> tuple[str, ...]:
    """Every flag the real workspace-tool invocation carries, read off that invocation."""

    executor = GrokWorkspaceToolExecutorFactory(settings).open()
    try:
        command = executor.prepare_process(subscription_request())
        seen: list[str] = []
        for argument in command.arguments:
            if argument.startswith("--") and argument not in seen:
                seen.append(argument)
        return tuple(seen)
    finally:
        executor.close()


def parsing_grok(known: Iterable[str], *, refuses_unknown: bool = True) -> str:
    """A fake CLI that reads exactly these flags, then refuses a call with no credentials.

    That is the measured order of the real one: an argument it cannot read ends
    the call before anything else, and a call whose arguments it read whole is
    still refused when no credentials arrive.
    """

    return (
        "import sys\n"
        f"known = {sorted(set(known))!r}\n"
        f"refuses_unknown = {refuses_unknown!r}\n"
        "if '--version' in sys.argv:\n"
        "    print('grok 1.0.4 (d846eb93d9) [stable]')\n"
        "    raise SystemExit(0)\n"
        "for argument in sys.argv[1:]:\n"
        "    if refuses_unknown and argument.startswith('--') "
        "and argument not in known:\n"
        '        sys.stderr.write("error: unexpected argument \'" + argument + "\' found\\n")\n'
        "        raise SystemExit(2)\n"
        "sys.stderr.write('Error: Not signed in.\\n')\n"
        "raise SystemExit(1)\n"
    )


def test_the_workspace_tool_factory_offers_its_own_identity_and_only_its_capability(
    tmp_path: Path,
) -> None:
    """A second operation of one provider, not a later revision of the first."""

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    factory = GrokWorkspaceToolExecutorFactory(settings)
    tool_free = GrokSubscriptionExecutorFactory(settings)

    assert factory.key == GROK_WORKSPACE_TOOLS_EXECUTOR_KEY
    assert factory.key.provider_id == tool_free.key.provider_id
    assert factory.key.executor_revision != tool_free.key.executor_revision
    assert factory.operational_identity != tool_free.operational_identity
    assert factory.declared_capabilities == frozenset(
        {AgentExecutionCapability.HEADLESS_WITH_TOOLS}
    )
    assert factory.open().close() is None


def test_the_tool_invocation_names_its_tools_and_keeps_every_other_containment_flag(
    tmp_path: Path,
) -> None:
    """One decision separates the two invocations, and it is the tool grant.

    The tool-free call's `--tools=` is gone. In its place: `--tools` names the
    built-in IDs and `--allow` names the five permission classes, because Grok
    splits the two switches Claude combines. Every other flag that call carries
    is still carried, and the environment is the same shape: what changed is
    what the process may touch, not who it is.
    """

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    tool_free = GrokSubscriptionExecutorFactory(settings).open()
    executor = GrokWorkspaceToolExecutorFactory(settings).open()
    request = subscription_request(model="grok-4", job=b"draw the owl")

    command = executor.prepare_process(request)
    tool_free_command = tool_free.prepare_process(request)

    assert "--tools=" in tool_free_command.arguments
    assert "--tools=" not in command.arguments
    assert argument_after(command.arguments, "--tools") == ",".join(WORKSPACE_TOOLS)
    allows = [
        command.arguments[index + 1]
        for index, argument in enumerate(command.arguments)
        if argument == "--allow"
    ]
    assert allows == list(WORKSPACE_ALLOW_RULES)
    assert argument_after(command.arguments, "--deny") == "MCPTool"
    assert "--always-approve" not in command.arguments
    assert "bypassPermissions" not in command.arguments
    kept = tuple(
        flag
        for flag in tool_free_command.arguments
        if flag.startswith("--") and flag != "--tools="
    )
    assert all(flag in command.arguments for flag in kept)
    assert argument_after(command.arguments, "--max-turns") == argument_after(
        tool_free_command.arguments, "--max-turns"
    )
    tool_home = dict(command.environment)
    free_home = dict(tool_free_command.environment)
    assert tuple(name for name, _value in command.environment) == tuple(
        name for name, _value in tool_free_command.environment
    )
    assert tool_home["PATH"] == free_home["PATH"] == settings.search_path
    assert tool_home["HOME"] == tool_home["GROK_HOME"]
    assert free_home["HOME"] == free_home["GROK_HOME"]
    assert Path(tool_home["HOME"]).parent == settings.workspace
    assert Path(free_home["HOME"]).parent == settings.workspace
    assert tool_home["HOME"] != free_home["HOME"]
    assert all(argument for argument in command.arguments)

    workspace = leased_workspace(tmp_path)
    result = executor.decode_process_completion(
        leased(command, workspace), launched(command, workspace)
    )

    assert isinstance(result, AgentExecutionResult)
    observed = json.loads(result.output_bytes)
    assert observed["arguments"][1:] == list(command.arguments[1:])
    assert observed["working_directory"] == str(workspace)
    assert observed["job"] == "draw the owl"
    executor.release_credential_channel(command)
    tool_free.release_credential_channel(tool_free_command)
    tool_free.close()
    executor.close()


@pytest.mark.proves("a-pinned-budget-turn-bound-is-the-tool-attempt-ceiling")
def test_a_workspace_tool_call_takes_the_pinned_turn_bound(
    tmp_path: Path,
) -> None:
    """The budget names the ceiling; without one the existing default stays.

    The tool-free call is the other reader: it keeps sixteen even when the
    same request carries a bound. Removing the request field from the tool
    vector makes the pinned case agree with the default.
    """

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokWorkspaceToolExecutorFactory(settings).open()
    tool_free = GrokSubscriptionExecutorFactory(settings).open()

    default_command = executor.prepare_process(subscription_request())
    pinned_command = executor.prepare_process(
        subscription_request(maximum_assistant_turns=8)
    )
    headless = tool_free.prepare_process(
        subscription_request(maximum_assistant_turns=8)
    )

    assert argument_after(default_command.arguments, "--max-turns") == "16"
    assert argument_after(pinned_command.arguments, "--max-turns") == "8"
    assert argument_after(headless.arguments, "--max-turns") == "16"
    executor.release_credential_channel(default_command)
    executor.release_credential_channel(pinned_command)
    tool_free.release_credential_channel(headless)
    tool_free.close()
    executor.close()


def test_a_non_subscription_profile_reaches_no_tool_bearing_process(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    executor = GrokWorkspaceToolExecutorFactory(settings).open()

    with pytest.raises(GrokSubscriptionAuthModeUnsupported, match="workspace-tool"):
        executor.prepare_process(subscription_request(auth_mode=AuthMode.API_KEY))


@pytest.mark.parametrize(
    ("executor_revision", "requested_capability"),
    [
        pytest.param(
            GROK_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.HEADLESS_WITH_TOOLS,
            id="the tool-free executor is asked for tools",
        ),
        pytest.param(
            GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision,
            AgentExecutionCapability.HEADLESS,
            id="the tool executor is asked for a tool-free call",
        ),
    ],
)
def test_a_binding_asking_a_grok_executor_for_a_capability_it_never_declared_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executor_revision: AgentExecutorRevision,
    requested_capability: AgentExecutionCapability,
) -> None:
    """Neither executor answers the other's ask, and the refusal is the starter's."""

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    runtime = grok_subscription_runtime(tmp_path, settings, workspace_tools=True)
    runtime.initialize_storage()

    def unexpected_enqueue(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("an undeclared capability reached the durable queue")

    monkeypatch.setattr(DBOSClient, "enqueue_in_transaction", unexpected_enqueue)
    try:
        refused, _workflow = grok_subscription_start(
            runtime,
            "grok/undeclared-capability",
            requested_capability=requested_capability,
            executor_revision=executor_revision,
        )
        with runtime.engine.connect() as connection:
            written = tuple(
                connection.scalar(sa.select(sa.func.count()).select_from(table))
                for table in (runs, run_agent_bindings)
            )
    finally:
        runtime.close()

    assert isinstance(refused, DurableAgentExecutorCapabilityUnavailable)
    assert written == (0, 0)


def test_a_node_requesting_grok_tools_starts_through_the_production_starter(
    tmp_path: Path,
) -> None:
    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    runtime = grok_subscription_runtime(tmp_path, settings, workspace_tools=True)
    runtime.initialize_storage()
    try:
        started, _workflow = grok_subscription_start(
            runtime,
            "grok/tool-capability",
            requested_capability=AgentExecutionCapability.HEADLESS_WITH_TOOLS,
            executor_revision=GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision,
        )
        with runtime.engine.connect() as connection:
            started_runs = connection.scalar(
                sa.select(sa.func.count()).select_from(runs)
            )
    finally:
        runtime.close()

    assert isinstance(started, DurableRunCreated)
    assert started_runs == 1


def test_a_tool_bearing_grok_attempt_writes_in_its_lease_and_answers_what_it_wrote(
    tmp_path: Path,
) -> None:
    """The vertical the capability exists for, through the production path."""

    settings = grok_subscription_deployment(tmp_path, TOOL_USING_GROK)
    runtime = grok_subscription_runtime(tmp_path, settings, workspace_tools=True)
    runtime.initialize_storage()
    try:
        execution = grok_subscription_attempt(
            runtime,
            "grok/workspace-tools",
            requested_capability=AgentExecutionCapability.HEADLESS_WITH_TOOLS,
            executor_revision=GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision,
            operational_identity=GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY,
        )
        workspaces = runtime_workspace_owner(runtime)
        leased_directory = workspaces.scratch_root / execution.attempt_id.value
        outcome = execute_agent_attempt(
            execution,
            GrokWorkspaceToolExecutorFactory(settings).open(),
            DbosAgentAttemptStore(runtime.engine),
            runtime.agent_process_supervisor,
            workspaces,
        )
        with runtime.engine.connect() as connection:
            receipts = connection.execute(sa.select(agent_receipts_v2)).mappings().all()
    finally:
        runtime.close()

    assert isinstance(outcome, AgentAttemptSucceeded)
    assert len(receipts) == 1
    assert receipts[0]["executor_revision"] == (
        GROK_WORKSPACE_TOOLS_EXECUTOR_KEY.executor_revision.value
    )
    assert receipts[0]["executor_operational_identity"] == (
        GROK_WORKSPACE_TOOLS_OPERATIONAL_IDENTITY.value
    )
    answered = json.loads(receipts[0]["output_bytes"])
    assert answered["wrote"] == str(leased_directory / WORKSPACE_ARTEFACT_NAME)
    assert answered["read_back"] == WORKSPACE_ARTEFACT_LINE
    assert not leased_directory.exists()


def test_an_executable_that_starts_this_exact_grok_invocation_is_attested(
    tmp_path: Path,
) -> None:
    reference = grok_named_deployment(tmp_path, "reference", INTROSPECTING_GROK)
    settings = grok_named_deployment(
        tmp_path, "deployment", parsing_grok(workspace_tool_flags(reference))
    )

    assert attest_grok_workspace_tool_invocation(settings) is None


def test_an_executable_missing_any_flag_of_this_grok_invocation_is_refused_by_that_flag(
    tmp_path: Path,
) -> None:
    """Every flag of the vector is a containment decision, so every one is probed."""

    reference = grok_named_deployment(tmp_path, "reference", INTROSPECTING_GROK)
    flags = workspace_tool_flags(reference)

    assert flags
    for missing in flags:
        settings = grok_named_deployment(
            tmp_path,
            f"without{flags.index(missing)}",
            parsing_grok(flag for flag in flags if flag != missing),
        )

        with pytest.raises(GrokExecutableUnsupported, match=re.escape(missing)):
            attest_grok_workspace_tool_invocation(settings)


def test_an_executable_that_never_names_an_unexpected_argument_cannot_be_attested(
    tmp_path: Path,
) -> None:
    """Without the control, "said nothing" and "has nothing to say" look alike."""

    reference = grok_named_deployment(tmp_path, "reference", INTROSPECTING_GROK)
    settings = grok_named_deployment(
        tmp_path,
        "deployment",
        parsing_grok(workspace_tool_flags(reference), refuses_unknown=False),
    )

    with pytest.raises(GrokExecutableUnsupported, match="no release can know"):
        attest_grok_workspace_tool_invocation(settings)


def test_an_executable_that_answers_a_jobless_grok_invocation_successfully_is_refused(
    tmp_path: Path,
) -> None:
    """The probe rests on that call being refused, so a success is unmeasured ground."""

    settings = grok_subscription_deployment(tmp_path, "raise SystemExit(0)\n")

    with pytest.raises(GrokExecutableUnsupported, match="jobless"):
        attest_grok_workspace_tool_invocation(settings)


def test_an_executable_that_answers_its_version_and_cannot_spawn_is_refused(
    tmp_path: Path,
) -> None:
    """The gap this attestation exists for: a version answer is not startability."""

    settings = grok_subscription_deployment(tmp_path, INTROSPECTING_GROK)
    assert verify_grok_capability(settings.executable) in CONFORMANT_GROK_VERSIONS
    settings.executable.write_text(
        "#!/atelier2/no/such/interpreter\n", encoding="utf-8"
    )
    settings.executable.chmod(0o755)

    with pytest.raises(GrokExecutableUnsupported, match="could not start"):
        attest_grok_workspace_tool_invocation(settings)
