from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atelier2.adapters.claude_subscription import (
    CLAUDE_SUBSCRIPTION_EXECUTOR_KEY,
    CLAUDE_SUBSCRIPTION_OPERATIONAL_IDENTITY,
    MINIMUM_CLAUDE_VERSION,
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
    AgentExecutionCapability,
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


MEASURED_CLAUDE_VERSION = ".".join(str(part) for part in MINIMUM_CLAUDE_VERSION)


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


def claude_subscription_deployment(
    directory: Path,
    program: str,
    version: str | None = MEASURED_CLAUDE_VERSION,
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
    return ClaudeSubscriptionSettings(executable, workspace, credentials, os.defpath)


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


def claude_subscription_start(
    runtime: DbosRuntime,
    run_name: str,
    model: str = "claude-haiku-4-5",
    requested_capability: AgentExecutionCapability = AgentExecutionCapability.HEADLESS,
) -> tuple[DurablePublishedRunResult, WorkflowRevision]:
    """Ask the production starter for one run bound to this executor."""

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
    )
    catalog.publish_agent_configuration_revision(configuration)
    workflow = WorkflowRevision(CLAUDE_SUBSCRIPTION_WORKFLOW)
    DbosWorkflowRevisionPublisher(runtime.engine).publish(workflow)
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
        return frozenset({AgentExecutionCapability.HEADLESS})

    def open(self) -> RecordingAgentExecutorV2:
        self.opens += 1
        self.lifecycle.append(f"open:{self.provider}")
        self.opened = RecordingAgentExecutorV2(
            self.output, [], self.lifecycle, self.provider
        )
        return self.opened
