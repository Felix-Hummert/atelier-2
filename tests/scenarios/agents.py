from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


@dataclass
class RecordingAgentExecutorV2:
    output: bytes
    requests: list[AgentExecutionRequestV2]
    lifecycle: list[str]
    name: str
    closes: int = 0

    def prepare_process(
        self, request: AgentExecutionRequestV2
    ) -> AgentProcessInvocation:
        self.requests.append(request)
        self.lifecycle.append(f"execute:{self.name}")
        return AgentProcessInvocation(
            (
                sys.executable,
                "-c",
                "import os; os.write(1, bytes.fromhex(__import__('sys').argv[1]))",
                self.output.hex(),
            ),
            Path.cwd(),
            standard_output_frame_bytes=SCENARIO_PROVIDER_FRAME_BYTES,
        )

    def decode_process_completion(
        self, completion: AgentProcessCompletion
    ) -> AgentExecutionResult | AgentExecutionFailure:
        if completion.return_code != 0:
            return AgentExecutionFailure(
                AgentAttemptFailureCode.PROCESS_EXITED_UNSUCCESSFULLY
            )
        return AgentExecutionResult(completion.standard_output)

    def close(self) -> None:
        self.closes += 1
        self.lifecycle.append(f"close:{self.name}")


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
        self.opened = RecordingAgentExecutorV2(
            self.output, [], self.lifecycle, self.provider
        )
        return self.opened
