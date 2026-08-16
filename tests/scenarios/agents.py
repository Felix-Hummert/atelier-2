from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Never

from atelier2.adapters.claude_subscription import (
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    CLAUDE_SUBSCRIPTION_OPERATIONAL_IDENTITY,
    CONFORMANT_CLAUDE_VERSIONS,
    CREDENTIAL_RECORD_ENTRY,
    ClaudeSubscriptionExecutorFactory,
    ClaudeSubscriptionSettings,
)
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.run_store import commit_agent_completed, load_graph
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import (
    EXACT_OUTPUT_EXECUTOR_BINDING,
    ExactOutputAgentExecutorFactory,
)
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.contracts.agent_attempts import AgentAttemptFailureCode, AgentAttemptId
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
    AgentConfigurationRevisionFormatVersion,
    AgentExecutionCapability,
    AgentExecutionRequest,
    AgentExecutionRequestV2,
    AgentExecutionResult,
    AgentExecutorOperationalIdentity,
    AgentExecutorRevision,
    AgentRole,
    AuthMode,
    AuthProfileRevision,
    ExactOutputContract,
    ProviderId,
)
from atelier2.contracts.effects import AdapterRevision, EffectDestination
from atelier2.contracts.executions import (
    AgentAttemptExecution,
    NodeExecutionId,
    TransitionSnapshot,
)
from atelier2.contracts.run_bindings import RunV2
from atelier2.contracts.runs import RunId, WorkflowRevision, WorkflowRevisionHash
from atelier2.contracts.workflows import AgentNode
from atelier2.ports.agent_executions import (
    AgentExecutionFailure,
    AgentExecutorKey,
    AgentProcessCompletion,
    AgentProcessInvocation,
)
from atelier2.ports.durable_runs import (
    DurablePublishedRunResult,
    DurableRunCreated,
    StartPublishedRunRequestV2,
)

SCENARIO_PROVIDER_FRAME_BYTES = 49_152
"""The raw stdout frame a scenario provider declares when it is not the subject."""


def configured_agent_request(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
) -> AgentExecutionRequest:
    node = load_graph(session, revision_hash).node(node_id)
    assert isinstance(node, AgentNode)
    return AgentExecutionRequest(
        NodeExecutionId.for_node(run_id, revision_hash, node_id),
        run_id,
        revision_hash,
        node_id,
        node.job.encode("utf-8"),
        ExactOutputContract(node.output.encode("utf-8")),
    )


def commit_configured_agent(
    session: Any,
    run_id: RunId,
    revision_hash: WorkflowRevisionHash,
    node_id: str,
) -> TransitionSnapshot:
    request = configured_agent_request(session, run_id, revision_hash, node_id)
    return commit_agent_completed(
        session,
        request,
        EXACT_OUTPUT_EXECUTOR_BINDING,
        AgentExecutionResult(request.exact_output.output_bytes),
    )


MEASURED_CLAUDE_VERSION = ".".join(
    str(part) for part in max(CONFORMANT_CLAUDE_VERSIONS)
)


def _version_answering(program: str, version: str | None) -> str:
    """Wrap a fake CLI so it answers `--version` the way the real one does.

    The deployment reads the executable's version before it composes anything,
    so a fake that cannot answer is not a fake of this CLI at all.
    """

    if version is None:
        return program
    return (
        "import sys\n"
        'if "--version" in sys.argv:\n'
        f'    print("{version} (Claude Code)")\n'
        "    raise SystemExit(0)\n"
    ) + program


def claude_search_path(directory: Path) -> str:
    """A search path carrying the bubblewrap the scrubbing CLI insists on.

    The CLI resolves it through the search path this deployment hands the
    launched process, so a deployment fake needs one there or it is not a
    deployment this executor accepts.
    """

    tools = directory / "tools"
    tools.mkdir(exist_ok=True)
    bubblewrap = tools / "bwrap"
    bubblewrap.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    bubblewrap.chmod(0o755)
    return str(tools)


PERSONAL_SUBSCRIPTION_TYPE = "max"
"""What a deployment fake's credential record says the account is.

This executor is contracted for a personal subscription, so a fake credential
directory carries the record a personal account leaves behind. Without one no
fake deployment could be attested at all.
"""


