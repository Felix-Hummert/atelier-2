from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from atelier2.adapters.dbos.agent_attempt_store import DbosAgentAttemptStore
from atelier2.adapters.dbos.agent_catalog import DbosAgentConfigurationCatalog
from atelier2.adapters.dbos.queries import DbosQueries
from atelier2.adapters.dbos.runtime import DbosRuntime, DbosRuntimeSettings
from atelier2.adapters.dbos.starter import (
    DbosDurableRunStarter,
    DbosWorkflowRevisionPublisher,
)
from atelier2.adapters.exact_output_agent import ExactOutputAgentExecutorFactory
from atelier2.adapters.loopback import LoopbackEffectAdapterFactory
from atelier2.application.execute_agent_attempt import execute_agent_attempt
from atelier2.contracts.agents import (
    AgentBinding,
    AgentBindingSet,
    AgentConfigurationRevision,
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
from atelier2.contracts.executions import NodeExecutionId
from atelier2.contracts.runs import RunId, WorkflowRevision
from atelier2.ports.agent_executions import AgentExecutionFailure, AgentExecutorKey
from atelier2.ports.durable_runs import StartPublishedRunRequestV2
from atelier2.ports.run_queries import RunFound

CRASHED = 86
DOCUMENT = b"""format_version: 2
start: build
nodes:
  - {id: done, type: subworkflow, operation: add, operands: [2, 3], next: null}
  - {id: build, type: agent, role: builder, job: build, next: done}
"""


@dataclass
class InertExecutor:
    def execute(
        self, request: AgentExecutionRequestV2
    ) -> AgentExecutionResult | AgentExecutionFailure:
        del request
        raise AssertionError(
            "runtime-owned executor is not the controlled test process"
        )

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class InertFactory:
    @property
    def key(self) -> AgentExecutorKey:
        return AgentExecutorKey(
            ProviderId("anthropic"), AgentExecutorRevision("claude-cli/v1")
        )

    @property
    def operational_identity(self) -> AgentExecutorOperationalIdentity:
        return AgentExecutorOperationalIdentity("controlled-process")

    def open(self) -> InertExecutor:
        return InertExecutor()


@dataclass
class ControlledProcessExecutor:
    counter: Path

    def execute(
        self, request: AgentExecutionRequestV2
    ) -> AgentExecutionResult | AgentExecutionFailure:
        del request
        run_controlled_process(self.counter)
        return AgentExecutionResult(b"done")

    def close(self) -> None:
        pass


def run_controlled_process(counter: Path) -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; Path(__import__('sys').argv[1]).open('ab').write(b'x')",
            str(counter),
        ],
        check=True,
        timeout=10,
    )


def runtime(root: Path) -> DbosRuntime:
    return DbosRuntime(
        DbosRuntimeSettings(root / "atelier.sqlite", "agent-attempt-crash"),
        LoopbackEffectAdapterFactory(
            root / "effects.sqlite",
            AdapterRevision("loopback-v1"),
            EffectDestination("agent-attempt-crash"),
        ),
        ExactOutputAgentExecutorFactory(),
        (InertFactory(),),
    )


def request(lease: DbosRuntime) -> AgentExecutionRequestV2:
    lease.initialize_storage()
    auth = AuthProfileRevision("max", 1, ProviderId("anthropic"), AuthMode.SUBSCRIPTION)
    configuration = AgentConfigurationRevision(
        "opus", auth.revision_hash, AgentExecutorRevision("claude-cli/v1")
    )
    catalog = DbosAgentConfigurationCatalog(lease.engine, lease.agent_executor_registry)
    catalog.publish_auth_profile_revision(auth)
    catalog.publish_agent_configuration_revision(configuration)
    workflow = WorkflowRevision(DOCUMENT)
    DbosWorkflowRevisionPublisher(lease.engine).publish(workflow)
    binding_set = AgentBindingSet(
        (AgentBinding(AgentRole("builder"), configuration.revision_hash),)
    )
    run_id = RunId("agent-attempt/crash")
    DbosDurableRunStarter(
        lease.engine, lease.settings, lease.agent_executor_registry
    ).start_published(
        StartPublishedRunRequestV2(run_id, workflow.revision_hash, binding_set)
    )
    resolved = ResolvedAgentBinding(AgentRole("builder"), configuration, auth)
    return AgentExecutionRequestV2(
        NodeExecutionId.for_node(run_id, workflow.revision_hash, "build"),
        run_id,
        workflow.revision_hash,
        "build",
        resolved,
        AgentExecutorOperationalIdentity("controlled-process"),
        b"build",
    )


def main(root: Path, mode: str) -> None:
    lease = runtime(root)
    try:
        exact_request = request(lease)
        store = DbosAgentAttemptStore(lease.engine)
        if mode == "crash-prepared":
            store.prepare(exact_request)
            os._exit(CRASHED)
        if mode == "crash-armed":
            store.prepare(exact_request)
            store.claim(exact_request)
            run_controlled_process(root / "counter")
            os._exit(CRASHED)
        if mode == "recover":
            execute_agent_attempt(
                exact_request,
                ControlledProcessExecutor(root / "counter"),
                store,
            )
            found = DbosQueries(lease.engine).get_run(exact_request.run_id)
            if isinstance(found, RunFound):
                attempt = found.projection.current_agent_attempt
                if attempt is not None:
                    (root / "projected-attempt-state").write_text(
                        attempt.state, encoding="utf-8"
                    )
            return
        raise ValueError(f"unknown mode {mode!r}")
    finally:
        lease.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2])