def claude_subscription_deployment(
    directory: Path,
    program: str,
    version: str | None = MEASURED_CLAUDE_VERSION,
    subscription_type: str | None = PERSONAL_SUBSCRIPTION_TYPE,
) -> ClaudeSubscriptionSettings:
    """Deploy one executable Python program in place of the Claude CLI."""

    executable = directory / "claude"
    executable.write_text(
        f"#!{sys.executable}\n{_version_answering(program, version)}",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    workspace = directory / "workspace"
    workspace.mkdir()
    credentials = directory / "credentials"
    credentials.mkdir()
    # The credential record an authenticated CLI would have left behind.
    (credentials / CREDENTIAL_RECORD_ENTRY).write_text(
        json.dumps({"claudeAiOauth": {"subscriptionType": subscription_type}}),
        encoding="utf-8",
    )
    return ClaudeSubscriptionSettings(
        executable, workspace, credentials, claude_search_path(directory)
    )


CLAUDE_SUBSCRIPTION_WORKFLOW = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
"""


def claude_subscription_runtime(
    root: Path, settings: ClaudeSubscriptionSettings
) -> DbosRuntime:
    """The production runtime, serving exactly the Claude subscription executor."""

    return DbosRuntime(
        DbosRuntimeSettings(root / "atelier.sqlite", "claude-subscription-test"),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("claude-subscription-test"),
        ),
        ExactOutputAgentExecutorFactory(),
        (ClaudeSubscriptionExecutorFactory(settings),),
    )


def claude_subscription_publication(
    runtime: DbosRuntime,
    model: str = "claude-haiku-4-5",
    requested_capability: AgentExecutionCapability = AgentExecutionCapability.HEADLESS,
) -> tuple[AgentConfigurationRevision, WorkflowRevision]:
    """Publish one configuration and workflow through the production catalog.

    The catalog binds a configuration to an executor key, not to a capability,
    so a configuration demanding what this executor cannot serve is published
    here exactly as a real one would be -- and refused where the demand is
    actually read.
    """

    catalog = DbosAgentConfigurationCatalog(
        runtime.engine, runtime.agent_executor_registry
    )
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    catalog.publish_auth_profile_revision(auth)
    configuration = AgentConfigurationRevision(
        model,
        auth.revision_hash,
        CLAUDE_SUBSCRIPTION_EXECUTOR_KEY.executor_revision,
        requested_capability,
        AgentConfigurationRevisionFormatVersion.V2,
    )
    catalog.publish_agent_configuration_revision(configuration)
    workflow = WorkflowRevision(CLAUDE_SUBSCRIPTION_WORKFLOW)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
    return configuration, workflow


def claude_subscription_start(
    runtime: DbosRuntime,
    run_name: str,
    model: str = "claude-haiku-4-5",
    requested_capability: AgentExecutionCapability = AgentExecutionCapability.HEADLESS,
) -> tuple[DurablePublishedRunResult, WorkflowRevision]:
    """Ask the production starter for one run bound to this executor."""

    configuration, workflow = claude_subscription_publication(
        runtime, model, requested_capability
    )
    started = DbosDurableRunStarter(
        runtime.engine, runtime.settings, runtime.agent_executor_registry
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


def claude_subscription_attempt(
    runtime: DbosRuntime, run_name: str, model: str = "claude-haiku-4-5"
) -> AgentAttemptExecution:
    """One durable run whose only agent node is bound to this executor."""

    run_id = RunId(run_name)
    started, workflow = claude_subscription_start(runtime, run_name, model)
    assert isinstance(started, DurableRunCreated)
    assert isinstance(started.run, RunV2)
    return agent_attempt_execution(
        AgentExecutionRequestV2(
            NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
            run_id,
            workflow.revision_hash,
            "build",
            started.run.agent_bindings[0],
            CLAUDE_SUBSCRIPTION_OPERATIONAL_IDENTITY,
            b"build",
        )
    )


def agent_attempt_execution(
    request: AgentExecutionRequestV2, ordinal: int = 1
) -> AgentAttemptExecution:
    return AgentAttemptExecution(
        request,
        AgentAttemptId.for_execution(
            request.node_execution_id, request.request_hash, ordinal
        ),
        ordinal,
    )


AgentProcessCommand = Callable[[AgentExecutionRequestV2], AgentProcessInvocation]
"""How a scenario executor turns a request into the process it launches."""

AgentCompletionDecoder = Callable[
    [AgentProcessCompletion], AgentExecutionResult | AgentExecutionFailure
]
"""How a scenario executor reads a finished process back."""

_PROVIDER_WRITES_HEX_AFTER_DELAY = (
    "import os, sys, time; time.sleep(float(sys.argv[1])); "
    "os.write(1, bytes.fromhex(sys.argv[2]))"
)


def launching(
    *arguments: str, frame_bytes: int = SCENARIO_PROVIDER_FRAME_BYTES
) -> AgentProcessCommand:
    """A command launching exactly these arguments, whatever the request says."""

    def command(request: AgentExecutionRequestV2) -> AgentProcessInvocation:
        del request
        return AgentProcessInvocation(
            arguments, Path.cwd(), standard_output_frame_bytes=frame_bytes
        )

    return command


def emitting(
    output: bytes,
    *,
    delay_seconds: float = 0,
    frame_bytes: int = SCENARIO_PROVIDER_FRAME_BYTES,
) -> AgentProcessCommand:
    """A command whose process writes exactly these bytes to standard output."""

    return launching(
        sys.executable,
        "-c",
        _PROVIDER_WRITES_HEX_AFTER_DELAY,
        str(delay_seconds),
        output.hex(),
        frame_bytes=frame_bytes,
    )


def refusing(reason: str) -> Callable[[Any], Never]:
    """A command or decoder for an executor this scenario must never reach."""

    def refuse(_unreached: Any) -> Never:
        raise AssertionError(reason)

    return refuse


def decode_process_exit(
    completion: AgentProcessCompletion,
) -> AgentExecutionResult | AgentExecutionFailure:
    """Read standard output back, or name the exit that was not successful."""

    if completion.return_code != 0:
        return AgentExecutionFailure(
            AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
        )
    return AgentExecutionResult(completion.standard_output)


def decoding_to(output: bytes) -> AgentCompletionDecoder:
    """A decoder reading every successful process back as exactly these bytes."""

    def decode(
        completion: AgentProcessCompletion,
    ) -> AgentExecutionResult | AgentExecutionFailure:
        decoded = decode_process_exit(completion)
        if isinstance(decoded, AgentExecutionFailure):
            return decoded
        return AgentExecutionResult(output)

    return decode


def answering(
    outcome: AgentExecutionResult | AgentExecutionFailure,
) -> AgentCompletionDecoder:
    """A decoder answering this outcome for any process, however it exited."""

    def decode(
        completion: AgentProcessCompletion,
    ) -> AgentExecutionResult | AgentExecutionFailure:
        del completion
        return outcome

    return decode


@dataclass
class RecordingAgentExecutorV2:
    output: bytes = b""
    requests: list[AgentExecutionRequestV2] = field(default_factory=list)
    lifecycle: list[str] = field(default_factory=list)
    name: str = "recording"
    closes: int = 0
    command: AgentProcessCommand | None = None
    """How the process is launched; by default it writes `output` and exits."""
    decoder: AgentCompletionDecoder = decode_process_exit
    close_failure: BaseException | None = None
    """Raised once the close has been recorded, for cleanup-failure scenarios."""
    completions: list[AgentProcessCompletion] = field(default_factory=list)
    results: list[AgentExecutionResult | AgentExecutionFailure] = field(
        default_factory=list
    )
    released_invocations: list[AgentProcessInvocation] = field(default_factory=list)

    def prepare_process(
        self, request: AgentExecutionRequestV2
    ) -> AgentProcessInvocation:
        self.requests.append(request)
        self.lifecycle.append(f"execute:{self.name}")
        return (self.command or emitting(self.output))(request)

    def decode_process_completion(
        self, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        self.completions.append(completion)
        result = self.decoder(completion)
        self.results.append(result)
        return result

    def release_process(self, invocation: AgentProcessInvocation) -> None:
        self.released_invocations.append(invocation)

    def close(self) -> None:
        self.closes += 1
        self.lifecycle.append(f"close:{self.name}")
        if self.close_failure is not None:
            raise self.close_failure


@dataclass
class RecordingAgentExecutorFactoryV2:
    provider: str
    revision: str
    operational_identity_value: str
    output: bytes
    lifecycle: list[str] = field(default_factory=list)
    capability_set: frozenset[AgentExecutionCapability] = field(
        default_factory=lambda: frozenset({AgentExecutionCapability.HEADLESS})
    )
    key_reads: int = 0
    identity_reads: int = 0
    opens: int = 0
    opened: RecordingAgentExecutorV2 | None = None
    command: AgentProcessCommand | None = None
    decoder: AgentCompletionDecoder = decode_process_exit
    open_failure: BaseException | None = None
    """Raised once the open has been recorded, for open-failure scenarios."""
    close_failure: BaseException | None = None

    @property
    def key(self) -> AgentExecutorKey:
        self.key_reads += 1
        return AgentExecutorKey(
            ProviderId(self.provider), AgentExecutorRevision(self.revision)
        )

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        self.identity_reads += 1
        return AgentExecutorOperationalIdentity(self.operational_identity_value)

    @property
    def declared_capabilities(self) -> frozenset[AgentExecutionCapability]:
        return self.capability_set

    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        if self.open_failure is not None:
            raise self.open_failure
        self.opened = RecordingAgentExecutorV2(
            self.output,
            [],
            self.lifecycle,
            self.provider,
            command=self.command,
            decoder=self.decoder,
            close_failure=self.close_failure,
        )
        return self.opened


def failing_agent_executor_factory(
    provider: str,
    lifecycle: list[str],
    *,
    open_failure: BaseException | None = None,
    close_failure: BaseException | None = None,
) -> RecordingAgentExecutorFactoryV2:
    """A factory named after its provider whose open or close fails as told."""

    return RecordingAgentExecutorFactoryV2(
        provider,
        f"{provider}/v1",
        f"{provider}-operation",
        b"",
        lifecycle,
        open_failure=open_failure,
        close_failure=close_failure,
    )
